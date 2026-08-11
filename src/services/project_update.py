"""项目版本更新服务（增量翻译）

游戏发布新版本后，就地升级项目：替换游戏文件、重建 SDK 模板，
把旧译文按原文内容匹配继承到新条目，只有新增/改动的文本需要翻译。

匹配键是 original_text 内容（与导出时 game_export.build_translation_dict
的查找键一致），不使用行号（新版会漂移）与 translate 块 identifier。

微改句子三档处理：
- 剥离 Ren'Py 标签后相等 → 自动继承
- difflib ratio ≥ 0.95 → 自动继承并写入 update_review(status='applied') 留痕
- 0.85 ≤ ratio < 0.95 → 不预填，写入 update_review(status='pending') 待人工

旧译文中新版已不存在的 → obsolete_translations 表（可查阅，不丢数据）。

失败/取消自动回滚：db 从备份恢复，game.bak-<ts> 改回 game/。

用法:
    updater = ProjectUpdater(project_manager, logger, get_sdk_path)
    result = await updater.update(
        name, new_game_dir,
        progress=lambda pct, text: ...,
        confirm_official_chinese=async def (file_count) -> bool,
    )
返回 {'success': True, 'carried': n, 'edited': n, 'new': n,
      'still_untranslated': n, 'obsolete': n, 'review': n,
      'embedded_rewrapped': n, 'embedded_lost': n} 或 {'cancelled': True}。
"""
import asyncio
import difflib
import json as _json
import re
import shutil
from collections import deque
from datetime import datetime
from functools import partial
from pathlib import Path

from logger import TranslationLogger
from project_manager import ProjectManager
from services.game_pipeline import (
    copy_with_progress, locate_ui_hints, refresh_characters,
    resolve_sdk_or_raise, unpack_and_decompile,
)
from services.project_creation import (
    cleanup_conflicts, detect_official_chinese, generate_tl_templates,
    parse_tl_dir, remove_official_chinese,
)

_FUZZY_AUTO = 0.95    # ≥ 自动继承（留痕）
_FUZZY_REVIEW = 0.85  # ≥ 进复核表（不预填）

_TAG_RE = re.compile(r'\{[^}]*\}')


def _strip_tags(text: str) -> str:
    """剥离 Ren'Py 文本标签（{i}{/i}{b} 等），用于微改判定"""
    return _TAG_RE.sub('', text or '')


# ========== 继承合并（纯函数，便于脚本验证） ==========

class _OldPool:
    """旧条目索引：双键（character+原文 / 仅原文）+ 消费标记。

    deque 按 (file_path, line_number, id) 序，命中即 popleft——
    重复原文按出现顺序对位。
    """

    def __init__(self, rows: list[dict], with_character: bool):
        self.by_char_text: dict[tuple, deque] = {}
        self.by_text: dict[str, deque] = {}
        self.consumed: set[int] = set()
        self.rows = {r['id']: r for r in rows}
        self.with_character = with_character
        for r in rows:
            self.by_text.setdefault(r['original_text'], deque()).append(r['id'])
            if with_character:
                key = (r.get('character', ''), r['original_text'])
                self.by_char_text.setdefault(key, deque()).append(r['id'])

    def _pop(self, dq: deque) -> dict | None:
        while dq:
            rid = dq.popleft()
            if rid not in self.consumed:
                self.consumed.add(rid)
                return self.rows[rid]
        return None

    def take(self, character: str, original: str) -> dict | None:
        """精确匹配：先 (character, 原文)，再仅原文"""
        if self.with_character:
            row = self._pop(self.by_char_text.get((character, original), deque()))
            if row is not None:
                return row
        return self._pop(self.by_text.get(original, deque()))

    def consume(self, row_id: int):
        self.consumed.add(row_id)

    def unconsumed_translated(self, character: str = None) -> list[dict]:
        """未消费的已翻译旧行（模糊匹配池）；传 character 时只看同角色"""
        out = []
        for rid, r in self.rows.items():
            if rid in self.consumed or not r.get('is_translated'):
                continue
            if character is not None and r.get('character', '') != character:
                continue
            out.append(r)
        return out


