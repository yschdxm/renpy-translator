"""AI 预筛：粗筛（带代码上下文 + 自评置信度）+ agentic 精审（tool calling 循环）

阶段 1（粗筛）：分批发送候选及出处 ±6 行代码，一次调用判定 keep/drop
并自评 confident；不置信的进入阶段 2。
阶段 2（精审）：对 uncertain 候选分批并发处理，AI 可通过 read_code /
search_code 工具自由查看源码（多轮循环，上限 10 轮），最后 submit_verdicts
提交判决（含理由）。

失败即抛错（粗筛批次失败、精审批次失败、判决未覆盖全部候选），
不做启发式降级——自用工具，失败要大声暴露。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_EXCLUDE_DIRS = {'renpy', 'lib', 'saves', 'cache', 'tl', 'output',
                 'audio', 'sound', 'images', 'image', 'fonts', 'font',
                 'video', 'movies'}

# agentic 精审工具 schema
_TOOLS = [
    {"type": "function", "function": {
        "name": "read_code",
        "description": "读取游戏源码文件的一段（按行区间，带行号返回）。用于查看字符串候选周边的完整上下文（函数定义、数据结构、屏幕布局等）。",
        "parameters": {"type": "object", "properties": {
            "file": {"type": "string", "description": "相对游戏 game/ 目录的文件路径，如 definitions/phone.rpy"},
            "start_line": {"type": "integer", "description": "起始行（1-based）"},
            "end_line": {"type": "integer", "description": "结束行（含），单次最多 200 行"}},
         "required": ["file", "start_line", "end_line"]}}},
    {"type": "function", "function": {
        "name": "search_code",
        "description": "在游戏全部源码中做子串搜索，返回匹配行（文件:行号:内容）。用于查清一个字符串在哪里被使用、是否会被显示给玩家。注意：是纯子串匹配（短词会命中更长的字符串），结果超过 30 处时会显示总数并截断，此时应用更长或带引号的 query 重搜。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索子串（建议用候选的完整原文，短词容易误命中）"}},
         "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "submit_verdicts",
        "description": "提交本批候选字符串的最终判定",
        "parameters": {"type": "object", "properties": {
            "verdicts": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "keep": {"type": "boolean", "description": "true=玩家可见应当翻译，false=噪音不翻译"},
                "reason": {"type": "string", "description": "一句话中文理由"}},
             "required": ["id", "keep", "reason"]}}},
         "required": ["verdicts"]}}},
]

_COARSE_PROMPT = """你是游戏本地化专家。以下是从 Ren'Py 游戏源码扫描出的字符串候选（JSON 数组），每条含：
- text: 候选原文
- hint/kind: 出处类型（screen=屏幕语言, python=脚本数据）
- code: 出处周边的代码片段（带行号）
- warning（可选）: 静态分析发现该字符串有非显示用途，判 keep 有破坏逻辑的风险

请逐条判断它是否是【玩家可见、应当翻译的显示文本】，并自评置信度：
- keep=true：界面文字、按钮、提示、叙事/消息/帖子内容等玩家直接读到的文本
- keep=false：内部标识符、键名、技术串、格式模板、代码用字符串、资源名称、纯数字或时间标记
- confident=true：你能确定判定；confident=false：代码片段不足以判断（该条将进入带源码查看工具的精审）
- reason：一句话中文说明判定依据（keep/drop 都要写）

候选：
{items_json}

只输出 JSON 数组，不要输出任何其他文字：[{{"id": 1, "keep": true, "confident": true, "reason": "界面按钮文字"}}, ...]"""

_REFINE_SYSTEM = """你在审查 Ren'Py 游戏源码中提取的字符串候选，判断哪些是【玩家可见、应当翻译的文本】。

