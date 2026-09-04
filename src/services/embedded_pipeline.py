"""内嵌文本提取管线服务（从 text_panel 抽取，无 UI 依赖）

流程：扫描 → 合并持久化 → AI 预筛（只判未决）→ [人工确认/灰区复核] →
应用选择（统一标记 + strings 表重生成；_() 包裹推迟到导出副本应用）。

源码只读原则：标记动作对游戏源码零写入。wrap 路径的译文条目与 table
路径一样经 zz 表合成（strings 表与 _() 共用 old/new 存储），实际包裹
由 GameExporter._apply_marked_wraps 在导出副本上做（含位置漂移重定位）。

粒度化 API，NiceGUI 面板与 FastAPI 任务共用同一实现：
    pipe = EmbeddedPipeline(db, translator, project_dir, sdk_path, logger)
    rows = await pipe.scan_and_merge()
    await pipe.screen_undecided(rows, on_progress=lambda phase, done, total: ...)
    # ... 人工确认（UI/任务 ask）；复核 → pipe.recheck_grayzone(rows) ...
    result = await pipe.apply_selection(rows, chosen_rows, stage=lambda text: ...)

失败哲学：不降级——无翻译器/AI 失败均抛异常，由调用方响亮呈现。
"""
import asyncio
import re
from pathlib import Path

from database import ProjectDatabase
from logger import TranslationLogger


def _swallow_task_error(task):
    """取回已结束 executor 任务的异常，避免 'exception was never retrieved'"""
    try:
        task.result()
    except Exception:
        pass


