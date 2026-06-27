"""统一翻译调度服务

单句翻译、批量翻译共用同一套逻辑。
翻译结果立即写入 SQLite。
翻译后自动提取新术语。
"""

import asyncio
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor

from translator import AITranslator
from database import ProjectDatabase
from logger import TranslationLogger


class TranslationService:
    """统一翻译调度服务"""

    def __init__(self, translator: AITranslator, db: ProjectDatabase,
                 logger: TranslationLogger, max_concurrent: int = 5,
                 max_context_k: int = 8, max_tokens: int = 1000):
        self.translator = translator
        self.db = db
        self.logger = logger
        self.max_concurrent = max_concurrent
        self.max_context_k = max_context_k
        self.max_tokens = max_tokens
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cancel_event = asyncio.Event()
        self._running = False
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)

    def set_model_config(self, max_context_k: int, max_tokens: int):
        """设置模型配置"""
        self.max_context_k = max_context_k
        self.max_tokens = max_tokens

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

    async def translate_single(self, item_id: int, content_type: str,
                                original_text: str, character: str = '') -> bool:
        """翻译单条内容 -> 立即写入 SQLite"""
        self.logger.info(f"开始翻译: {original_text[:30]}...", panel=content_type)

        async with self._semaphore:
            loop = asyncio.get_event_loop()
            try:
                # 获取术语表 + 人名表
                def _get_glossary():
                    glossary = self.db.get_glossary_for_prompt()
                    characters = self.db.get_characters_for_prompt()
                    parts = []
                    if characters:
                        parts.append(characters)
                    if glossary:
                        parts.append(glossary)
                    return "\n\n".join(parts) if parts else ""

                glossary_text = await loop.run_in_executor(None, _get_glossary)

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