规则：
- keep=true：界面文字、按钮、提示、叙事/消息/帖子内容等玩家直接读到的文本
- keep=false：内部标识符、字典键名、技术串、格式模板、变量占位、代码用字符串、资源名称、纯数字或时间标记
- 候选若带 warning 字段，说明静态分析发现它另有比较/键名/资源用途，判 keep 前必须用工具核实这些用途的位置和方式
- 候选若带 static_analysis 字段，那是静态分析找到的出现点证据，可作参考，但有疑问仍应以 read_code/search_code 实际查到的代码为准
- 拿不准时必须用 read_code 查看候选周边的完整代码，或用 search_code 搜索该字符串在源码中的使用位置，不要凭猜测判定
- 每条候选都给出了 file 和 line（字符串所在位置），可以据此查看任意范围的代码
- 同一轮可以并行调用多个工具，请尽量批量查询，目标是在 6 轮以内完成本批判断
- 完成所有判断后，调用 submit_verdicts 一次性提交本批全部判定（reason 用一句话中文说明依据）"""


class ScreeningCancelled(Exception):
    """AI 预筛/精审被取消（cancel_event 置位）"""


class AIScreener:
    """内嵌文本 AI 预筛器"""

    MAX_ROUNDS = 10       # 精审单批最大 tool 循环轮数
    REFINE_CONCURRENCY = 3
    COARSE_BATCH = 40
    REFINE_BATCH = 15

    def __init__(self, translator, game_root: str, logger=None):
        self.translator = translator
        self.game_root = Path(game_root)
        # 调用方传入的是 find_candidates 的 rel_file 基准（可能是 game/game），
        # 只有传入项目根时才需要再下一层
        nested = self.game_root / 'game'
        self.game_sub = nested if nested.is_dir() else self.game_root
        self.logger = logger
        self._pool = ThreadPoolExecutor(max_workers=self.REFINE_CONCURRENCY)
        self._cancel_event = None  # screen_all/_refine_screen 调用时设置

    def close(self):
        """关闭精审线程池（不等待在飞任务；screener 为一次性使用）"""
        self._pool.shutdown(wait=False)

    def _check_cancel(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise ScreeningCancelled('AI 预筛已取消')

    def _log(self, msg):
        if self.logger:
            self.logger.info(msg, panel='ui')

    # ========== 总入口 ==========

    def screen_all(self, candidates: list, progress: dict, cancel_event=None):
        """规则分流 → 粗筛 → 精审全流程（同步，在线程池中调用）

        progress: 共享进度字典 {phase, done, total, finished}
        cancel_event: 可选 threading.Event，批次间检查，置位则抛
            ScreeningCancelled（已写入的判定保留在候选对象上，由调用方
            决定是否入库）
        结果写回候选的 ai_keep / ai_confident / ai_reason。
        """
        self._cancel_event = cancel_event
        try:
            targets = self._static_screen(candidates, progress)
            if targets:
                self._check_cancel()
                self._coarse_screen(targets, progress)
                self._check_cancel()
                self._refine_screen(targets, progress)
            # 危险用途标记（static_danger）由 pipeline 随判定一起入库，
            # 前端以独立 ⚠ 徽标展示，不拼进 reason（避免与 AI 核实结论矛盾）
        finally:
            progress['finished'] = True

    # ========== 阶段 0：静态规则分流 ==========

    def _static_screen(self, candidates: list, progress: dict):
        """规则分流：确定 keep/drop 的直接写回，返回需交 AI 的候选"""
        from usage_rules import KEEP, DROP, RULE_REASON_PREFIX, UsageAnalyzer
        progress.update(phase='规则分流', done=0, total=len(candidates))
        analyzer = UsageAnalyzer(str(self.game_root))
        analyzer.classify_all(candidates)
        progress['done'] = len(candidates)

        targets = []
        kept = dropped = 0
        for c in candidates:
            if c.static_verdict == KEEP:
                c.ai_keep, c.ai_confident = True, True
                c.ai_reason = RULE_REASON_PREFIX + c.static_reason
                kept += 1
            elif c.static_verdict == DROP:
                c.ai_keep, c.ai_confident = False, True
                c.ai_reason = RULE_REASON_PREFIX + c.static_reason
                dropped += 1
            else:
                targets.append(c)
        self._log(f'规则分流: 确定保留 {kept} / 确定丢弃 {dropped} / '
                  f'交 AI 判定 {len(targets)}')
        return targets

    # ========== 阶段 1：粗筛 ==========

    def _snippet(self, c, ctx: int = 6) -> str:
        """候选前后 ctx 行代码（带行号）"""
        try:
            lines = Path(c.file).read_text(encoding='utf-8', errors='ignore').split('\n')
        except OSError:
            return ''
        start = max(0, c.line - 1 - ctx)
        end = min(len(lines), c.line + ctx)
        return '\n'.join(f'{i + 1:>5}│{lines[i]}' for i in range(start, end))

    def _coarse_screen(self, candidates: list, progress: dict):
        items = []
        for i, c in enumerate(candidates):
            item = {
                'id': i, 'text': c.text, 'hint': c.hint, 'kind': c.kind,
                'code': self._snippet(c),
            }
            if getattr(c, 'static_danger', False):
                item['warning'] = ('该字符串在代码中另有比较/键名/资源引用等'
                                   '非显示用途（' + c.static_reason + '）；'
                                   '若判 keep 并包 _() 翻译，这些用途会失效')
            items.append(item)

        progress.update(phase='粗筛', done=0, total=len(items))
        for start in range(0, len(items), self.COARSE_BATCH):
            self._check_cancel()
            batch = items[start:start + self.COARSE_BATCH]
            # 失败直接上抛，不降级
            verdicts = self._coarse_batch(batch)
            for it in batch:
                v = verdicts.get(it['id'])
                if v is not None:
                    keep, confident, reason = v
                    candidates[it['id']].ai_keep = keep
                    candidates[it['id']].ai_confident = confident
                    candidates[it['id']].ai_reason = reason
                else:
                    # 粗筛响应遗漏的条目按未决处理（进入精审判定，非降级）
                    candidates[it['id']].ai_confident = False
            progress['done'] = min(start + self.COARSE_BATCH, len(items))

    def _coarse_batch(self, batch: list) -> dict:
        """单批粗筛，返回 {id: (keep, confident, reason)}"""
        items_json = json.dumps(batch, ensure_ascii=False)
        result = self.translator.analyze_text(
            _COARSE_PROMPT.format(items_json=items_json),
            max_tokens=max(self.translator.config.max_tokens, 8000))
        m = re.search(r'\[.*\]', result, re.S)
        if not m:
            raise ValueError(f'粗筛返回无法解析: {result[:100]}')
        parsed = json.loads(m.group(0))
        verdicts = {}
        for entry in parsed:
            if isinstance(entry, dict) and 'id' in entry:
                verdicts[entry['id']] = (bool(entry.get('keep', True)),
                                         bool(entry.get('confident', True)),
                                         str(entry.get('reason', '')))
        if not verdicts:
            raise ValueError('粗筛返回为空')
        return verdicts

    # ========== 阶段 2：agentic 精审 ==========

    def _refine_screen(self, candidates: list, progress: dict,
                       cancel_event=None):
        if cancel_event is not None:
            self._cancel_event = cancel_event
        uncertain = [(i, c) for i, c in enumerate(candidates)
                     if not c.ai_confident]
        if not uncertain:
            return

        progress.update(phase='精审', done=0, total=len(uncertain))
        batches = [uncertain[i:i + self.REFINE_BATCH]
                   for i in range(0, len(uncertain), self.REFINE_BATCH)]

        # 失败直接上抛，不降级
        # as_completed 按完成顺序推进进度（map 按提交顺序，慢批次会卡住进度条）
        futures = {self._pool.submit(self._refine_batch, b, progress): b
                   for b in batches}
        for fut in as_completed(futures):
            self._check_cancel()
            verdicts = fut.result()
            for idx, (keep, reason) in verdicts.items():
                candidates[idx].ai_keep = keep
                candidates[idx].ai_reason = reason
                candidates[idx].ai_confident = True
            progress['done'] = min(progress['done'] + len(futures[fut]),
                                   len(uncertain))

    def _refine_batch(self, batch: list, progress: dict = None) -> dict:
        """单批 agentic 精审：多轮 tool 循环，返回 {候选索引: (keep, reason)}

        progress 非空时每轮更新 phase 文本（并发批次相互覆盖无碍，仅作展示）。
        """
        id_map = {n: idx for n, (idx, _) in enumerate(batch)}
        items = []
        for n, (_, c) in enumerate(batch):
            item = {'id': n, 'text': c.text, 'hint': c.hint, 'kind': c.kind,
                    'file': c.rel_file, 'line': c.line}
            if getattr(c, 'static_reason', ''):
                # 静态用途分析的证据（出现点位置），供 AI 参考而非复述
                item['static_analysis'] = c.static_reason
            if getattr(c, 'static_danger', False):
                item['warning'] = ('该字符串另有非显示用途（' + c.static_reason +
                                   '），判 keep 并包 _() 可能破坏逻辑，'
                                   '请用工具核实后再判')
            items.append(item)

        messages = [
            {"role": "system", "content": _REFINE_SYSTEM},
            {"role": "user", "content":
                '本批候选（JSON 数组）：\n' + json.dumps(items, ensure_ascii=False)},
        ]

        for round_no in range(1, self.MAX_ROUNDS + 1):
            self._check_cancel()
            if progress is not None:
                progress['phase'] = f'精审·第 {round_no} 轮'
            message = self.translator._call_api(
                messages=messages,
                temperature=self.translator.config.temperature,
                max_tokens=self.translator.config.max_tokens,
                tools=_TOOLS, tool_choice="auto",
                return_message=True, task_type='analysis',
            )

            if message.tool_calls:
                # submit_verdicts 优先：提取判决直接返回
                for tc in message.tool_calls:
                    if tc.function.name == 'submit_verdicts':
                        return self._parse_verdict_args(
                            tc.function.arguments, id_map)

                self._log(f'精审第 {round_no} 轮: ' +
                          ', '.join(tc.function.name for tc in message.tool_calls))
                messages.append(message)
                for tc in message.tool_calls:
                    result = self._execute_tool(tc.function.name,
                                                tc.function.arguments)
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": result,
                    })
                continue

            # 无 tool call：兜底从文本解析，否则提示其提交
            if message.content:
                verdicts = self._parse_verdicts_text(message.content, id_map)
                if verdicts:
                    return verdicts
                messages.append(message)
                messages.append({"role": "user", "content":
                                 '请调用 submit_verdicts 工具提交判定结果。'})

        # 达到轮数上限：强制要求用纯文本输出判决，再做最后一轮解析
        self._log(f'精审批次达到 {self.MAX_ROUNDS} 轮上限，要求立即提交')
        messages.append({"role": "user", "content":
            '轮数已达上限。请立即基于已有信息输出最终判决 JSON 数组'
            '（[{"id":N,"keep":true/false,"reason":"..."}]），不要再调用工具。'})
        message = self.translator._call_api(
            messages=messages,
            temperature=self.translator.config.temperature,
            max_tokens=max(self.translator.config.max_tokens, 8000),
            return_message=True, task_type='analysis',
        )
        if message.content:
            verdicts = self._parse_verdicts_text(message.content, id_map)
            if verdicts:
                return verdicts
        raise RuntimeError(f'精审批次 {self.MAX_ROUNDS} 轮后仍未能获得判决')

    def _parse_verdict_args(self, arguments: str, id_map: dict) -> dict:
        """解析 submit_verdicts 工具参数（必须覆盖整批，否则报错）"""
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as e:
            raise ValueError(f'submit_verdicts 参数解析失败: {e}') from e
        verdicts = {}
        for entry in args.get('verdicts', []):
            if isinstance(entry, dict) and entry.get('id') in id_map:
                idx = id_map[entry['id']]
                verdicts[idx] = (bool(entry.get('keep', True)),
                                 str(entry.get('reason', '')))
        missing = set(id_map.values()) - set(verdicts)
        if missing:
            raise ValueError(f'精审判决未覆盖全部候选（缺 {len(missing)} 条）')
        return verdicts

    def _parse_verdicts_text(self, content: str, id_map: dict) -> dict:
        """兜底：从文本内容解析判决 JSON（必须覆盖整批）"""
        m = re.search(r'\[.*\]|\{.*\}', content, re.S)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        entries = parsed if isinstance(parsed, list) else parsed.get('verdicts', [])
        verdicts = {}
        for entry in entries:
            if isinstance(entry, dict) and entry.get('id') in id_map:
                idx = id_map[entry['id']]
                verdicts[idx] = (bool(entry.get('keep', True)),
                                 str(entry.get('reason', '')))
        if set(id_map.values()) - set(verdicts):
            return None
        return verdicts or None

    # ========== 工具执行（只读） ==========

    def _resolve(self, rel_file: str):
        """把相对路径解析到 game/ 内，越界返回 None"""
        try:
            p = (self.game_sub / rel_file).resolve()
            p.relative_to(self.game_sub.resolve())
            return p
        except (ValueError, OSError):
            return None

    def _execute_tool(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return f'参数解析失败: {e}'

        if name == 'read_code':
            return self._tool_read_code(args)
        if name == 'search_code':
            return self._tool_search_code(args)
        return f'未知工具: {name}'

    def _tool_read_code(self, args: dict) -> str:
        rel = str(args.get('file', ''))
        start = int(args.get('start_line', 1))
        end = int(args.get('end_line', start))
        if end < start:
            start, end = end, start
        end = min(end, start + 199)  # 单次最多 200 行

        path = self._resolve(rel)
        if not path or not path.exists():
            return f'文件不存在: {rel}'
        try:
            lines = path.read_text(encoding='utf-8', errors='ignore').split('\n')
        except OSError as e:
            return f'读取失败: {e}'
        start = max(1, start)
        end = min(end, len(lines))
        if start > end:
            return f'行区间越界（文件共 {len(lines)} 行）'
        out = [f'{i + 1:>5}│{lines[i]}' for i in range(start - 1, end)]
        return '\n'.join(out)

    def _tool_search_code(self, args: dict) -> str:
        query = str(args.get('query', ''))
        if not query:
            return 'query 为空'
        MAX_SHOW = 30
        matches = []
        total = 0
        hit_files = set()
        for rpy in sorted(self.game_sub.rglob('*.rpy')):
            if _EXCLUDE_DIRS & set(rpy.parts):
                continue
            try:
                content = rpy.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            rel = rpy.relative_to(self.game_sub).as_posix()
            for i, line in enumerate(content.split('\n'), 1):
                if query in line:
                    total += 1
                    hit_files.add(rel)
                    if len(matches) < MAX_SHOW:
                        matches.append(f'{rel}:{i}: {line.strip()[:120]}')
        if not total:
            return '（无匹配）'
        header = f'共 {total} 处匹配（{len(hit_files)} 个文件）'
        if total > MAX_SHOW:
            header += (f'，以下只显示前 {MAX_SHOW} 处——判定前请注意结果不完整，'
                       '可用更长/带引号的 query 缩小范围')
        return header + '\n' + '\n'.join(matches)
