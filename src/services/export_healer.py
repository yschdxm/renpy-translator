"""导出后编译校验与自愈

校验：对导出目录跑 SDK translate（等价于全量解析所有 .rpy，含 tl 模板），
解析报错中的 File/line 定位出错点。

源码只读化后内嵌 wrap 只存在于导出副本（每轮导出重新应用），工作副本
从未被包裹——内嵌修复不需要拆源码/SDK 重生成，标 skipped + 重导出即可。

错误分类（报错文件位置 + 内嵌标记库对照，均为机械判定）：
- game/tl/ 下：定位出错条目原文，对照内嵌标记库——
  · 条目原文命中已标记候选 → 内嵌标记问题，命中行标 skipped
    （重生成 zz 表）→ 重新导出
  · 未命中 → 译文问题：AI 修译文（fix/blank）→ 更新库 → 重填 tl → 再校验
- game/ 其他 → 内嵌标记问题：按报错位置 ±3 行找已标记候选，同上
- 其他位置   → 不可修复，响亮失败

每轮至少修复一处才继续，最多 MAX_ROUNDS 轮。
"""
import asyncio
import json
import re
import threading
from pathlib import Path

from database import ProjectDatabase
from logger import TranslationLogger

# Ren'Py 报错中的文件/行号（traceback 与 parser 错误都含此格式）
_ERR_RE = re.compile(r'File "([^"]+\.rpy)", line (\d+)[,:]?\s*(.*)')

# tl 模板中的条目定位
_OLD_RE = re.compile(r'^\s*old\s+"(.*)"\s*$')
_COMMENT_STR_RE = re.compile(r'^\s*#\s+(?:\w+\s+)?"(.*)"\s*$')
_PATH_COMMENT_RE = re.compile(r'^\s*#\s*(game/|[\w./-]+\.rpym?:\d+)')

_FIX_PROMPT = """你是 Ren'Py 本地化专家。导出的翻译文件编译报错，需要修复译文。

错误: {err_file} 第 {err_line} 行: {err_msg}
出错条目原文: {original}
当前译文: {current}
出错位置周边代码（带行号）:
{context}

判断原因并输出 JSON（不要输出任何其他文字）：
- 译文本身可修正 → {{"action": "fix", "text": "修正后的译文"}}
- 无法安全修正 → {{"action": "blank"}}（该条将回退显示原文，不影响游戏运行）

要求：修正译文必须忠实原文；不得含未闭合的 [ ]、{{ }} 等插值/格式标记；
换行、引号、% 由程序自动转义，无需处理。"""