def _fuzzy_match(new_text: str, pool: list[dict],
                 fuzzy_auto: float, fuzzy_review: float) -> tuple:
    """在未消费的旧行池里找最佳匹配。

    Returns: (action, old_row, ratio)
        action: 'auto'（tag 归一化相等或 ratio≥fuzzy_auto）
                'review'（fuzzy_review ≤ ratio < fuzzy_auto）
                None（低于阈值）
    """
    new_stripped = _strip_tags(new_text)
    best, best_ratio = None, 0.0
    for old in pool:
        old_text = old['original_text']
        # 档1：剥离标签后相等（{i} 标签类改动，确定性命中）
        if _strip_tags(old_text) == new_stripped:
            return ('auto', old, 1.0)
        sm = difflib.SequenceMatcher(None, new_text, old_text)
        if sm.real_quick_ratio() < fuzzy_review:
            continue
        if sm.quick_ratio() < fuzzy_review:
            continue
        ratio = sm.ratio()
        if ratio > best_ratio:
            best, best_ratio = old, ratio
    if best is None:
        return (None, None, 0.0)
    if best_ratio >= fuzzy_auto:
        return ('auto', best, best_ratio)
    return ('review', best, best_ratio)


def merge_translations(old_dialogues: list[dict], old_ui: list[dict],
                       new_tl_result: dict,
                       fuzzy_auto: float = _FUZZY_AUTO,
                       fuzzy_review: float = _FUZZY_REVIEW) -> dict:
    """把旧译文合并进新解析结果。

    Returns: {
        'dialogues': [...],   # 新条目（已带入继承译文），供 replace_dialogues
        'ui_texts': [...],
        'review': [...],      # target_kind/target_index/new_original/old_original/
                              # old_translation/ratio/status
        'obsolete': [...],    # kind/file_path/character/original_text/translated_text
        'stats': {'carried', 'edited', 'new', 'still_untranslated',
                  'obsolete', 'review'},
    }
    """
    dlg_pool = _OldPool(old_dialogues, with_character=True)
    ui_pool = _OldPool(old_ui, with_character=False)

    stats = {'carried': 0, 'edited': 0, 'new': 0,
             'still_untranslated': 0, 'obsolete': 0, 'review': 0}
    review = []

    def _merge_kind(new_items, pool, kind):
        out = []
        for idx, item in enumerate(new_items):
            item = dict(item)
            character = item.get('character', '') if kind == 'dialogue' else ''
            original = item.get('original_text', '')
            old = pool.take(character, original)
            if old is not None:
                # 精确命中：继承译文（旧行未翻译则仍为待译，不算新增）
                item['translated_text'] = old.get('translated_text', '')
                item['is_translated'] = bool(old.get('is_translated'))
                if item['is_translated']:
                    stats['carried'] += 1
                else:
                    stats['still_untranslated'] += 1
                out.append(item)
                continue

            # 模糊：对话先在同角色池找，找不到再全池
            if kind == 'dialogue' and character:
                pool_rows = pool.unconsumed_translated(character)
                action, old, ratio = _fuzzy_match(
                    original, pool_rows, fuzzy_auto, fuzzy_review)
                if action is None:
                    action, old, ratio = _fuzzy_match(
                        original, pool.unconsumed_translated(),
                        fuzzy_auto, fuzzy_review)
            else:
                action, old, ratio = _fuzzy_match(
                    original, pool.unconsumed_translated(),
                    fuzzy_auto, fuzzy_review)

            if action == 'auto':
                item['translated_text'] = old.get('translated_text', '')
                item['is_translated'] = True
                pool.consume(old['id'])
                stats['edited'] += 1
                if ratio < 1.0:
                    # tag 归一化相等是确定性命中，不留痕；difflib 自动继承需可审计
                    review.append({
                        'target_kind': kind, 'target_index': idx,
                        'new_original': original,
                        'old_original': old['original_text'],
                        'old_translation': old.get('translated_text', ''),
                        'ratio': ratio, 'status': 'applied',
                    })
            elif action == 'review':
                pool.consume(old['id'])
                stats['new'] += 1
                stats['review'] += 1
                review.append({
                    'target_kind': kind, 'target_index': idx,
                    'new_original': original,
                    'old_original': old['original_text'],
                    'old_translation': old.get('translated_text', ''),
                    'ratio': ratio, 'status': 'pending',
                })
            else:
                stats['new'] += 1
            out.append(item)
        return out

    new_dialogues = _merge_kind(
        new_tl_result.get('dialogues', []), dlg_pool, 'dialogue')
    new_ui = _merge_kind(new_tl_result.get('ui_texts', []), ui_pool, 'ui')

    # 失效：未消费的已翻译旧行
    obsolete = []
    for pool, kind in ((dlg_pool, 'dialogue'), (ui_pool, 'ui')):
        for r in pool.unconsumed_translated():
            obsolete.append({
                'kind': kind,
                'file_path': r.get('file_path', ''),
                'character': r.get('character', ''),
                'original_text': r.get('original_text', ''),
                'translated_text': r.get('translated_text', ''),
            })
    stats['obsolete'] = len(obsolete)

    return {
        'dialogues': new_dialogues, 'ui_texts': new_ui,
        'review': review, 'obsolete': obsolete, 'stats': stats,
    }