def _grayzone_rows(rows: list, flip_ids: set) -> list:
    """灰区复核目标（判定与证据可能冲突的行；纯函数便于单测）：
    - 判 keep 且有非显示用途警告（ai_danger）——危险方向必须核实
    - 判 drop 但像自然语言长文本（去插值后 ≥20 字符且 ≥3 词）——可能错杀
    - 历史翻转过的行（verdict_log 中出现过不同判定）
    """
    out = []
    for r in rows:
        if r['ai_keep'] == 1 and r.get('ai_danger'):
            out.append(r)
            continue
        if r['ai_keep'] == 0:
            text = re.sub(r'\[[^\]]*\]', '', r['candidate'].text).strip()
            if len(text) >= 20 and len(text.split()) >= 3:
                out.append(r)
                continue
        if r['id'] in flip_ids:
            out.append(r)
    return out


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

        # 保存判定（含静态分析的危险用途标记）；批次失败保持未决的
        # 候选 ai_keep=None，归一为 -1 入库（0 会被当成"判不翻"）。
        # keep=1 的行一并决定应用路径（strings 表 / _() 包裹）。
        # stage 按判定来源区分：精审判决带证据引用（粗筛票决没有）——
        # verdict_log 的审计链据此能精确还原"谁判的"
        from usage_rules import decide_apply_path
        for r in undecided:
            c = r['candidate']
            danger = bool(getattr(c, 'static_danger', False))
            keep_db = -1 if c.ai_keep is None else (1 if c.ai_keep else 0)
            apply_path = decide_apply_path(c) if keep_db == 1 else ''
            evidence = getattr(c, 'ai_evidence', '')
            await loop.run_in_executor(
                None, self.db.update_embedded_ai,
                r['id'], keep_db, c.ai_reason, danger,
                evidence, apply_path,
                'refine' if evidence else 'coarse')
            r['ai_keep'] = keep_db
            r['ai_reason'] = c.ai_reason
            r['ai_danger'] = 1 if danger else 0
            r['ai_evidence'] = getattr(c, 'ai_evidence', '')
            r['apply_path'] = apply_path

    async def recheck_grayzone(self, rows, on_progress=None, cancel_event=None):
        """灰区复核（取代"全部重判"）：只对判定与证据可能冲突的行做
        锚定精审；其余行一律不重送（判定冻结——级联判定已产出终判，
        复核的目的是纠错，不是重掷骰子）。

        锚定精审：上轮判定与理由随条目给出，除非工具找到矛盾的新证据
        （必须引用 file:line）否则维持原判——翻转即纠错，都有据可查。
        """
        from ai_screener import AIScreener, ScreeningCancelled
        from source_tree import SourceTree
        from usage_rules import UsageAnalyzer, decide_apply_path
        loop = asyncio.get_event_loop()

        flip_ids = set(await loop.run_in_executor(
            None, self.db.get_embedded_flip_history))
        targets = _grayzone_rows(rows, flip_ids)
        if not targets:
            self.logger.info('灰区复核: 没有判定与证据冲突的行', panel='ui')
            return

        before = {r['id']: r['ai_keep'] for r in targets}
        cands = [r['candidate'] for r in targets]
        # 静态分析重跑（源码可能已变），与精审工具共用同一源码缓存
        tree = SourceTree(str(self.base_dir))
        await loop.run_in_executor(
            None,
            UsageAnalyzer(str(self.base_dir), files=tree.as_dict()).classify_all,
            cands)

        screener = AIScreener(self.translator, str(self.base_dir), self.logger,
                              source_tree=tree)
        anchored = {i: {'keep': bool(r['ai_keep']), 'reason': r['ai_reason']}
                    for i, r in enumerate(targets)}
        progress = {'phase': '复核', 'done': 0, 'total': len(cands),
                    'finished': False}

        def _run():
            try:
                screener._refine_screen(cands, progress, cancel_event,
                                        anchored=anchored)
            finally:
                progress['finished'] = True

        task = loop.run_in_executor(None, _run)
        try:
            while not progress.get('finished'):
                if cancel_event is not None and cancel_event.is_set():
                    task.add_done_callback(_swallow_task_error)
                    raise ScreeningCancelled('灰区复核已取消')
                if on_progress:
                    on_progress(progress['phase'], progress['done'],
                                progress['total'])
                await asyncio.sleep(0.5)
            await task
        finally:
            screener.close()

        for r in targets:
            c = r['candidate']
            danger = bool(getattr(c, 'static_danger', False))
            keep_db = -1 if c.ai_keep is None else (1 if c.ai_keep else 0)
            apply_path = decide_apply_path(c) if keep_db == 1 else ''
            await loop.run_in_executor(
                None, self.db.update_embedded_ai,
                r['id'], keep_db, c.ai_reason, danger,
                getattr(c, 'ai_evidence', ''), apply_path, 'recheck')
            r['ai_keep'] = keep_db
            r['ai_reason'] = c.ai_reason
            r['ai_danger'] = 1 if danger else 0
            r['ai_evidence'] = getattr(c, 'ai_evidence', '')
            r['apply_path'] = apply_path

        # 翻转透明化：锚定复核的翻转都附新证据（evidence 引用），仍走人工确认
        flips = [r for r in targets
                 if r['ai_keep'] in (0, 1) and r['ai_keep'] != before[r['id']]]
        self.logger.info(
            f'灰区复核完成: 复核 {len(targets)} 条，维持 '
            f'{len(targets) - len(flips)}，翻转 {len(flips)}'
            '（翻转均有引用证据，仍需人工确认）', panel='ui')

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

        from usage_rules import decide_apply_path
        for r in rows:
            c = r['candidate']
            danger = bool(getattr(c, 'static_danger', False))
            keep_db = -1 if c.ai_keep is None else (1 if c.ai_keep else 0)
            apply_path = decide_apply_path(c) if keep_db == 1 else ''
            await loop.run_in_executor(
                None, self.db.update_embedded_ai,
                r['id'], keep_db, c.ai_reason, danger,
                getattr(c, 'ai_evidence', ''), apply_path, 'refine')
            r['ai_keep'] = keep_db
            r['ai_reason'] = c.ai_reason
            r['ai_danger'] = 1 if danger else 0
            r['ai_evidence'] = getattr(c, 'ai_evidence', '')
            r['apply_path'] = apply_path

    # ---- 单句精判 ----

    async def refine_single(self, row) -> tuple:
        """单句 AI 精判（agentic，带工具），写库并返回
        (keep, reason, danger, evidence, apply_path)

        先跑静态用途分析，把出现点证据/危险用途注入精审输入，
        AI 不用从头盲查（refine_by_id 重建的候选没有 static_* 字段）。
        danger 作为独立标记入库/返回，不拼进 reason。
        """
        from ai_screener import AIScreener
        from source_tree import SourceTree
        from usage_rules import UsageAnalyzer, decide_apply_path
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
        keep, reason, evidence, _apply = verdicts[0]
        danger = bool(getattr(c, 'static_danger', False))
        apply_path = decide_apply_path(c) if keep else ''
        c.ai_keep, c.ai_reason = keep, reason
        row['ai_keep'] = 1 if keep else 0
        row['ai_reason'] = reason
        row['ai_danger'] = 1 if danger else 0
        row['ai_evidence'] = evidence
        row['apply_path'] = apply_path
        await loop.run_in_executor(
            None, self.db.update_embedded_ai, row['id'], keep, reason, danger,
            evidence, apply_path, 'refine_single')
        return keep, reason, danger, evidence, apply_path

    async def refine_by_id(self, row_id: int) -> tuple:
        """按 db 行 id 单句精判（API 用：从 db 重建最小 Candidate）
        返回 (keep, reason, danger, evidence, apply_path)"""
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

    # ---- 步骤 4~6: 应用选择（统一标记；_() 包裹推迟到导出副本）----

    async def apply_selection(self, rows, chosen_rows, stage=None,
                              cancel_event=None) -> dict:
        """应用人工选择。stage(text) 报告阶段。

        源码只读：两种路径统一标记 + 全量重生成 zz 翻译表
        （table/wrap 行的译文条目都在此合成）。wrap 行的实际 `_()`
        包裹由 GameExporter 在导出副本上应用（regen 已按全部 marked
        行写入条目），本步骤不需要 SDK，且对游戏源码零写入。
        Returns: {'tabled': int, 'wrapped': int, 'skipped': int, 'inserted': int}
        （wrapped 恒 0：包裹不再发生于此，保留键仅为兼容调用方/前端）
        """
        from services.embedded_table import regen_embedded_table
        loop = asyncio.get_event_loop()

        def _stage(text):
            if stage:
                stage(text)

        if not chosen_rows:
            return {'tabled': 0, 'wrapped': 0, 'skipped': 0, 'inserted': 0}

        chosen_ids = {r['id'] for r in chosen_rows}

        # ---- 统一标记 + strings 表重生成（table/wrap 同路） ----
        _stage('正在写入内嵌翻译表...')
        await loop.run_in_executor(
            None, self.db.set_embedded_status,
            list(chosen_ids), 'marked')
        inserted = await loop.run_in_executor(
            None, regen_embedded_table, self.db, self.game_root,
            self.logger)
        self.logger.info(
            f'内嵌翻译表: {len(chosen_rows)} 条已写入 zz_embedded.rpy',
            panel='ui')

        # 未选行照旧标 skipped
        await loop.run_in_executor(
            None, self.db.set_embedded_status,
            [r['id'] for r in rows if r['id'] not in chosen_ids], 'skipped')

        self.logger.info(
            f'内嵌文本提取完成: 入表 {len(chosen_rows)}, '
            f'新增 {inserted} 条', panel='ui')
        return {'tabled': len(chosen_rows), 'wrapped': 0,
                'skipped': 0, 'inserted': inserted}
