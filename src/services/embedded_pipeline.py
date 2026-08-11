"""内嵌文本提取管线服务（从 text_panel 抽取，无 UI 依赖）

流程：扫描 → 合并持久化 → AI 预筛（只判未决）→ [人工确认/全部重判] →
标记源码 → SDK 重生成 → 解析合并入库。

粒度化 API，NiceGUI 面板与 FastAPI 任务共用同一实现：
    pipe = EmbeddedPipeline(db, translator, project_dir, sdk_path, logger)
    rows = await pipe.scan_and_merge()
    await pipe.screen_undecided(rows, on_progress=lambda phase, done, total: ...)
    # ... 人工确认（UI/任务 ask）；重判 → pipe.rescreen_all(rows) ...
    result = await pipe.apply_selection(rows, chosen_rows, stage=lambda text: ...)

失败哲学：不降级——无翻译器/无 SDK/AI 失败均抛异常，由调用方响亮呈现。
"""
import asyncio
import time
from pathlib import Path

from database import ProjectDatabase
from logger import TranslationLogger


def _swallow_task_error(task):
    """取回已结束 executor 任务的异常，避免 'exception was never retrieved'"""
    try:
        task.result()
    except Exception:
        pass


class EmbeddedPipeline:
    def __init__(self, db: ProjectDatabase, translator,
                 project_dir: str, sdk_path: str, logger: TranslationLogger):
        if not translator:
            raise RuntimeError('AI 预筛需要翻译器，请先配置模型')
        from embedded_strings import resolve_source_root
        self.db = db
        self.translator = translator
        self.project_dir = Path(project_dir)
        self.game_root = self.project_dir / 'game'
        # find_candidates 的 rel_file 基准：game/ 子目录存在时以它为根
        # （否则源码查看/精判的工具读文件会错位到 game_root 下而 404）
        self.base_dir = resolve_source_root(self.game_root)
        self.sdk_path = sdk_path
        self.logger = logger

    # ---- 步骤 1: 扫描 + 合并持久化 ----

    async def scan_and_merge(self) -> list:
        """扫描源码候选并合并入库（恢复历史 AI 判定/状态）。

        Returns: 待确认行列表 [{id, ai_keep, ai_reason, status, candidate}, ...]
        空列表 = 无候选或全部已处理。
        """
        from embedded_strings import find_candidates
        loop = asyncio.get_event_loop()

        candidates = await loop.run_in_executor(
            None, find_candidates, str(self.game_root))
        if not candidates:
            return []

        rows = await loop.run_in_executor(
            None, self.db.merge_embedded_candidates, candidates)
        for r in rows:
            c = r['candidate']
            if r['ai_keep'] != -1:
                c.ai_keep = bool(r['ai_keep'])
                c.ai_reason = r['ai_reason']
        return rows

    # ---- 步骤 2: AI 预筛（只判未决，失败上抛） ----

    async def screen_undecided(self, rows, on_progress=None, cancel_event=None):
        """对 ai_keep==-1 的候选跑 AI 预筛并保存判定到 db。

        on_progress(phase, done, total)：异步轮询友好（从事件循环线程调用）。
        cancel_event: 可选 threading.Event（任务取消信号），置位时中止
            预筛并抛 ScreeningCancelled（不写库，未决行保持未决）。
        """
        from ai_screener import AIScreener, ScreeningCancelled
        loop = asyncio.get_event_loop()

        undecided = [r for r in rows if r['ai_keep'] == -1]
        if not undecided:
            return

        screener = AIScreener(self.translator, str(self.base_dir), self.logger)
        progress = {'phase': '粗筛', 'done': 0, 'total': len(undecided),
                    'finished': False}
        targets = [r['candidate'] for r in undecided]
        screen_task = loop.run_in_executor(
            None, screener.screen_all, targets, progress, cancel_event)
        try:
            while not progress.get('finished'):
                if cancel_event is not None and cancel_event.is_set():
                    # screener 批次间看到同一事件会自行中止；吞掉其异常
                    screen_task.add_done_callback(_swallow_task_error)
                    raise ScreeningCancelled('AI 预筛已取消')
                if on_progress:
                    on_progress(progress['phase'], progress['done'], progress['total'])
                await asyncio.sleep(0.5)
            await screen_task
        finally:
            screener.close()

        # 保存判定（含静态分析的危险用途标记）
        for r in undecided:
            c = r['candidate']
            danger = bool(getattr(c, 'static_danger', False))
            await loop.run_in_executor(
                None, self.db.update_embedded_ai,
                r['id'], c.ai_keep, c.ai_reason, danger)
            r['ai_keep'] = 1 if c.ai_keep else 0
            r['ai_reason'] = c.ai_reason
            r['ai_danger'] = 1 if danger else 0

    async def rescreen_all(self, rows, on_progress=None, cancel_event=None):
        """全部重判：清空判定后重新预筛"""
        loop = asyncio.get_event_loop()
        ids = [r['id'] for r in rows]
        await loop.run_in_executor(None, self.db.reset_embedded_ai, ids)
        for r in rows:
            r['ai_keep'] = -1
            r['ai_reason'] = ''
            r['candidate'].ai_keep = None
            r['candidate'].ai_reason = ''
        await self.screen_undecided(rows, on_progress, cancel_event)

    async def refine_rows(self, rows, on_progress=None, cancel_event=None):
        """批量 agentic 精判（跳过粗筛，每行都走带工具的精审），写库

        on_progress(phase, done, total)：与 screen_undecided 相同。
        cancel_event: 可选 threading.Event，置位时抛 ScreeningCancelled。
        """
        from ai_screener import AIScreener, ScreeningCancelled
        from source_tree import SourceTree
        from usage_rules import UsageAnalyzer
        loop = asyncio.get_event_loop()
        if not rows:
            return

        cands = [r['candidate'] for r in rows]
        for c in cands:
            c.ai_confident = False
        # 静态用途分析先行：证据/危险用途注入精审输入。
        # 静态分析与精审工具共用同一源码缓存（一次调用一棵树）
        tree = SourceTree(str(self.base_dir))
        await loop.run_in_executor(
            None,
            UsageAnalyzer(str(self.base_dir), files=tree.as_dict()).classify_all,
            cands)

        screener = AIScreener(self.translator, str(self.base_dir), self.logger,
                              source_tree=tree)
        progress = {'phase': '精审', 'done': 0, 'total': len(cands),
                    'finished': False}

        def _run():
            try:
                screener._refine_screen(cands, progress, cancel_event)
            finally:
                progress['finished'] = True

        refine_task = loop.run_in_executor(None, _run)
        try:
            while not progress.get('finished'):
                if cancel_event is not None and cancel_event.is_set():
                    refine_task.add_done_callback(_swallow_task_error)
                    raise ScreeningCancelled('AI 精判已取消')
                if on_progress:
                    on_progress(progress['phase'], progress['done'], progress['total'])
                await asyncio.sleep(0.5)
            await refine_task
        finally:
            screener.close()

        for r in rows:
            c = r['candidate']
            danger = bool(getattr(c, 'static_danger', False))
            await loop.run_in_executor(
                None, self.db.update_embedded_ai,
                r['id'], c.ai_keep, c.ai_reason, danger)
            r['ai_keep'] = 1 if c.ai_keep else 0
            r['ai_reason'] = c.ai_reason
            r['ai_danger'] = 1 if danger else 0

    # ---- 单句精判 ----

    async def refine_single(self, row) -> tuple:
        """单句 AI 精判（agentic，带工具），写库并返回 (keep, reason, danger)

        先跑静态用途分析，把出现点证据/危险用途注入精审输入，
        AI 不用从头盲查（refine_by_id 重建的候选没有 static_* 字段）。
        danger 作为独立标记入库/返回，不拼进 reason。
        """
        from ai_screener import AIScreener
        from source_tree import SourceTree
        from usage_rules import UsageAnalyzer
        loop = asyncio.get_event_loop()
        c = row['candidate']
        # 静态分析与精审工具共用同一源码缓存（单句也免两次全树扫描）
        tree = SourceTree(str(self.base_dir))
        await loop.run_in_executor(
            None,
            UsageAnalyzer(str(self.base_dir), files=tree.as_dict()).classify_all,
            [c])
        screener = AIScreener(self.translator, str(self.base_dir), self.logger,
                              source_tree=tree)
        c.ai_confident = False
        try:
            verdicts = await loop.run_in_executor(
                None, screener._refine_batch, [(0, c)])
        finally:
            screener.close()
        keep, reason = verdicts[0]
        danger = bool(getattr(c, 'static_danger', False))
        c.ai_keep, c.ai_reason = keep, reason
        row['ai_keep'] = 1 if keep else 0
        row['ai_reason'] = reason
        row['ai_danger'] = 1 if danger else 0
        await loop.run_in_executor(
            None, self.db.update_embedded_ai, row['id'], keep, reason, danger)
        return keep, reason, danger

    async def refine_by_id(self, row_id: int) -> tuple:
        """按 db 行 id 单句精判（API 用：从 db 重建最小 Candidate）
        返回 (keep, reason, danger)"""
        from embedded_strings import Candidate
        loop = asyncio.get_event_loop()
        rec = await loop.run_in_executor(
            None, self.db.get_embedded_candidate, row_id)
        if not rec:
            raise KeyError(f'候选不存在: {row_id}')
        candidate = Candidate(
            file=str(self.base_dir / rec['rel_file']),
            rel_file=rec['rel_file'], line=rec['line'],
            col_start=rec['col_start'],
            col_end=rec['col_start'] + len(rec['raw']),
            raw=rec['raw'], text=rec['text'], kind=rec['kind'],
            hint=rec['hint'], confidence=rec['confidence'],
        )
        row = {'id': row_id, 'candidate': candidate,
               'ai_keep': rec['ai_keep'], 'ai_reason': rec['ai_reason']}
        return await self.refine_single(row)

    # ---- 步骤 4~6: 标记 → SDK 重生成 → 合并入库 ----

    async def apply_selection(self, rows, chosen_rows, stage=None,
                              cancel_event=None) -> dict:
        """应用人工选择。stage(text) 报告阶段。
        cancel_event: 可选 threading.Event，传给 SDK 子进程以便中止。

        Returns: {'wrapped': int, 'skipped': int, 'inserted': int}
        无 SDK 路径 / SDK 失败均抛异常（已标记的 _() 留在源码里无害，可重跑）。
        """
        from embedded_strings import apply_wrapping
        loop = asyncio.get_event_loop()

        def _stage(text):
            if stage:
                stage(text)

        if not chosen_rows:
            return {'wrapped': 0, 'skipped': 0, 'inserted': 0}

        chosen = [r['candidate'] for r in chosen_rows]

        _stage('正在标记源码...')
        wrapped, skipped = await loop.run_in_executor(None, apply_wrapping, chosen)
        self.logger.info(f'内嵌文本标记: {wrapped} 成功, {skipped} 跳过', panel='ui')
        chosen_ids = {r['id'] for r in chosen_rows}
        await loop.run_in_executor(
            None, self.db.set_embedded_status, list(chosen_ids), 'marked')
        await loop.run_in_executor(
            None, self.db.set_embedded_status,
            [r['id'] for r in rows if r['id'] not in chosen_ids], 'skipped')

        # SDK 重新生成模板
        if not self.sdk_path:
            raise RuntimeError(
                f"已标记 {wrapped} 条，但未配置 Ren'Py SDK 路径，无法重新生成模板")

        _stage('正在重新生成翻译模板...')
        from sdk_manager import SDKManager

        def _regen():
            sdk = SDKManager()
            sdk.sdk_path = Path(self.sdk_path)
            # renpy.exe 在 Windows 上偶发访问冲突崩溃（0xC0000005，与负载/杀软
            # 扫描相关的间歇性崩溃），延迟重试数次
            for attempt in range(1, 6):
                result = sdk.generate_translations(
                    str(self.game_root), 'chinese', cancel_event=cancel_event)
                if result['success'] or result.get('cancelled'):
                    return result
                self.logger.warning(
                    f'SDK 生成模板第 {attempt} 次失败: {result["message"]}', panel='ui')
                time.sleep(2)
            return result

        sdk_result = await loop.run_in_executor(None, _regen)
        if sdk_result.get('cancelled'):
            from ai_screener import ScreeningCancelled
            raise ScreeningCancelled('SDK 重新生成模板已取消')
        if not sdk_result['success']:
            self.logger.error(f'SDK 重新生成模板失败: {sdk_result["message"]}', panel='ui')
            raise RuntimeError(f'SDK 重新生成失败: {sdk_result["message"]}')

        # 合并入库
        _stage('正在合并新字符串入库...')
        from tl_parser import parse_translation_files
        tl_dir = self.game_root / 'game' / 'tl' / 'chinese'
        tl_result = await loop.run_in_executor(
            None, parse_translation_files, tl_dir, str(self.game_root), self.logger)

        # 把提取阶段的出处写进新字符串的 context_hint
        # （候选 text 已反转义，tl old 文本含字面 \n，两种形式都建映射）
        hint_map = {}
        for c in chosen:
            hint_map[c.text] = c.hint
            hint_map[c.text.replace('\n', '\\n').replace('"', '\\"')] = c.hint
        for it in tl_result.get('ui_texts', []):
            if it['original_text'] in hint_map:
                it['context_hint'] = hint_map[it['original_text']]

        inserted = await loop.run_in_executor(
            None, self.db.insert_ui_texts_new_only, tl_result.get('ui_texts', []))

        self.logger.info(
            f'内嵌文本提取完成: 标记 {wrapped}, 新增 {inserted} 条', panel='ui')
        return {'wrapped': wrapped, 'skipped': skipped, 'inserted': inserted}