# ========== 内嵌文本重标记 ==========

def rewrap_marked_embedded(db, game_work_dir: Path, game_bak_dir: Path,
                           logger: TranslationLogger) -> tuple[int, int]:
    """把旧版本已标记的内嵌 _() 在新源码上按内容重新定位并重新包裹。

    行号在版本间会漂移，匹配键为 (rel_file, raw)；同一 (rel_file, raw)
    出现多次时按 (line, col_start) 序位对位。必须在 SDK 重新生成模板之前
    调用（否则 _() 字符串进不了新模板）。

    同步函数（调用方放 executor）。返回 (重标记数, 丢失数)。
    丢失的候选重置为 pending（保留 AI 判定，后续扫描可重新处理）。
    """
    from embedded_strings import apply_wrapping, find_candidates

    marked = db.get_marked_embedded()
    if not marked:
        return (0, 0)

    new_cands = find_candidates(str(game_work_dir))
    new_by_key: dict[tuple, list] = {}
    for c in new_cands:
        new_by_key.setdefault((c.rel_file, c.raw), []).append(c)

    old_by_key = None  # 需要序位匹配时才扫旧树
    to_wrap = []       # (db_row, Candidate)
    lost_ids = []
    for row in marked:
        key = (row['rel_file'], row['raw'])
        matches = new_by_key.get(key, [])
        if len(matches) == 1:
            to_wrap.append((row, matches[0]))
        elif len(matches) > 1:
            if old_by_key is None:
                old_by_key = {}
                for c in find_candidates(str(game_bak_dir)):
                    old_by_key.setdefault((c.rel_file, c.raw), []).append(c)
            rank = 0
            for c in old_by_key.get(key, []):
                if (c.line, c.col_start) < (row['line'], row['col_start']):
                    rank += 1
            to_wrap.append((row, matches[min(rank, len(matches) - 1)]))
        else:
            lost_ids.append(row['id'])

    rewrapped = 0
    if to_wrap:
        # apply_wrapping 自带位置校验（源码变了就跳过），
        # 坐标来自对同一新树的全新扫描，正常不会跳过
        wrapped, skipped = apply_wrapping([c for _, c in to_wrap])
        if skipped:
            logger.warning(
                f'内嵌重标记有 {skipped} 条位置校验未通过（对应文本需重新标记）',
                panel='projects')
        rewrapped = wrapped
        for row, c in to_wrap:
            db.update_embedded_position(row['id'], c.line, c.col_start)
    if lost_ids:
        # merge_embedded_candidates 会跳过 status='marked' 的行——
        # 丢失的必须重置回 pending，否则永远不再出现
        db.set_embedded_status(lost_ids, 'pending')
        logger.warning(
            f'{len(lost_ids)} 条内嵌标记在新版中未找到（已重置为待处理）',
            panel='projects')
    return (rewrapped, len(lost_ids))


# ========== 项目更新编排 ==========

