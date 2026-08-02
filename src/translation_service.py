"""统一翻译调度服务

单句翻译、批量翻译共用同一套逻辑。
翻译结果立即写入 SQLite。
翻译后自动提取新术语。
"""

import asyncio
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor

from translator import AITranslator, FatalAPIError
from database import ProjectDatabase
from logger import TranslationLogger


class TranslationService:
    """统一翻译调度服务"""

    def __init__(self, translator: AITranslator, db: ProjectDatabase,
                 logger: TranslationLogger, max_concurrent: int = 5,
                 max_context_k: int = 8, max_tokens: int = 1000,
                 batch_lines: int = 100):
        self.translator = translator
        self.db = db
        self.logger = logger
        self.max_concurrent = max_concurrent
        self.max_context_k = max_context_k
        self.max_tokens = max_tokens
        self.batch_lines = batch_lines
        # 可选：每次批量翻译前拉取最新模型配置的回调
        # 返回 (max_context_k, max_tokens, batch_lines) 或 None
        self.config_provider: Optional[Callable] = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cancel_event = asyncio.Event()
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)

    def set_model_config(self, max_context_k: int, max_tokens: int, batch_lines: int = None):
        """设置模型配置"""
        self.max_context_k = max_context_k
        self.max_tokens = max_tokens
        if batch_lines is not None:
            self.batch_lines = batch_lines

    def _calc_context_count(self, glossary_text: str = "", character_profile: str = "") -> int:
        """根据模型上下文能力和提示词实际大小动态计算上下文行数

        可用 token = 模型上下文窗口 - 已占用部分
        """
        total_tokens = self.max_context_k * 1024

        # 估算已占用的 token（中文约 3 字符/token，英文约 4 字符/token）
        glossary_chars = len(glossary_text) if glossary_text else 0
        profile_chars = len(character_profile) if character_profile else 0
        system_chars = 800
        fixed_overhead = (system_chars + glossary_chars + profile_chars) // 3

        # 每行上下文约 50 token（原文 + 译文 + 角色名）
        tokens_per_line = 50

        available = total_tokens - fixed_overhead
        count = max(3, available // tokens_per_line)

        return min(count, 20)

    @property
    def is_running(self) -> bool:
        return self._running

    def _get_glossary_text(self) -> str:
        """获取术语表 + 人名表提示词文本（同步，在线程池中调用）"""
        glossary = self.db.get_glossary_for_prompt()
        characters = self.db.get_characters_for_prompt()
        parts = []
        if characters:
            parts.append(characters)
        if glossary:
            parts.append(glossary)
        return "\n\n".join(parts) if parts else ""

    # ===== 批次翻译 =====

    _BATCH_FIXED_CHARS = 3000  # 系统提示词模板 + 用户提示词模板/格式说明 + 前文参考 的估算开销

    def group_into_batches(self, items: list, glossary_text: str = "",
                           character_profiles: str = "") -> list:
        """按句数与 token 双重上限分组

        1. 每批最多 batch_lines 句（模型配置）
        2. 整体输入（含提示词）+ 估算输出（批原文 × 1.2 + 300）≤ 上下文窗口
        3. 估算输出 ≤ 模型声明 max_tokens
        """
        window_tokens = self.max_context_k * 1024
        fixed_chars = self._BATCH_FIXED_CHARS + len(glossary_text) + len(character_profiles)
        fixed_tokens = fixed_chars // 3

        # fixed + src + src×1.2+300 ≤ window  →  src ≤ (window − fixed − 300) / 2.2
        window_cap = int((window_tokens - fixed_tokens - 300) / 2.2)
        # src×1.2+300 ≤ max_tokens  →  src ≤ (max_tokens − 300) / 1.2
        declared_cap = int((self.max_tokens - 300) / 1.2)
        budget = max(500, min(window_cap, declared_cap))

        batches, current, used = [], [], 0
        for item in items:
            char = item.get('character', '')
            # 编号 + [角色] 标记 + 格式开销
            line_tokens = (len(item.get('original_text', ''))
                           + (len(char) + 4 if char else 0) + 8) // 3
            if current and (len(current) >= self.batch_lines
                            or used + line_tokens > budget):
                batches.append(current)
                current, used = [], 0
            current.append(item)
            used += line_tokens
        if current:
            batches.append(current)
        return batches

    async def prepare_batches(self, items: list, content_type: str) -> list:
        """读取术语表后按预算分组（预算中预留角色特征开销）

        每次批量翻译前通过 config_provider 拉取最新模型配置，
        配置面板保存后无需重新打开项目即可生效。
        """
        loop = asyncio.get_event_loop()
        if self.config_provider:
            cfg = await loop.run_in_executor(None, self.config_provider)
            if cfg:
                self.max_context_k, self.max_tokens, self.batch_lines = cfg
        glossary_text = await loop.run_in_executor(None, self._get_glossary_text)
        # 对话批次需携带批内角色特征，预留约 2000 字符；UI 无此开销
        profile_placeholder = ' ' * 2000 if content_type == 'dialogue' else ''
        return self.group_into_batches(items, glossary_text, profile_placeholder)

    async def translate_batch(self, items: list, content_type: str) -> dict:
        """批次翻译一组条目（一次 API 调用），返回 {item_id: translated}

        解析失败（句数不匹配）或重试耗尽时整批记失败返回空 dict，
        由调用方继续下一批；FatalAPIError 原样上抛。
        """
        if not items:
            return {}
        loop = asyncio.get_event_loop()

        # 术语表 + 人名表（每批一次，不再每句一次）
        glossary_text = await loop.run_in_executor(None, self._get_glossary_text)

        # 批内角色特征汇总（仅对话）
        character_profiles = ""
        if content_type == 'dialogue':
            chars = sorted({it.get('character', '') for it in items if it.get('character')})
            if chars:
                def _get_profiles():
                    parts = []
                    for c in chars:
                        profile = self.db.get_profile(c)
                        if profile:
                            lines = [f"- {k}：{v}" for k, v in profile.items() if v]
                            parts.append(f"[{c}] 人物特征：\n" + "\n".join(lines))
                    return "\n\n".join(parts)
                character_profiles = await loop.run_in_executor(None, _get_profiles)

        # 前文上下文（取批内首条；批内句子互为上下文，取消后文参考）
        context_count = min(self._calc_context_count(glossary_text, character_profiles), 8)
        first_id = items[0]['id']

        def _get_context():
            return self.db.get_dialogue_context(first_id, content_type, count=context_count)

        context_before, _ = await loop.run_in_executor(None, _get_context)

        async with self._semaphore:
            try:
                translated_list, terms = await loop.run_in_executor(
                    self._executor,
                    lambda: self.translator.translate_batch(
                        items,
                        content_type=content_type,
                        glossary_text=glossary_text,
                        character_profiles=character_profiles,
                        context_before=context_before,
                        context_window_tokens=self.max_context_k * 1024,
                    )
                )
            except FatalAPIError:
                # 不可重试的致命错误，向上传递以中止批量任务
                raise
            except Exception as e:
                self.logger.error(f"批次翻译失败（{len(items)} 条）: {e}", panel=content_type)
                return {}

        if translated_list is None:
            # 解析失败（句数不匹配）：整批记失败，继续下一批
            self.logger.warning(
                f"批次解析失败（句数不匹配），{len(items)} 条记为失败",
                panel=content_type
            )
            return {}

        # 逐句写库 + 术语入库
        def _save_all():
            saved = {}
            for it, text in zip(items, translated_list):
                if not text:
                    continue
                if content_type == 'ui':
                    self.db.update_ui_text(it['id'], text)
                else:
                    self.db.update_dialogue(it['id'], text)
                saved[it['id']] = text
            if terms:
                for t in terms:
                    t['term_type'] = 'other'
                    t['source'] = 'ai'
                self.db.add_glossary_batch(terms)
            return saved

        results = await loop.run_in_executor(None, _save_all)
        self.logger.info(f"批次翻译完成: {len(results)}/{len(items)} 条", panel=content_type)
        return results

    async def translate_single(self, item_id: int, content_type: str,
                                original_text: str, character: str = '') -> bool:
        """翻译单条内容 -> 立即写入 SQLite"""
        self.logger.info(f"开始翻译: {original_text[:30]}...", panel=content_type)

        async with self._semaphore:
            loop = asyncio.get_event_loop()
            try:
                # 获取术语表 + 人名表
                glossary_text = await loop.run_in_executor(None, self._get_glossary_text)

                # 获取角色特征（对话翻译时）
                character_profile = ""
                if character and content_type == 'dialogue':
                    def _get_profile():
                        profile = self.db.get_profile(character)
                        if profile:
                            lines = [f"- {k}：{v}" for k, v in profile.items() if v]
                            return f"当前说话角色 [{character}] 的人物特征：\n" + "\n".join(lines)
                        return ""
                    character_profile = await loop.run_in_executor(None, _get_profile)

                # 动态计算上下文行数
                context_count = self._calc_context_count(
                    glossary_text, character_profile
                )

                # 获取 label 上下文
                def _get_context():
                    return self.db.get_dialogue_context(item_id, content_type, count=context_count)

                context_before, context_after = await loop.run_in_executor(None, _get_context)

                # 翻译
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._translate_one_sync(
                        text=original_text,
                        content_type=content_type,
                        character=character,
                        glossary_text=glossary_text,
                        character_profile=character_profile,
                        context_before=context_before,
                        context_after=context_after,
                    )
                )

                # 解析结果：name 返回 str，ui/dialogue 返回 (str, list)
                if content_type == 'name':
                    translated = result
                    terms = []
                else:
                    translated, terms = result if isinstance(result, tuple) else (result, [])

                if translated:
                    # 保存翻译结果
                    def _save():
                        if content_type == 'name':
                            self.db.update_character_cn_name(original_text, translated)
                        elif content_type == 'ui':
                            self.db.update_ui_text(item_id, translated)
                        elif content_type == 'dialogue':
                            self.db.update_dialogue(item_id, translated)
                        # 保存 AI 提取的术语
                        if terms:
                            for t in terms:
                                t['term_type'] = 'other'
                                t['source'] = 'ai'
                            self.db.add_glossary_batch(terms)

                    await loop.run_in_executor(None, _save)

                    self.logger.info(
                        f"翻译完成: {original_text[:20]} -> {translated[:20]}",
                        panel=content_type
                    )
                    return True
                else:
                    self.logger.warning(f"翻译返回空结果: {original_text[:30]}", panel=content_type)
                    return False

            except FatalAPIError:
                # 不可重试的致命错误（认证失败、余额不足等），向上传递以中止批量任务
                raise
            except Exception as e:
                self.logger.error(f"翻译失败: {original_text[:30]} - {e}", panel=content_type)
                return False

    async def stop(self):
        self._cancel_event.set()
        self.logger.info("正在停止翻译...", panel="")

    def _translate_one_sync(self, text: str, content_type: str,
                            character: str = '',
                            glossary_text: str = "",
                            character_profile: str = "",
                            context_before: list = None,
                            context_after: list = None):
        """在线程池中执行的单条翻译（同步方法）

        返回：
        - name: str
        - ui/dialogue: tuple[str, list[dict]]  (译文, 术语列表)
        """
        if content_type == 'name':
            return self.translator.translate_name(
                text, glossary_text=glossary_text, debug=False
            )
        elif content_type == 'ui':
            return self.translator.translate_ui(
                text, glossary_text=glossary_text, debug=False
            )
        elif content_type == 'dialogue':
            return self.translator.translate_text(
                text=text,
                character=character,
                context_before=context_before,
                context_after=context_after,
                glossary_text=glossary_text,
                character_profile=character_profile,
                debug=False
            )
        return ""