class ExportHealer:
    """导出后校验与自愈（异步，在导出任务线程中调用）"""

    MAX_ROUNDS = 3      # 校验-修复循环上限
    LINE_TOLERANCE = 3  # 内嵌候选行号与报错行号的容差

    def __init__(self, db: ProjectDatabase, translator, project_dir: str,
                 sdk_path: str, logger: TranslationLogger, exporter):
        self.db = db
        self.translator = translator
        self.project_dir = Path(project_dir)
        self.game_root = self.project_dir / 'game'
        self.sdk_path = sdk_path
        self.logger = logger
        self.exporter = exporter  # GameExporter（重填 tl 用）
        self._cancel_check = None
        # 内部取消事件：包装 cancel_check 时置位；_sdk_cancel_event 为实际
        # 传给 SDK 子进程的事件（外部传入时指向外部事件）
        self._cancel_event = threading.Event()
        self._sdk_cancel_event = self._cancel_event

    def _check_cancel(self):
        if self._cancel_check is not None:
            self._cancel_check()

    # ========== 总入口 ==========

    async def validate_and_heal(self, export_dir: Path, log,
                                cancel_check=None, cancel_event=None) -> str:
        """校验导出目录并按需自愈，返回 'ok' | 'reexport' | 'fail'

        cancel_check: 可调用对象，抛异常即中止（任务取消用）。
        cancel_event: 可选 threading.Event（任务取消信号），传入后 SDK
            子进程在取消时被立即终止；不传则退化为包装 cancel_check
            （只有检查点触发后才置位，在飞的 SDK 调用不能及时中止）。
        """
        if cancel_event is not None:
            # 外部事件（如 job.cancel_event），直接使用，绝不清除
            self._sdk_cancel_event = cancel_event
        else:
            self._cancel_event.clear()
            self._sdk_cancel_event = self._cancel_event
            if cancel_check is not None:
                # 包装一层：取消异常触发时同步置位 _cancel_event，
                # 让正在跑的 SDK 子进程（_validate/_regen）随之终止
                raw_check = cancel_check

                def cancel_check():
                    try:
                        raw_check()
                    except Exception:
                        self._cancel_event.set()
                        raise
        self._cancel_check = cancel_check
        loop = asyncio.get_event_loop()
        for round_no in range(1, self.MAX_ROUNDS + 1):
            self._check_cancel()
            log(f'编译校验（第 {round_no}/{self.MAX_ROUNDS} 轮）...')
            errors = await loop.run_in_executor(
                None, self._validate, export_dir)
            if not errors:
                # SDK 校验会重新生成模板：源码中任何 _()（导出时包的 wrap、
                # 游戏原生 _()、游戏补丁引入的 _()）此刻都被提取成 old 条目，
                # 与 zz 表/其他模板里的同名条目构成"重复 old"（Ren'Py 加载
                # 硬错误）。必须在打包前清扫——这是最后一道也是唯一一道
                # 能覆盖"校验时才生成的模板"的防线。
                from services.game_export import dedupe_string_tables
                await loop.run_in_executor(
                    None, dedupe_string_tables, export_dir, log)
                log('编译校验通过')
                return 'ok'
            log(f'发现 {len(errors)} 处报错，开始自动修复:')
            action = await self._heal_round(errors, export_dir, log)
            if action == 'reexport':
                return 'reexport'
            if action == 'fail':
                return 'fail'
        log('达到修复轮数上限仍未通过校验')
        return 'fail'

    # ========== 校验 ==========

    def _validate(self, export_dir: Path) -> list:
        """SDK translate 校验（会重新生成模板但保留已填译文），返回错误列表"""
        from sdk_manager import SDKManager
        sdk = SDKManager()
        sdk.sdk_path = Path(self.sdk_path)
        result = sdk.generate_translations(
            str(export_dir), 'chinese', cancel_event=self._sdk_cancel_event)
        if result.get('cancelled'):
            # 子进程因取消被终止：有 cancel_check 时抛取消异常，否则响亮失败
            self._check_cancel()
            raise RuntimeError('编译校验已取消')
        if result['success']:
            return []

        output = result.get('output', '') or result.get('message', '')
        errors = []
        seen = set()
        for m in _ERR_RE.finditer(output):
            rel = m.group(1).replace('\\', '/')
            key = (rel, int(m.group(2)))
            if key in seen:
                continue
            seen.add(key)
            errors.append({'file': rel, 'line': key[1],
                           'msg': m.group(3).strip()[:200]})
        if not errors:
            # 无法定位的错误：原文上抛由调用方响亮呈现
            raise RuntimeError(
                '编译校验失败且无法解析出错位置: ' + output[-500:])
        return errors

    # ========== 单轮修复 ==========

    async def _heal_round(self, errors: list, export_dir: Path, log) -> str:
        """返回 'fixed'（译文已修，可再校验）| 'reexport' | 'fail'"""
        loop = asyncio.get_event_loop()
        tl_errors, game_errors, other_errors = [], [], []
        for e in errors:
            f = e['file']
            if '/tl/' in f or f.startswith('game/tl/'):
                tl_errors.append(e)
            elif f.startswith('game/'):
                game_errors.append(e)
            else:
                other_errors.append(e)

        if other_errors:
            for e in other_errors:
                log(f"  无法修复的报错（非翻译文件）: {e['file']}:{e['line']} {e['msg']}")
            return 'fail'

        # 保留的反编译文件（语言按钮/内嵌译文载体）编译报错：不是内嵌
        # 标记问题——按文件放弃（记入 dropped_decompiled_files）并重新导出，
        # 该文件回退原始 rpyc，只丢它承载的切换按钮/内嵌译文
        kept_meta = await loop.run_in_executor(
            None, self.db.get_meta, 'kept_decompiled_files')
        kept = set(json.loads(kept_meta or '[]'))
        broken = sorted({e['file'] for e in game_errors} & kept)
        if broken:
            dropped_meta = await loop.run_in_executor(
                None, self.db.get_meta, 'dropped_decompiled_files')
            dropped = sorted(set(json.loads(dropped_meta or '[]'))
                             | set(broken))
            await loop.run_in_executor(
                None, self.db.set_meta, 'dropped_decompiled_files',
                json.dumps(dropped))
            for f in broken:
                log(f'  保留的反编译文件 {f} 编译报错，放弃该文件'
                    '（回退原始 rpyc）并重新导出')
            return 'reexport'

        # tl 报错不等于译文问题：内嵌标记的字符串导出后也会在 tl 中生成
        # old/new 条目，需对照内嵌标记库区分根因
        embedded_rows = []
        trans_errors = []
        if tl_errors:
            marked = await loop.run_in_executor(
                None, self.db.get_marked_embedded)
            for e in tl_errors:
                entry = self._locate_entry(export_dir / e['file'], e['line'])
                hit = self._match_embedded(entry[1] if entry else None, marked)
                if hit:
                    embedded_rows.append(hit)
                else:
                    trans_errors.append(e)

        if game_errors or embedded_rows:
            # 内嵌标记问题：命中行标 skipped + 重生成 zz 表 → 重新导出
            # （优先于译文修复，因为重导出会重建整个 tl，
            # 译文修复等重新导出后再做）
            ok = await self._heal_embedded(game_errors, embedded_rows, log)
            return 'reexport' if ok else 'fail'

        if trans_errors:
            if not self.translator:
                log('  译文报错需要 AI 修复，但未配置模型')
                return 'fail'
            fixed = await self._heal_translations(trans_errors, export_dir, log)
            return 'fixed' if fixed else 'fail'

        return 'fail'

    @staticmethod
    def _match_embedded(original, marked: list):
        """tl 条目原文是否对应某个已标记的内嵌候选（考虑转义差异）"""
        if not original:
            return None
        variants = {original, original.replace('\\n', '\n')}
        for r in marked:
            if r['text'] in variants:
                return r
        return None

    # ========== 译文修复（tl 模板内的错误） ==========

    async def _heal_translations(self, errors: list, export_dir: Path,
                                 log) -> bool:
        """逐条 AI 修复，全部处理（fix/blank 都算修复动作）返回 True"""
        loop = asyncio.get_event_loop()
        fixed_any = False
        for e in errors:
            self._check_cancel()
            tl_file = export_dir / e['file']
            entry = self._locate_entry(tl_file, e['line'])
            if not entry:
                log(f"  {e['file']}:{e['line']} 无法定位出错条目，跳过")
                continue
            kind, original = entry
            row = await loop.run_in_executor(
                None,
                (self.db.find_dialogue_by_original if kind == 'dialogue'
                 else self.db.find_ui_text_by_original),
                original)
            if not row or not row.get('translated_text'):
                log(f"  {e['file']}:{e['line']} 库中未找到对应译文，跳过")
                continue

            action, new_text = await loop.run_in_executor(
                None, self._ai_fix, e, original, row['translated_text'],
                tl_file)
            update = (self.db.update_dialogue if kind == 'dialogue'
                      else self.db.update_ui_text)
            if action == 'fix' and new_text:
                await loop.run_in_executor(None, update, row['id'], new_text)
                log(f"  修复译文: {original[:30]!r} → {new_text[:30]!r}")
                fixed_any = True
            elif action == 'blank':
                await loop.run_in_executor(None, update, row['id'], '')
                log(f"  放弃该条翻译（回退原文）: {original[:40]!r}")
                fixed_any = True
            else:
                log(f"  AI 无法给出修复方案: {original[:40]!r}")

        if fixed_any:
            # 重新填充导出的 tl（填充按原文键查找，幂等）
            log('重新填充导出的翻译文件...')
            translation_dict = await loop.run_in_executor(
                None, self.exporter.build_translation_dict)
            tl_dir = export_dir / 'game' / 'tl' / 'chinese'
            await loop.run_in_executor(
                None, self.exporter._fill_dialogue, tl_dir,
                translation_dict, None)
            await loop.run_in_executor(
                None, self.exporter._fill_strings, tl_dir,
                translation_dict, None)
        return fixed_any

    def _locate_entry(self, tl_file: Path, err_line: int):
        """从报错行向上定位出错条目，返回 ('dialogue'|'ui', 原文) 或 None"""
        try:
            lines = tl_file.read_text(encoding='utf-8', errors='ignore').split('\n')
        except OSError:
            return None
        start = min(err_line - 1, len(lines) - 1)
        for i in range(start, max(start - 15, -1), -1):
            line = lines[i]
            m = _OLD_RE.match(line)
            if m:
                return 'ui', m.group(1).replace('\\"', '"')
            if _PATH_COMMENT_RE.match(line):
                continue
            m = _COMMENT_STR_RE.match(line)
            if m:
                return 'dialogue', m.group(1).replace('\\"', '"')
        return None

    def _ai_fix(self, error: dict, original: str, current: str,
                tl_file: Path) -> tuple:
        """AI 审查并修复译文，返回 ('fix', 新译文) | ('blank', '') | ('', '')"""
        context = ''
        try:
            lines = tl_file.read_text(encoding='utf-8', errors='ignore').split('\n')
            s = max(0, error['line'] - 6)
            e = min(len(lines), error['line'] + 5)
            context = '\n'.join(f'{i + 1:>5}│{lines[i]}' for i in range(s, e))
        except OSError:
            pass
        prompt = _FIX_PROMPT.format(
            err_file=error['file'], err_line=error['line'],
            err_msg=error['msg'], original=original,
            current=current, context=context)
        try:
            result = self.translator.analyze_text(
                prompt, max_tokens=max(self.translator.config.max_tokens, 2000))
            m = re.search(r'\{.*\}', result, re.S)
            if not m:
                return '', ''
            parsed = json.loads(m.group(0))
            action = parsed.get('action', '')
            return action, str(parsed.get('text', '')).strip()
        except Exception as e:
            self.logger.error(f'AI 修复译文失败: {e}', panel='export')
            return '', ''

    # ========== 内嵌标记修复（导出副本编译错误） ==========

    async def _heal_embedded(self, errors: list, direct_rows: list, log) -> bool:
        """命中行标 skipped 并重生成 zz 表，触发重导出

        源码只读原则：工作副本从未被包裹，无需拆包；wrap 包裹只存在于
        导出副本（每轮导出重新生成），把出问题的行标 skipped 后重导出
        即不再包裹该处。修复回路从"改源码+SDK 重生成+重导出"变为
        "改 DB+重导出"，无 SDK 往返。

        errors: game 源码报错（按位置 ±3 行匹配候选）
        direct_rows: tl 报错中按条目原文已命中的候选行（直接使用）
        """
        loop = asyncio.get_event_loop()

        marked = await loop.run_in_executor(None, self.db.get_marked_embedded)
        rows = list(direct_rows)
        seen_ids = {r['id'] for r in rows}
        for e in errors:
            # 报错路径相对导出目录（game/xxx.rpy），候选 rel_file 相对 game/game/
            rel = e['file']
            if rel.startswith('game/'):
                rel = rel[len('game/'):]
            hits = [r for r in marked
                    if r['rel_file'] == rel
                    and abs(r['line'] - e['line']) <= self.LINE_TOLERANCE]
            if not hits:
                log(f"  {e['file']}:{e['line']} 附近找不到已标记的内嵌候选，无法自动修复")
                return False
            for r in hits:
                if r['id'] not in seen_ids:
                    seen_ids.add(r['id'])
                    rows.append(r)

        for r in rows:
            log(f"  放弃该处翻译（标 skipped）: {r['text'][:40]!r} "
                f"({r['rel_file']}:{r['line']})")

        # 标 skipped + 重生成 zz 表（条目移除）；wrap 位置坐标保持，
        # skipped 行不再出现在 get_marked_embedded，导出时不再包裹
        ids = [r['id'] for r in rows]
        await loop.run_in_executor(
            None, self.db.set_embedded_status, ids, 'skipped')
        from services.embedded_table import regen_embedded_table
        await loop.run_in_executor(
            None, regen_embedded_table, self.db, self.game_root, self.logger)
        return True