class ProjectUpdater:
    """就地升级项目到新游戏版本（异步编排；阻塞操作全部 run_in_executor）"""

    def __init__(self, project_manager: ProjectManager,
                 logger: TranslationLogger, get_sdk_path=None):
        self.project_manager = project_manager
        self.logger = logger
        self.get_sdk_path = get_sdk_path

    async def update(self, name: str, new_game_dir: str,
                     progress, confirm_official_chinese=None,
                     cancel_event=None) -> dict:
        """执行更新。progress(pct, text) 同步回调。

        任何异常（含取消）都会回滚到更新前状态后重抛。
        用户拒绝官中处理时回滚并返回 {'cancelled': True}。
        cancel_event: 可选 threading.Event，传给 SDK 子进程以便中止。
        """
        loop = asyncio.get_event_loop()
        _rie = partial(loop.run_in_executor, None)

        project_dir = self.project_manager.project_dir(name)
        game_work_dir = project_dir / 'game'
        db_file = project_dir / 'project.db'
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        game_bak = project_dir / f'game.bak-{ts}'
        backups_dir = project_dir / 'backups'
        backup_db = backups_dir / f'project-{ts}.db'

        db = None

        def _rename_with_retry(src: Path, dst: Path, attempts: int = 8,
                               interval: float = 2.0):
            """目录改名带重试：杀软/索引/资源管理器的瞬时句柄通常几秒内释放。
            重试耗尽后原样抛出最后一次异常"""
            import time
            for i in range(attempts):
                try:
                    src.rename(dst)
                    return
                except OSError:
                    if i == attempts - 1:
                        raise
                    time.sleep(interval)

        def _restore():
            """回滚：db 从备份恢复，game.bak 改回 game/

            铁律：只有 game.bak 存在（备份改名成功过）才动 game/ ——
            否则 game/ 从未被更新流程触碰，绝不能删。
            半成品 game/ 先改名挪走（瞬时）再改回备份——先 rmtree 几万文件
            再 rename 在 Windows 上容易被杀软/索引占用打断（WinError 5）。
            """
            nonlocal db
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
                db = None
            if backup_db.exists():
                shutil.copy2(backup_db, db_file)
            # 备份在 checkpoint(TRUNCATE) 之后制作，无 wal 副本；
            # 当前 wal 里是更新期间的写入，必须删掉，否则 SQLite 会把
            # 它重放到恢复后的库文件上
            for suffix in ('-wal', '-shm'):
                Path(str(db_file) + suffix).unlink(missing_ok=True)
            if game_bak.exists():
                fail_dir = project_dir / f'game.failed-{ts}'
                if game_work_dir.exists():
                    try:
                        _rename_with_retry(game_work_dir, fail_dir)
                    except OSError:
                        shutil.rmtree(game_work_dir, ignore_errors=True)
                _rename_with_retry(game_bak, game_work_dir)
                # 失败残留尽力清理（清不掉不阻断回滚）
                if fail_dir.exists():
                    shutil.rmtree(fail_dir, ignore_errors=True)

        try:
            # 步骤1: 快照旧数据（WAL 先并入库文件，备份才完整）
            progress(0.02, '正在备份当前项目...')
            db = await _rie(self.project_manager.open_project, name)
            if db is None:
                raise RuntimeError('项目数据库不存在')
            await _rie(db.checkpoint_wal)
            old_dialogues = await _rie(db.get_all_dialogues)
            old_ui = await _rie(db.get_all_ui_texts)

            # 步骤2: 备份 db（保留最近 3 份）+ game/ 改名为 game.bak-<ts>
            def _backup():
                backups_dir.mkdir(exist_ok=True)
                shutil.copy2(db_file, backup_db)
                old = sorted(backups_dir.glob('project-*.db'),
                             key=lambda p: p.stat().st_mtime)
                for f in old[:-3]:
                    f.unlink(missing_ok=True)
                if game_work_dir.exists():
                    try:
                        _rename_with_retry(game_work_dir, game_bak)
                    except PermissionError:
                        raise RuntimeError(
                            '游戏目录被其他程序占用，无法备份。\n'
                            '请关闭正在运行的游戏、资源管理器/编辑器中'
                            '打开的项目目录窗口后重试') from None
            await _rie(_backup)

            # 步骤3: 复制新版本游戏文件
            progress(0.05, '正在复制新版本游戏文件...')
            await copy_with_progress(_rie, new_game_dir, game_work_dir,
                                     progress, 0.05, 0.30,
                                     label='正在复制新版本游戏文件...')
            progress(0.30, '新版本文件复制完成')

            # 步骤4: 解包 rpa & 反编译 rpyc（参数与建项一致）
            rel_files = await unpack_and_decompile(
                _rie, game_work_dir, db, self.logger, progress)

            # 步骤5: 新版本官中重检
            official_tl = await _rie(detect_official_chinese, game_work_dir)
            if official_tl:
                progress(0.50, '检测到新版本自带中文翻译...')
                if confirm_official_chinese is None:
                    raise RuntimeError('检测到官方中文翻译但未提供确认回调')
                use_sdk = await confirm_official_chinese(official_tl)
                if not use_sdk:
                    progress(0.50, '已取消更新')
                    await _rie(_restore)
                    self.logger.info(
                        '用户取消更新：新版本自带中文翻译', panel='projects')
                    return {'cancelled': True}
                await _rie(remove_official_chinese, game_work_dir, self.logger)
                self.logger.info(
                    f'已删除新版本自带中文翻译（{official_tl} 个文件）',
                    panel='projects')

            # 步骤6: 内嵌 _() 重标记（必须在 SDK 生成模板之前）
            progress(0.52, '正在重新标记内嵌文本...')
            rewrapped, lost = await _rie(
                rewrap_marked_embedded, db, game_work_dir, game_bak, self.logger)
            if rewrapped or lost:
                self.logger.info(
                    f'内嵌文本重标记: {rewrapped} 成功, {lost} 丢失', panel='projects')

            # 步骤7: SDK 重新生成模板
            sdk_path = await resolve_sdk_or_raise(
                _rie, self.get_sdk_path, game_work_dir, '更新')
            progress(0.55, '正在使用 SDK 重新生成翻译文件...')
            await generate_tl_templates(
                sdk_path, game_work_dir, rel_files, db,
                self.logger, progress, _rie,
                cancel_event=cancel_event)
            progress(0.60, 'SDK 模板就绪')

            # 步骤8: 解析新模板
            tl_dir = game_work_dir / 'game' / 'tl' / 'chinese'
            tl_exists = await _rie(tl_dir.exists)
            if not tl_exists:
                raise RuntimeError('SDK 未生成翻译模板目录')
            progress(0.60, '正在解析新翻译模板...')
            tl_result = await _rie(parse_tl_dir, tl_dir, game_work_dir,
                                   self.logger)

            # 步骤9: 继承合并 + 重建表
            progress(0.70, '正在继承已有翻译...')
            merged = await _rie(
                merge_translations, old_dialogues, old_ui, tl_result)
            stats = merged['stats']

            def _save_merged():
                dlg_ids = db.replace_dialogues(merged['dialogues'])
                ui_ids = db.replace_ui_texts(merged['ui_texts'])
                # target_index → 新条目 id
                id_map = {'dialogue': dlg_ids, 'ui': ui_ids}
                review_rows = []
                for r in merged['review']:
                    review_rows.append({
                        **{k: v for k, v in r.items() if k != 'target_index'},
                        'target_id': id_map[r['target_kind']][r['target_index']],
                    })
                db.save_update_review(review_rows)
                db.save_obsolete(merged['obsolete'])
            await _rie(_save_merged)

            # 步骤10: 角色重解析（insert_characters 按变量名合并，
            # 保留 cn_name/profile；新版删除的角色保留不删）
            progress(0.85, '正在更新角色信息...')
            await _rie(refresh_characters, game_work_dir, db,
                       merged['dialogues'], True)

            # 步骤11: UI 上下文定位
            progress(0.90, '正在定位字符串上下文...')
            await locate_ui_hints(
                _rie, game_work_dir, db, self.logger, '更新')

            # 步骤12: 收尾
            progress(0.95, '正在清理...')
            await _rie(cleanup_conflicts, game_work_dir, self.logger)

            report = {
                'success': True,
                'carried': stats['carried'],
                'edited': stats['edited'],
                'new': stats['new'],
                'still_untranslated': stats['still_untranslated'],
                'obsolete': stats['obsolete'],
                'review': stats['review'],
                'embedded_rewrapped': rewrapped,
                'embedded_lost': lost,
                'updated_at': datetime.now().isoformat(),
            }

            def _finalize():
                db.set_meta('updated_at', report['updated_at'])
                db.set_meta('last_update_report', _json.dumps(
                    report, ensure_ascii=False))
                db.close()
            await _rie(_finalize)
            db = None

            progress(1.0, f'✅ 更新完成！继承 {stats["carried"]} 条，'
                          f'微改继承 {stats["edited"]} 条，'
                          f'新增 {stats["new"]} 条待翻译')

        except Exception:
            # 失败/取消统一回滚（JobCancelled 也走这里；_restore 内部已关库）
            await _rie(_restore)
            raise

        # 成功之后才删除旧版备份：放在 try 外，任何后置步骤异常都不会
        # 再触发回滚（否则 db 已回滚而旧版游戏文件已删，两头落空）
        try:
            await _rie(shutil.rmtree, game_bak, True)
        except OSError as e:
            self.logger.warning(f'旧版备份目录清理失败（不影响更新）: {e}',
                                panel='projects')
        return report
