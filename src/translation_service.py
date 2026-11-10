"""统一翻译调度服务

单句翻译、批量翻译共用同一套逻辑。
翻译结果立即写入 SQLite。
翻译后自动提取新术语。
"""

import asyncio
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor

from translator import AITranslator, FatalAPIError
from token_budget import TokenBudget, count_tokens
from database import ProjectDatabase
from logger import TranslationLogger


class TranslationService:
    """统一翻译调度服务"""

    def __init__(self, translator: AITranslator, db: ProjectDatabase,
                 logger: TranslationLogger, max_concurrent: int = 5,
                 max_context_k: int = 8, max_tokens: Optional[int] = None,
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
        self._closed = False

    def set_model_config(self, max_context_k: int, max_tokens: Optional[int],
                         batch_lines: int = None):
        """设置模型配置"""
        self.max_context_k = max_context_k
        self.max_tokens = max_tokens
        if batch_lines is not None:
            self.batch_lines = batch_lines

    def close(self):
        """关闭内部线程池。

        项目关闭/切换时由 state 调用；不 shutdown 的话 ThreadPoolExecutor
        线程只能靠 GC 回收，反复开关项目会持续累积线程。
        """
        self._closed = True
        self._executor.shutdown(wait=False)

    def _check_open(self):
        """executor 关闭后继续翻译是全局不可恢复错误（项目切换/服务停止
        后旧任务仍在跑）——必须当中止任务的致命错误抛出，否则后续每批
        都炸 'cannot schedule new futures after shutdown'，把全部剩余条目
        暂存成垃圾失败记录（实测 57,952 条）"""
        if self._closed:
            raise FatalAPIError(
                503,
                '翻译服务已关闭（项目切换或服务停止）。请重新启动翻译，'
                '未翻译条目会自动拾起。')

    def _calc_context_count(self, glossary_text: str = "", character_profile: str = "") -> int:
        """根据模型上下文能力和提示词实际大小动态计算上下文行数

        可用 token = 模型上下文窗口 - 已占用部分（模板常量 + 术语表/角色特征实测）
        """
        requested = max(0, self.translator.config.context_lines)
        return TokenBudget(self.max_context_k * 1024).context_line_count(
            glossary_tokens=count_tokens(glossary_text),
            profile_tokens=count_tokens(character_profile),
            min_lines=0, max_lines=requested,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    def _get_glossary_text(self) -> str:
        """获取术语表 + 人名表提示词文本（同步，在线程池中调用）

        全量注入（人名翻译的风格锚点与分批预算估算仍用它）；
        对话/UI 批翻译走 _matched_prompt_texts 按批匹配注入。
        """
        glossary = self.db.get_glossary_for_prompt()
        characters = self.db.get_characters_for_prompt()
        parts = []
        if characters:
            parts.append(characters)
        if glossary:
            parts.append(glossary)
        return "\n\n".join(parts) if parts else ""

    def _matched_prompt_texts(self, items: list, extra_texts=None) -> tuple:
        """按批内容匹配注入（同步，在线程池中调用）

        返回 (characters_text, glossary_text)：只注入本批待译原文与前文
        上下文命中的条目（词面命中或归一化边界包含，见 prompt_match）。
        命中率 ≥95% 时回退全量注入——此时全量与匹配几乎等价，保持提示
        词稳定还能吃前缀缓存。
        """
        from prompt_match import match_entries

        texts = [it.get('original_text', '') for it in items]
        texts += [t for t in (extra_texts or []) if t]

        # ---- 人名表 ----
        characters_text = ''
        rows = self.db.get_characters()
        name_entries = [{'i': i, 'en': (c.get('display_name') or '').strip()}
                        for i, c in enumerate(rows)
                        if not c.get('is_placeholder')
                        and (c.get('cn_name') or '').strip()
                        and (c.get('display_name') or '').strip()]
        if name_entries:
            # 本批说话人的显示名直通注入（台词里没提到也必须一致）
            var_map = self.db.get_variable_map()
            speakers = {var_map.get(it.get('character'), '')
                        for it in items if it.get('character')}
            hits = {h['i'] for h in match_entries(texts, name_entries)}
            kept = [rows[i] for i in hits]
            kept += [c for c in rows
                     if (c.get('display_name') or '').strip() in speakers
                     and c not in kept
                     and not c.get('is_placeholder')
                     and (c.get('cn_name') or '').strip()]
            if len(kept) >= len(name_entries) * 0.95:
                characters_text = self.db.get_characters_for_prompt()
            elif kept:
                lines = ["人名对照表（翻译时请使用以下中文名，保持一致性）："]
                for c in kept:
                    lines.append(f"  {c['display_name']} → {c['cn_name']}")
                characters_text = "\n".join(lines)

        # ---- 术语表（静态 UI 参考译表 + 库内词条，同格式分节） ----
        glossary_text = ''
        db_rows = self.db.get_glossary_rows_for_prompt()
        # 静态表已有的英文不重复注入（静态条目随后统一进 UI 分节）
        static_keys = set(self.db.UI_GLOSSARY)
        all_entries = (
            [{'ui': True, 'en': en, 'cn': cn} for en, cn in self.db.UI_GLOSSARY.items()]
            + [{'ui': r.get('term_type') == 'ui', 'en': r['en_term'], 'cn': r['cn_term']}
               for r in db_rows if r['en_term'] not in static_keys])
        hits = match_entries(texts, all_entries)
        if len(hits) >= len(all_entries) * 0.95:
            glossary_text = self.db.get_glossary_for_prompt()
        elif hits:
            ui_hits = [h for h in hits if h['ui']]
            game_hits = [h for h in hits if not h['ui']]
            lines = ["已有术语表（以下术语已有翻译，请直接使用，不要重复提取）："]
            lines.append("")
            lines.append("【UI/菜单文字】")
            for h in ui_hits:
                lines.append(f"  {h['en']} → {h['cn']}")
            if game_hits:
                lines.append("")
                lines.append("【游戏术语】")
                for h in game_hits:
                    lines.append(f"  {h['en']} → {h['cn']}")
            glossary_text = "\n".join(lines)

        return characters_text, glossary_text

    def _get_style_guide(self) -> str:
        """获取作品风格指南（同步，在线程池中调用）"""
        return self.db.get_meta('style_guide')

    # 角色特征注入优先级：语气相关的排前面，其余截断避免稀释
    _PROFILE_PRIORITY = ['说话风格', '翻译建议', '口癖', '称谓', '语气']
    _PROFILE_FIELD_MAX = 300  # 非优先字段的最大字符数

    def _load_options(self):
        """读项目翻译模式选项（同步，调用方放 executor）"""
        from translate_options import parse_options
        return parse_options(self.db.get_meta('translate_options'))

    @classmethod
    def _format_character_profiles(cls, chars: list, get_profile,
                                   only_keys=None) -> str:
        """汇总批内角色特征，按语气相关度排序注入

        only_keys 非空时只保留键名含其一的字段（轻量模式只留「翻译建议」）。
        """
        parts = []
        for c in chars:
            profile = get_profile(c)
            if not profile:
                continue
            priority, rest = [], []
            for k, v in profile.items():
                if not v:
                    continue
                if only_keys and not any(p in k for p in only_keys):
                    continue
                line = f"- {k}：{v}"
                if any(p in k for p in cls._PROFILE_PRIORITY):
                    priority.append(line)
                else:
                    rest.append(line[:cls._PROFILE_FIELD_MAX])
            lines = priority + rest
            if not lines:
                continue
            parts.append(f"[{c}] 人物特征：\n" + "\n".join(lines))
        return "\n\n".join(parts)

    # ===== 批次翻译 =====

    def group_into_batches(self, items: list, glossary_tokens: int = 0,
                           profile_tokens: int = 0,
                           profile_tokens_by_character: Optional[dict[str, int]] = None) -> list:
        """按句数与 token 双重上限分组

        1. 每批最多 batch_lines 句（模型配置）
        2. 整体输入（含提示词）+ 估算输出（批原文 × 1.2 + 300）≤ 上下文窗口
        3. 估算输出 ≤ 模型声明 max_tokens

        角色资料按候选批次内的不同角色累加，不能给每批扣除全项目角色资料。
        profile_tokens 保留给固定的额外资料；角色映射中的值含段落分隔开销。
        """
        token_budget = TokenBudget(self.max_context_k * 1024)
        profiles = profile_tokens_by_character or {}

        def source_budget(profile_size):
            return token_budget.batch_src_token_budget(
                glossary_tokens=glossary_tokens,
                profile_tokens=profile_size,
                declared_max_tokens=self.max_tokens or 0,
            )

        batches, current, used = [], [], 0
        current_chars = set()
        current_profile_tokens = profile_tokens
        for item in items:
            char = item.get('character', '')
            # 编号 + [角色] 标记 + 格式开销
            line_tokens = count_tokens(item.get('original_text', '')) \
                + (count_tokens(char) + 2 if char else 0) + 4
            added_profile_tokens = profiles.get(char, 0) if char not in current_chars else 0
            if current and (len(current) >= self.batch_lines
                            or used + line_tokens > source_budget(
                                current_profile_tokens + added_profile_tokens)):
                batches.append(current)
                current, used = [], 0
                current_chars = set()
                current_profile_tokens = profile_tokens
                added_profile_tokens = profiles.get(char, 0)
            current.append(item)
            used += line_tokens
            current_chars.add(char)
            current_profile_tokens += added_profile_tokens
        if current:
            batches.append(current)
        return batches

    async def prepare_batches(self, items: list, content_type: str) -> list:
        """读取术语表与角色特征后按实测 token 预算分组

        每次批量翻译前通过 config_provider 拉取最新模型配置，
        配置面板保存后无需重新打开项目即可生效。
        """
        loop = asyncio.get_event_loop()
        if self.config_provider:
            cfg = await loop.run_in_executor(None, self.config_provider)
            if cfg:
                self.max_context_k, self.max_tokens, self.batch_lines = cfg
        def prepare():
            opts = self._load_options()
            glossary_tokens = 0
            if opts.glossary:
                glossary_tokens += count_tokens(self._get_glossary_text())
            if opts.style_guide:
                glossary_tokens += count_tokens(self._get_style_guide())
            profiles = {}
            if content_type == 'dialogue' and opts.character_profile != 'off':
                only_keys = ('翻译建议',) if opts.character_profile == 'advice_only' else None
                chars = {it.get('character', '') for it in items if it.get('character')}
                for char in chars:
                    text = self._format_character_profiles(
                        [char], self.db.get_profile, only_keys=only_keys)
                    profiles[char] = count_tokens(text) + 2 if text else 0
            return self.group_into_batches(
                items, glossary_tokens, profile_tokens_by_character=profiles)

        # 大项目分词与分组也放在线程池，避免准备阶段阻塞 API 事件循环。
        return await loop.run_in_executor(None, prepare)

    async def translate_batch(self, items: list, content_type: str,
                              stash_on_failure: bool = True) -> dict:
        """批次翻译一组条目（一次 API 调用），返回 {item_id: translated}

        能匹配的译文直接入库；未匹配/存疑的条目在 stash_on_failure=True 时
        逐条暂存到 failed_batches 表（翻译页可重试/单翻/手动），False 时仅
        从返回 dict 中缺席（失败条目重试任务自己管理暂存，避免重复入库）。
        FatalAPIError 原样上抛。
        """
        if not items:
            return {}
        self._check_open()
        loop = asyncio.get_event_loop()

        # 翻译模式开关：关闭的注入项直接置空，[[?token]] 可选段整段消失
        opts = await loop.run_in_executor(None, self._load_options)

        # 批内角色特征汇总（仅对话；轻量模式只注入「翻译建议」）
        character_profiles = ""
        if content_type == 'dialogue' and opts.character_profile != 'off':
            chars = sorted({it.get('character', '') for it in items if it.get('character')})
            only_keys = ('翻译建议',) if opts.character_profile == 'advice_only' else None
            if chars:
                character_profiles = await loop.run_in_executor(
                    None, lambda: self._format_character_profiles(
                        chars, self.db.get_profile, only_keys=only_keys)
                )

        # 前文上下文（取批内首条；批内句子互为上下文，取消后文参考）。
        # 预算按全量术语/档案保守估算——实际注入的是匹配子集，只会更小
        context_before = []
        if opts.context:
            budget_text = (self._get_glossary_text()
                           if (opts.glossary or opts.names) else '')
            context_count = min(self._calc_context_count(budget_text, character_profiles), 8)
            first_id = items[0]['id']

            def _get_context():
                return self.db.get_dialogue_context(first_id, content_type, count=context_count)

            context_before, _ = await loop.run_in_executor(None, _get_context)

        style_guide = ''
        if opts.style_guide:
            style_guide = await loop.run_in_executor(None, self._get_style_guide)

        # 术语表 + 人名表：按批内容（含上下文行）匹配注入，不再全量重发
        names_text = glossary_text = ''
        if opts.glossary or opts.names:
            def _match():
                extra = [r.get('original_text', '') for r in context_before]
                nt, gt = self._matched_prompt_texts(items, extra)
                return (nt if opts.names else ''), (gt if opts.glossary else '')
            names_text, glossary_text = await loop.run_in_executor(None, _match)

        async with self._semaphore:
            cache_before = (getattr(self.translator, 'cache_hit_total', 0),
                            getattr(self.translator, 'cache_prompt_total', 0))
            try:
                translated_map, terms, fail_reasons, rejected_map = \
                    await loop.run_in_executor(
                        self._executor,
                        lambda: self.translator.translate_batch(
                            items,
                            content_type=content_type,
                            glossary_text=glossary_text,
                            character_profiles=character_profiles,
                            context_before=context_before,
                            style_guide=style_guide,
                            characters_text=names_text,
                            context_window_tokens=self.max_context_k * 1024,
                        )
                    )
            except FatalAPIError:
                # 配置类致命错误（key 无效/余额耗尽）：后续批全会失败，
                # 继续无意义，向上传递以中止批量任务
                raise
            except Exception as e:
                if 'cannot schedule new futures' in str(e):
                    # executor 已被关闭（状态竞争兜底）——同 _check_open，
                    # 致命错误上抛中止任务，不再整批暂存
                    raise FatalAPIError(
                        503,
                        '翻译服务已关闭（项目切换或服务停止）。请重新启动翻译，'
                        '未翻译条目会自动拾起。') from e
                # 整批失败不中断任务：整批暂存（批量任务收尾会自动重试
                # 一轮），返回空结果让后续批次继续
                reason = f'整批异常（已重试 {self.translator.MAX_RETRIES} 次）: {e}'
                if stash_on_failure:
                    def _stash_all():
                        self.db.add_failed_batch(
                            content_type,
                            [{'id': it['id'],
                              'original_text': it.get('original_text', ''),
                              'character': it.get('character', ''),
                              'reason': reason} for it in items],
                            f'整批失败 {len(items)} 条')
                    await loop.run_in_executor(None, _stash_all)
                    self.logger.error(
                        f'整批 {len(items)} 条翻译失败已暂存'
                        '（任务收尾将自动重试）: ' + str(e)[:200],
                        panel=content_type)
                    return {}
                raise RuntimeError(
                    f"批次翻译失败（{len(items)} 条，已重试 "
                    f"{self.translator.MAX_RETRIES} 次）: {e}") from e

        # 逐句写库 + 术语入库 + 未译出条目暂存
        def _save_all():
            saved = {}
            for i, it in enumerate(items):
                text = translated_map.get(i)
                if not text:
                    continue
                text = self.db.save_translation_if_unchanged(
                    content_type, it['id'], it.get('translated_text', ''), text)
                if text:
                    saved[it['id']] = text
            if terms and opts.term_extract:
                for t in terms:
                    # UI 翻译提取的术语单独归类：只进 UI 分节，不再污染
                    # 游戏术语区（对话批次无需注入界面词）
                    t['term_type'] = 'ui' if content_type == 'ui' else 'other'
                    t['source'] = 'ai'
                self.db.add_glossary_batch(terms)
            failed_items = [
                {'id': it['id'],
                 'original_text': it.get('original_text', ''),
                 'character': it.get('character', ''),
                 'reason': fail_reasons.get(i, '模型未返回该句译文'),
                 # AI 返回过但校验被拒的译文（失败条目页面展示供修订/采用）
                 'rejected': rejected_map.get(i, '')}
                for i, it in enumerate(items) if not translated_map.get(i)
            ]
            if failed_items and stash_on_failure:
                self.db.add_failed_batch(
                    content_type, failed_items,
                    f'批次内 {len(failed_items)}/{len(items)} 条未译出')
            return saved, len(failed_items)

        results, n_failed = await loop.run_in_executor(None, _save_all)
        if n_failed:
            self.logger.warning(
                f'{n_failed} 条未译出已暂存，'
                '可在翻译页「失败条目」中重试/单翻/手动',
                panel=content_type)
        self.logger.info(f"批次翻译完成: {len(results)}/{len(items)} 条", panel=content_type)
        # prompt 缓存命中率（DeepSeek/OpenAI 缓存字段存在时才有增量；
        # 提供商不回缓存字段则静默不输出）
        if hasattr(self.translator, 'cache_stats_delta'):
            hit, total = self.translator.cache_stats_delta(cache_before)
            if hit and total:
                self.logger.info(
                    f'prompt 缓存命中 {hit}/{total} tokens'
                    f'（{hit / total * 100:.0f}%）', panel=content_type)
        return results

    async def translate_single(self, item_id: int, content_type: str,
                                original_text: str, character: str = '') -> bool:
        """翻译单条内容 -> 立即写入 SQLite"""
        self.logger.info(f"开始翻译: {original_text[:30]}...", panel=content_type)

        async with self._semaphore:
            loop = asyncio.get_event_loop()
            try:
                # 翻译模式开关：关闭的注入项置空（[[?token]] 整段消失）
                opts = await loop.run_in_executor(None, self._load_options)

                # 获取角色特征（对话翻译时；轻量模式只留「翻译建议」）
                character_profile = ""
                if character and content_type == 'dialogue' and opts.character_profile != 'off':
                    only_keys = ('翻译建议',) if opts.character_profile == 'advice_only' else None

                    def _get_profile():
                        profile = self.db.get_profile(character)
                        if not profile:
                            return ""
                        lines = [f"- {k}：{v}" for k, v in profile.items() if v
                                 and (not only_keys or any(p in k for p in only_keys))]
                        if not lines:
                            return ""
                        return f"当前说话角色 [{character}] 的人物特征：\n" + "\n".join(lines)
                    character_profile = await loop.run_in_executor(None, _get_profile)

                # 动态计算上下文行数（预算按全量术语保守估算）
                context_before, context_after = [], []
                if opts.context:
                    budget_text = (self._get_glossary_text()
                                   if (opts.glossary or opts.names) else '')
                    context_count = self._calc_context_count(
                        budget_text, character_profile
                    )

                    # 获取 label 上下文
                    def _get_context():
                        return self.db.get_dialogue_context(item_id, content_type, count=context_count)

                    context_before, context_after = await loop.run_in_executor(None, _get_context)

                style_guide = ''
                if opts.style_guide:
                    style_guide = await loop.run_in_executor(None, self._get_style_guide)

                # 术语表 + 人名表：按本句+上下文匹配注入（name 类型保持全量——
                # 人名翻译需要完整人名表做风格锚点）
                names_text = glossary_text = ''
                if content_type == 'name':
                    if opts.glossary:
                        glossary_text = await loop.run_in_executor(None, self._get_glossary_text)
                elif opts.glossary or opts.names:
                    def _match():
                        extra = [r.get('original_text', '')
                                 for r in context_before + context_after]
                        item = {'original_text': original_text, 'character': character}
                        nt, gt = self._matched_prompt_texts([item], extra)
                        return (nt if opts.names else ''), (gt if opts.glossary else '')
                    names_text, glossary_text = await loop.run_in_executor(None, _match)

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
                        style_guide=style_guide,
                        characters_text=names_text,
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
                        # 保存 AI 提取的术语（term_extract 关闭时丢弃）
                        if terms and opts.term_extract:
                            for t in terms:
                                t['term_type'] = 'ui' if content_type == 'ui' else 'other'
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
                            context_after: list = None,
                            style_guide: str = "",
                            characters_text: str = ""):
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
                text, glossary_text=glossary_text, style_guide=style_guide,
                characters_text=characters_text, debug=False
            )
        elif content_type == 'dialogue':
            return self.translator.translate_text(
                text=text,
                character=character,
                context_before=context_before,
                context_after=context_after,
                glossary_text=glossary_text,
                character_profile=character_profile,
                style_guide=style_guide,
                characters_text=characters_text,
                debug=False
            )
        return ""
