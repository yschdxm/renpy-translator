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
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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
        "description": "在游戏全部源码中做子串搜索，返回匹配行及前后各 2 行上下文（文件:行号:内容）。用于查清一个字符串在哪里被使用、是否会被显示给玩家。注意：是纯子串匹配（短词会命中更长的字符串），结果超过 20 处时会显示总数并截断，此时应用更长或带引号的 query 重搜。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索子串（建议用候选的完整原文，短词容易误命中）"}},
         "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "trace_symbol",
        "description": "追踪一个符号（变量/screen/函数名）的定义点与全部引用点，每个引用点带用途分类（display=显示/nondisplay=逻辑/容器传递/函数参数等）。用于回答『这个字符串经过变量传递最终流向了哪里』——比反复 search_code 拼数据流更直接。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "符号名（标识符），如 mood_text、stats_screen"}},
         "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "submit_verdicts",
        "description": "提交本批候选字符串的最终判定",
        "parameters": {"type": "object", "properties": {
            "verdicts": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "keep": {"type": "boolean", "description": "true=玩家可见应当翻译，false=噪音不翻译"},
                "reason": {"type": "string", "description": "一句话中文理由"},
                "evidence": {"type": "string", "description": "判定依据的代码位置（file:line，可空格分隔多个），必须来自工具实际查到的代码"},
                "apply": {"type": "string", "enum": ["table", "wrap"], "description": "可选。仅当你有明确证据表明该字符串会被拼接/格式化进更大文本（翻译表整串匹配会失效）时填 wrap；其余情况不要填"}},
             "required": ["id", "keep", "reason", "evidence"]}}},
         "required": ["verdicts"]}}},
]

# 粗筛提示词三变体（任务等价、措辞不同）：票间去相关性来自输入差异
# （确定性洗牌 + 变体），而非温度——判定任务全部 temperature=0
_COARSE_PROMPT_A = """你是游戏本地化专家。以下是从 Ren'Py 游戏源码扫描出的字符串候选（JSON 数组），每条含：
- text: 候选原文
- hint/kind: 出处类型（screen=屏幕语言, python=脚本数据）
- evidence: 静态分析给出的出现点用途全景（哪些位置是显示用途、哪些是比较/键名/资源用途）——这是确定性扫描的结果，可信
- code: 出处周边的代码片段（带行号）
- warning（可选）: 静态分析发现该字符串另有非显示用途——不影响翻译（选中后默认写入翻译表，逻辑代码仍用原文值），仅提示你需要更谨慎地判断它是否会被玩家读到

请逐条判断它是否是【玩家可见、应当翻译的显示文本】：
- keep=true：界面文字、按钮、提示、叙事/消息/帖子内容等玩家直接读到的文本
- keep=false：内部标识符、键名、技术串、格式模板、代码用字符串、资源名称、纯数字或时间标记
- reason：一句话中文说明判定依据

【kind=fstring 的候选是 f-string 模板】：{{0}}/{{1}} 是 Python 插值占位，
[x] 是 Ren'Py 插值。导出时程序会机械替换插值并**单独翻译插值内容**——
判断时不要把模板当字面内容，应看插值的内容是否会在屏幕上显示：
插值是人名/标题/属性名/描述等显示文本 → keep=true（即使模板本身
没有可译文字）；插值是键名/内部值 → keep=false。

候选：
{items_json}

只输出 JSON 数组，不要输出任何其他文字：[{{"id": 1, "keep": true, "reason": "界面按钮文字"}}, ...]"""

_COARSE_PROMPT_B = """你在为一款 Ren'Py 游戏做汉化前置审查。下面 JSON 数组里的字符串都没有包翻译标记，需要判断哪些该补标。每条字段：
- text 候选原文；hint/kind 出处（screen 界面 / python 脚本）
- evidence 静态扫描的出现点全景：这串在源码里还被用在哪、是什么用途（显示=展示给玩家；比较/键名/索引=逻辑用途）——机械扫描结果，可以直接采信
- code 出现位置前后的代码（带行号）
- warning（可选）这串另有逻辑用途——不影响翻译（逻辑代码仍用原文值），只是提醒你要更谨慎判断它是否会被玩家读到

判定标准：
- 玩家能在游戏画面里读到的（界面、按钮、通知、消息、叙事）→ keep=true
- 程序内部使用的（标识符、键、路径、格式串、资源名、纯数字时间）→ keep=false
- 每条给出一句中文 reason

【kind=fstring 的是 f-string 模板】：{{0}}/{{1}} 是 Python 插值占位、[x] 是
Ren'Py 插值，程序导出时会机械替换并**单独翻译插值内容**——所以模板本身
没有可译文字不代表 keep=false：插值内容会在屏幕上显示（人名/标题/属性/
描述）→ keep=true；插值是键名/内部值 → keep=false。

候选：
{items_json}

只输出 JSON：[{{"id": 1, "keep": true, "reason": "..."}}, ...]，不要输出其他内容"""

_COARSE_PROMPT_C = """任务：Ren'Py 游戏内嵌字符串的翻译价值判定。

输入：JSON 数组，每条是一个从源码提取的字符串，含 text（原文）、hint/kind（出处）、
evidence（静态分析的出现点用途统计，确定性结果）、code（周边代码）。
部分条目带 warning（另有非显示用途的提示——不影响翻译，逻辑代码仍用原文值，仅提醒你更谨慎判断）。

输出：对每条给出 keep（true=玩家可见应翻译 / false=内部串不翻译）与一句中文 reason。
判断核心问题只有一个：玩家玩游戏时会在屏幕上读到这段文字吗？
- 会读到（界面文字/按钮/提示/通知/消息/叙事内容）→ true
- 不会（键名/标识符/路径/格式模板/资源名/代码标记）→ false
- kind=fstring 的是 f-string 模板：{{0}} 是 Python 插值占位、[x] 是 Ren'Py
  插值，程序会机械替换并单独翻译插值内容——模板无可译文字 ≠ false；
  插值内容会在屏幕显示（人名/标题/属性/描述）→ true，内部值 → false

候选：
{items_json}

仅输出 JSON 数组：[{{"id": 1, "keep": false, "reason": "..."}}, ...]"""

_COARSE_PROMPT_VARIANTS = (
    _COARSE_PROMPT_A, _COARSE_PROMPT_B, _COARSE_PROMPT_C)

_REFINE_SYSTEM = """你在审查 Ren'Py 游戏源码中提取的字符串候选，判断哪些是【玩家可见、应当翻译的文本】。

规则：
- keep=true：界面文字、按钮、提示、叙事/消息/帖子内容等玩家直接读到的文本
- keep=false：内部标识符、字典键名、技术串、格式模板、变量占位、代码用字符串、资源名称、纯数字或时间标记
- kind=fstring 的候选是 f-string 模板：{0}/{1} 是 Python 插值占位、[x] 是
  Ren'Py 插值，程序导出时机械替换并**单独翻译插值内容**——模板本身没有
  可译文字不代表 keep=false；插值内容会在屏幕显示（人名/标题/属性/描述）
  → keep=true，插值是键名/内部值 → keep=false
- 候选若带 evidence 字段，那是静态分析的出现点用途全景（确定性扫描，可直接采信）：多数条目据此即可判定，submit_verdicts 的 evidence 可引用这些出现点位置
- 候选若带 warning 字段，说明它另有比较/键名/资源用途——这不妨碍翻译（选中后默认写入翻译表，逻辑代码仍用原文值），你要核实的只是它是否真的会被玩家读到
- 需要用工具的情形：evidence 与代码片段矛盾、信息不足、或 keep/drop 关键抉择证据不够。trace_symbol 看变量流向（定义点+引用点分类），search_code 查字符串使用位置（带上下文），read_code 读完整代码段
- 【必须批量查询】每一轮都要并行发起多个工具调用（一次查多个字符串/多个文件段），不允许一轮只查一处——逐条单独查询会在轮数上限前耗光查询预算。例外：只剩最后一个疑点时可以单查
- 完成所有判断后，调用 submit_verdicts 一次性提交本批全部判定：reason 用一句话中文说明依据，evidence 填判定依据的代码位置（file:line，可引用 evidence 出现点或工具新查到的，多个用空格分隔）"""

# 灰区复核锚定段（anchored 模式追加）：复核目的是纠错，不是重猜
_ANCHOR_SECTION = """

【复核模式】本批每条候选都带 prior_verdict / prior_reason 字段，是上轮判定的结论与理由。
复核的目的是纠错，不是重新猜测：
- 逐条对照 evidence 与上轮理由——不矛盾的直接维持原判，无需调用任何工具（多数条目应如此）
- 只有当你有具体理由怀疑上轮判错时，才用工具取证；翻转必须在 evidence 中引用 file:line
- 查不到矛盾证据就等于维持原判——不要为了"完成核实"而反复搜索，目标是在 2 轮以内提交"""


class ScreeningCancelled(Exception):
    """AI 预筛/精审被取消（cancel_event 置位）"""


class AIScreener:
    """内嵌文本 AI 预筛器"""

    # 精审单批最大 tool 循环轮数。按实测校准：部分模型不批查
    # （每轮只有 1-2 个工具调用），16 轮 × ~2 次 ≈ 30 次查询，
    # 摊 6 条候选每条 ~5 次——与"4 轮 × 6 并行"的设计预算等价
    MAX_ROUNDS = 16
    REFINE_CONCURRENCY = 3
    COARSE_BATCH = 40
    # 精审批量必须小：每条判决都要求引用证据，批大了模型在轮数上限内
    # 查不完，被强制交卷的半成品判决会把双跑不一致率推高（实测 15 条/批
    # 时几乎每批触发 10 轮上限、42% 不一致）
    REFINE_BATCH = 6

    def __init__(self, translator, game_root: str, logger=None,
                 source_tree=None):
        from embedded_strings import resolve_source_root
        from source_tree import SourceTree
        self.translator = translator
        self.game_root = Path(game_root)
        # 调用方传入的是 find_candidates 的 rel_file 基准（可能是 game/game），
        # 只有传入项目根时才需要再下一层
        self.game_sub = resolve_source_root(self.game_root)
        self.logger = logger
        # 源码缓存：静态分流/粗筛片段/精审工具共用一棵树（一次实例即一次调用）
        self._tree = source_tree or SourceTree(str(self.game_sub))
        self._pool = ThreadPoolExecutor(max_workers=self.REFINE_CONCURRENCY)
        self._cancel_event = None  # screen_all/_refine_screen 调用时设置
        self._analyzer = None      # UsageAnalyzer 惰性自建（trace_symbol 工具）

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
        # 复用源码缓存，不再全树重读；留存实例供精审 trace_symbol 工具复用
        self._analyzer = UsageAnalyzer(str(self.game_root),
                                       files=self._tree.as_dict())
        self._analyzer.classify_all(candidates)
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

    # ========== 阶段 1：粗筛（多票级联） ==========

    def _snippet(self, c, ctx: int = 6) -> str:
        """候选前后 ctx 行代码（带行号）"""
        lines = self._tree.lines(c.rel_file)
        if not lines:
            # rel_file 不在源码树内（如 game/ 之外的文件）：回退直读绝对路径
            try:
                lines = Path(c.file).read_text(
                    encoding='utf-8', errors='ignore').split('\n')
            except OSError:
                return ''
        start = max(0, c.line - 1 - ctx)
        end = min(len(lines), c.line + ctx)
        return '\n'.join(f'{i + 1:>5}│{lines[i]}' for i in range(start, end))

    # True 时固定三票；默认自适应——前两票一致即定案，只给分裂条目投第三票
    # （结局与固定三票等价：第三票只在分裂时才有意义），省 1/3 调用
    COARSE_ALWAYS_3 = False

    _KIND_CN = {'display': '显示', 'nondisplay': '非显示', 'format': '格式化',
                'fragment': '拼接', 'assign': '赋值', 'unknown': '未分类'}

    def _evidence(self, c) -> dict:
        """从静态分析的出现点证据蒸馏判定输入：全景统计 + 分类样本

        静态能算的不让 LLM 猜：出现点计数与用途分类是确定性扫描的结果，
        直接给出结论比让模型从裸代码里数出现点更准、更省 token。
        """
        sites = getattr(c, 'static_sites', None) or []
        if not sites:
            return {}
        counts = {}
        samples = {}
        for s in sites:
            k = s['kind']
            counts[k] = counts.get(k, 0) + 1
            samples.setdefault(k, []).append(s)
        parts = [f'{self._KIND_CN.get(k, k)} {n}' for k, n in counts.items()]
        panorama = f"出现点 {len(sites)} 处：" + ' / '.join(parts)
        if getattr(c, 'static_sites_truncated', False):
            panorama += '（截断，还有更多未列出）'
        examples = []
        for k, group in samples.items():
            for s in group[:3]:
                examples.append({'site': s['site'],
                                 'role': self._KIND_CN.get(k, k),
                                 'line': s['text']})
        return {'panorama': panorama, 'examples': examples[:8]}

    def _coarse_screen(self, candidates: list, progress: dict):
        items = []
        for i, c in enumerate(candidates):
            item = {
                'id': i, 'text': c.text, 'hint': c.hint, 'kind': c.kind,
                'code': self._snippet(c),
            }
            ev = self._evidence(c)
            if ev:
                item['evidence'] = ev
            if getattr(c, 'static_danger', False):
                item['warning'] = (
                    '该字符串另有比较/键名/资源引用等非显示用途（'
                    + c.static_reason + '）。这些用途不受翻译影响：选中的'
                    '字符串默认写入翻译表（不改源码），逻辑代码仍使用原文值，'
                    '只有显示给玩家时才替换为译文')
            items.append(item)

        votes = {i: [] for i in range(len(items))}  # id -> [(keep, reason)]
        progress.update(phase='粗筛', done=0, total=len(items))

        def run_vote(vote_idx: int, vote_items: list):
            """对给定条目集合投一票（分批；批失败收尾统一重试，仍败该票留空）"""
            failed = []
            # 按票分阶段显示：总量=候选数（票间去相关靠洗牌+变体，
            # 不是多筛了一遍——避免 x/2N 被读成"候选变多了"）
            progress.update(phase=f'粗筛·第 {vote_idx + 1} 票', done=0,
                            total=len(vote_items))
            for start in range(0, len(vote_items), self.COARSE_BATCH):
                self._check_cancel()
                batch = vote_items[start:start + self.COARSE_BATCH]
                try:
                    verdicts = self._coarse_vote(batch, vote_idx)
                except ScreeningCancelled:
                    raise
                except Exception:  # noqa: BLE001 - 批次失败留待收尾重试
                    failed.append(batch)
                    progress['done'] = min(progress['done'] + len(batch),
                                           len(vote_items))
                    continue
                for it in batch:
                    v = verdicts.get(it['id'])
                    if v is not None:
                        votes[it['id']].append(v)
                progress['done'] = min(progress['done'] + len(batch),
                                       len(vote_items))
            for batch in failed:
                self._check_cancel()
                try:
                    verdicts = self._coarse_vote(batch, vote_idx)
                except ScreeningCancelled:
                    raise
                except Exception:  # noqa: BLE001 - 重试仍败，该票留空按分裂处理
                    continue
                for it in batch:
                    v = verdicts.get(it['id'])
                    if v is not None:
                        votes[it['id']].append(v)

        # 前两票（措辞变体 + 确定性洗牌去相关）
        run_vote(0, items)
        run_vote(1, items)

        # 第三票只在固定模式投：自适应模式下前两票一旦分裂，三票结果
        # 必是 2:1（照样升级精审），第三票改变不了结局，不花这个钱
        if self.COARSE_ALWAYS_3:
            run_vote(2, items)

        # 计票定案。升级精审的只有两类：票间分裂、keep×危险×拼接
        # （wrap 路径——_() 包裹会改变逻辑值，这个方向值得花精审的钱；
        # table 路径下逻辑仍拿原文值，误判 keep 几乎无害，不必升级）
        escalated = 0
        for i, c in enumerate(candidates):
            keep, reason, decided = self._tally(votes[i])
            if decided:
                c.ai_keep, c.ai_confident, c.ai_reason = keep, True, reason
                if (keep and getattr(c, 'static_danger', False)
                        and getattr(c, 'static_fragment', False)):
                    c.ai_confident = False
                    escalated += 1
            else:
                c.ai_confident = False
                escalated += 1
        if escalated:
            self._log(f'粗筛完成：{escalated} 条升级精审'
                      '（票间分裂或拼接 keep 需证据核实）')

    @staticmethod
    def _tally(votes: list) -> tuple:
        """计票：[(keep, reason), ...] → (keep|None, reason, decided)

        全票一致（≥2 票）→ 定案；任何分歧或票不足 → 未决（升级精审，
        由带工具的精审用代码证据定死）
        """
        keeps = [k for k, _ in votes]
        if len(keeps) >= 2 and all(k == keeps[0] for k in keeps):
            return keeps[0], votes[0][1], True
        return None, '', False

    def _coarse_vote(self, batch: list, vote_idx: int) -> dict:
        """单批投一票，返回 {id: (keep, reason)}

        批内顺序按票种子确定性洗牌（同票同序、异票异序）：票间去相关性
        来自输入差异而非温度——temperature 固定 0（可复现性契约）；
        批次组成不变（保文件局部性，相关条目互为上下文）。
        """
        order = list(batch)
        random.Random(vote_idx).shuffle(order)
        items_json = json.dumps(order, ensure_ascii=False)
        prompt = _COARSE_PROMPT_VARIANTS[
            vote_idx % len(_COARSE_PROMPT_VARIANTS)]
        result = self.translator.analyze_text(
            prompt.format(items_json=items_json),
            max_tokens=max(self.translator.config.max_tokens, 8000),
            temperature=0)
        m = re.search(r'\[.*\]', result, re.S)
        if not m:
            raise ValueError(f'粗筛返回无法解析: {result[:100]}')
        parsed = json.loads(m.group(0))
        verdicts = {}
        for entry in parsed:
            if isinstance(entry, dict) and 'id' in entry:
                verdicts[entry['id']] = (bool(entry.get('keep', True)),
                                         str(entry.get('reason', '')))
        if not verdicts:
            raise ValueError('粗筛返回为空')
        return verdicts

    # ========== 阶段 2：agentic 精审（双跑决胜） ==========

    def _refine_screen(self, candidates: list, progress: dict,
                       cancel_event=None, anchored: dict = None):
        """分裂票与危险 keep 的最终判定

        双跑决胜协议：每批独立精审两次（全新消息链，互不看见）；逐候选
        两次一致 → 定案，不一致 → 第三跑决胜（三票必有多数；极端的
        一票对一票保持未决，不猜）。
        anchored: 可选 {候选索引: {'keep','reason'}}——灰区复核模式，
        上轮判定随条目给出，除非工具找到矛盾新证据否则维持原判。
        """
        if cancel_event is not None:
            self._cancel_event = cancel_event
        if anchored is not None:
            uncertain = list(enumerate(candidates))
        else:
            uncertain = [(i, c) for i, c in enumerate(candidates)
                         if not c.ai_confident]
        if not uncertain:
            return

        batches = [uncertain[i:i + self.REFINE_BATCH]
                   for i in range(0, len(uncertain), self.REFINE_BATCH)]

        # 双跑：每批两个独立 future（消息链互不看见）。
        # 进度按"跑次"计（批次×2），决胜阶段另起——避免 ×2 总量被误读
        progress.update(phase='精审·双跑', done=0, total=len(batches) * 2)
        runs = {}
        for b in batches:
            for _ in range(2):
                runs[self._pool.submit(self._refine_batch, b, progress,
                                       anchored)] = b
        pair_votes = {}  # idx -> [(keep, reason, evidence, apply)]
        for fut in as_completed(runs):
            self._check_cancel()
            b = runs[fut]
            try:
                verdicts = fut.result()
            except ScreeningCancelled:
                raise
            except Exception:  # noqa: BLE001 - 该跑失败，另一跑/决胜跑兜底
                progress['done'] = min(progress['done'] + 1,
                                       len(batches) * 2)
                continue
            for idx, v in verdicts.items():
                pair_votes.setdefault(idx, []).append(v)
            progress['done'] = min(progress['done'] + 1, len(batches) * 2)

        decided = {}
        tiebreak = []
        cand_by_idx = dict(uncertain)
        for idx, c in uncertain:
            vs = pair_votes.get(idx, [])
            keeps = [v[0] for v in vs]
            if len(keeps) >= 2 and keeps[0] == keeps[1]:
                decided[idx] = vs[0]
            else:
                tiebreak.append((idx, c))
        if tiebreak:
            self._log(f'精审双跑 {len(tiebreak)} 条不一致，第三跑决胜...')
            tb_batches = [tiebreak[i:i + self.REFINE_BATCH]
                          for i in range(0, len(tiebreak), self.REFINE_BATCH)]
            progress.update(phase='精审·决胜', done=0, total=len(tb_batches))
            for b in tb_batches:
                self._check_cancel()
                try:
                    verdicts = self._refine_batch(b, anchored=anchored)
                except ScreeningCancelled:
                    raise
                except Exception as e:  # noqa: BLE001 - 决胜仍败，保持未决
                    msg = str(e)
                    for idx, c in b:
                        c.ai_keep = None
                        c.ai_confident = False
                        c.ai_reason = f'精审失败，保持未决: {msg[:160]}'
                    self._log(f'决胜跑后仍有 {len(b)} 条未获判决（已保留为'
                              '未决，可人工判定或再次精判）')
                    progress['done'] = min(progress['done'] + 1,
                                           len(tb_batches))
                    continue
                for idx, v in verdicts.items():
                    prior = pair_votes.get(idx, [])
                    keeps = [x[0] for x in prior] + [v[0]]
                    t, f = keeps.count(True), keeps.count(False)
                    if t == f:
                        # 极端的一票对一票：不猜，保持未决
                        c = cand_by_idx[idx]
                        c.ai_keep = None
                        c.ai_confident = False
                        c.ai_reason = '精审三次判定分歧（keep/drop 僵持），保持未决'
                        continue
                    win = t > f
                    chosen = v if v[0] == win else next(
                        (x for x in prior if x[0] == win), v)
                    decided[idx] = chosen
                progress['done'] = min(progress['done'] + 1, len(tb_batches))
        self._apply_verdicts(candidates, decided)

    @staticmethod
    def _apply_verdicts(candidates: list, verdicts: dict):
        """把 {候选索引: (keep, reason, evidence, apply)} 写回候选并标记已决"""
        for idx, (keep, reason, evidence, apply) in verdicts.items():
            candidates[idx].ai_keep = keep
            candidates[idx].ai_reason = reason
            candidates[idx].ai_evidence = evidence
            candidates[idx].ai_apply = apply
            candidates[idx].ai_confident = True

    def _refine_batch(self, batch: list, progress: dict = None,
                      anchored: dict = None) -> dict:
        """单批 agentic 精审：多轮 tool 循环，
        返回 {候选索引: (keep, reason, evidence, apply)}

        progress 非空时每轮更新 phase 文本（并发批次相互覆盖无碍，仅作展示）。
        anchored 非空时条目带 prior_verdict/prior_reason（灰区复核锚定）。
        """
        id_map = {n: idx for n, (idx, _) in enumerate(batch)}
        items = []
        for n, (idx, c) in enumerate(batch):
            item = {'id': n, 'text': c.text, 'hint': c.hint, 'kind': c.kind,
                    'file': c.rel_file, 'line': c.line}
            ev = self._evidence(c)
            if ev:
                item['evidence'] = ev
            if getattr(c, 'static_reason', ''):
                # 静态用途分析的证据（出现点位置），供 AI 参考而非复述
                item['static_analysis'] = c.static_reason
            if getattr(c, 'static_danger', False):
                item['warning'] = (
                    '该字符串另有比较/键名/资源引用等非显示用途（'
                    + c.static_reason + '）。这些用途不受翻译影响'
                    '（翻译表路径下逻辑仍用原文值），你要核实的只是它'
                    '是否真的会被玩家读到')
            if anchored and idx in anchored:
                item['prior_verdict'] = anchored[idx]['keep']
                item['prior_reason'] = anchored[idx]['reason']
            items.append(item)

        system = _REFINE_SYSTEM + (_ANCHOR_SECTION if anchored else '')
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content":
                '本批候选（JSON 数组）：\n' + json.dumps(items, ensure_ascii=False)},
        ]

        accepted = {}  # 累计已受理的判决 {候选索引: (keep, reason, evidence, apply)}
        for round_no in range(1, self.MAX_ROUNDS + 1):
            self._check_cancel()
            message = self.translator.chat_completion(
                messages=messages,
                # 判定任务固定 0 温度（理由同粗筛）：重判可复现
                temperature=0,
                max_tokens=self.translator.config.max_tokens,
                tools=_TOOLS, tool_choice="auto",
                return_message=True, task_type='analysis',
            )

            if message.tool_calls:
                # submit_verdicts 优先：提取判决
                submit = next((tc for tc in message.tool_calls
                               if tc.function.name == 'submit_verdicts'), None)
                if submit is not None:
                    err = None
                    try:
                        accepted.update(self._parse_verdict_args(
                            submit.function.arguments, id_map))
                    except ValueError as e:
                        err = str(e)
                    missing = [n for n, idx in id_map.items()
                               if idx not in accepted]
                    if not missing:
                        return accepted
                    # 判决不全/参数损坏：不抛错，回喂受理情况并追问补齐
                    self._log(f'精审第 {round_no} 轮判决不全'
                              + (f'（{err}）' if err else '')
                              + f'，缺 {len(missing)} 条，追问补齐')
                    messages.append(message)
                    for tc in message.tool_calls:
                        if tc is submit:
                            content = (
                                f'已受理 {len(accepted)}/{len(id_map)} 条判决。'
                                + (f'注意：上次提交参数有误（{err}）。'
                                   if err else '')
                                + f'还缺 id {missing} 的判决，请再次调用 '
                                  'submit_verdicts 补齐（只需提交缺失的 id）。')
                        else:
                            # 同一条 message 的其他工具调用必须有响应，
                            # 否则下一轮请求会被 API 拒绝
                            content = self._execute_tool(tc.function.name,
                                                         tc.function.arguments)
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": content,
                        })
                    continue

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

            # 无 tool call：兜底从文本解析（允许部分覆盖，累计后追问缺失）
            if message.content:
                got = self._parse_verdicts_text(message.content, id_map)
                if got:
                    accepted.update(got)
                missing = [n for n, idx in id_map.items()
                           if idx not in accepted]
                if not missing:
                    return accepted
                messages.append(message)
                messages.append({"role": "user", "content":
                    f'判决不完整，还缺 id {missing}。'
                    '请调用 submit_verdicts 工具提交这些候选的判定。'})

        # 达到轮数上限：强制要求用纯文本输出判决，再做最后一轮解析
        missing = [n for n, idx in id_map.items() if idx not in accepted]
        self._log(f'精审批次达到 {self.MAX_ROUNDS} 轮上限，要求立即提交')
        messages.append({"role": "user", "content":
            '轮数已达上限。请立即基于已有信息输出最终判决 JSON 数组'
            '（[{"id":N,"keep":true/false,"reason":"..."}]），不要再调用工具。'
            + (f'只需输出还缺判决的 id: {missing}。' if accepted else '')})
        message = self.translator.chat_completion(
            messages=messages,
            # 判定任务固定 0 温度（理由同多轮循环）：重判可复现
            temperature=0,
            max_tokens=max(self.translator.config.max_tokens, 8000),
            return_message=True, task_type='analysis',
        )
        if message.content:
            got = self._parse_verdicts_text(message.content, id_map)
            if got:
                accepted.update(got)
        missing = [n for n, idx in id_map.items() if idx not in accepted]
        if not missing:
            return accepted
        if anchored is not None:
            # 复核模式兜底：查不出矛盾证据就等于维持原判（锚定语义本身），
            # 未获判决的行按上轮判定落地，不按批次失败重烧一遍决胜跑
            for n in missing:
                idx = id_map[n]
                if idx in anchored:
                    accepted[idx] = (anchored[idx]['keep'],
                                     '复核未找到矛盾的新证据，维持原判', '', '')
            self._log(f'复核 {len(missing)} 条未获新判决，按锚定维持原判')
            return accepted
        # 附 AI 最后输出：批次最终失败时（跳过+收尾重试仍败），用户能
        # 看到模型实际回了什么来定位问题
        last = (getattr(message, 'content', '') or '')
        if not last and getattr(message, 'tool_calls', None):
            last = '; '.join(
                (tc.function.arguments or '')[:120]
                for tc in message.tool_calls)
        shown = missing[:8]
        more = f' 等 {len(missing)} 条' if len(missing) > 8 else ''
        raise RuntimeError(
            f'精审批次 {self.MAX_ROUNDS} 轮追问后仍有 {len(missing)} 条'
            f'候选未获判决（id: {shown}{more}）；'
            f'AI 最后输出: {last[:200]!r}')

    def _parse_verdict_args(self, arguments: str, id_map: dict) -> dict:
        """解析 submit_verdicts 工具参数，
        返回 {候选索引: (keep, reason, evidence, apply)}。

        允许部分覆盖：缺漏由调用方追问补齐；仅参数 JSON 损坏时报错
        （调用方同样转为追问）。"""
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError as e:
            raise ValueError(f'submit_verdicts 参数解析失败: {e}') from e
        verdicts = {}
        for entry in args.get('verdicts', []):
            if isinstance(entry, dict) and entry.get('id') in id_map:
                idx = id_map[entry['id']]
                apply = str(entry.get('apply', '') or '')
                verdicts[idx] = (bool(entry.get('keep', True)),
                                 str(entry.get('reason', '')),
                                 str(entry.get('evidence', '')),
                                 apply if apply in ('table', 'wrap') else '')
        return verdicts

    def _parse_verdicts_text(self, content: str, id_map: dict) -> dict:
        """兜底：从文本内容解析判决 JSON。允许部分覆盖（缺漏由调用方
        追问补齐）；找不到可解析的 JSON 才返回 None"""
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
                apply = str(entry.get('apply', '') or '')
                verdicts[idx] = (bool(entry.get('keep', True)),
                                 str(entry.get('reason', '')),
                                 str(entry.get('evidence', '')),
                                 apply if apply in ('table', 'wrap') else '')
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
        if name == 'trace_symbol':
            return self._tool_trace_symbol(args)
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
        # _resolve 已校验在 game_sub 内，可直接取缓存 key
        key = path.relative_to(self.game_sub.resolve()).as_posix()
        lines = self._tree.lines(key)
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
        # 每命中带 ±2 行上下文：判双重用途时命中行本身经常不够
        # （"x" in some_list 还得看这个 list 之后去哪）
        MAX_SHOW = 20
        CTX = 2
        blocks = []
        total = 0
        hit_files = set()
        for rel, i, line in self._tree.search(query):
            total += 1
            hit_files.add(rel)
            if len(blocks) < MAX_SHOW:
                lines = self._tree.lines(rel)
                lo = max(1, i - CTX)
                hi = min(len(lines), i + CTX) if lines else i
                block = '\n'.join(
                    f'{"▶" if no == i else " "} {no}: '
                    f'{lines[no - 1].rstrip()[:120]}'
                    for no in range(lo, hi + 1))
                blocks.append(f'--- {rel}:{i}\n{block}')
        if not total:
            return '（无匹配）'
        header = f'共 {total} 处匹配（{len(hit_files)} 个文件）'
        if total > MAX_SHOW:
            header += (f'，以下只显示前 {MAX_SHOW} 处——判定前请注意结果不完整，'
                       '可用更长/带引号的 query 缩小范围')
        return header + '\n' + '\n'.join(blocks)

    def _tool_trace_symbol(self, args: dict) -> str:
        """符号定义点 + 引用点分类报告：回答"这串经变量传递最终流向哪"，
        比让模型反复 search_code 拼数据流省轮次也省 token"""
        name = str(args.get('name', '')).strip()
        if not name:
            return 'name 为空'
        if self._analyzer is None:
            from usage_rules import UsageAnalyzer
            self._analyzer = UsageAnalyzer(str(self.game_root),
                                           files=self._tree.as_dict())
        rep = self._analyzer.trace_symbol_report(name)
        out = [f'符号 {name}：定义点 {len(rep["definitions"])} 处，'
               f'引用点 {len(rep["references"])} 处'
               + ('（截断仅显示前 50）' if rep['truncated'] else '')]
        for d in rep['definitions']:
            out.append(f'[定义] {d["site"]}: {d["text"]}')
        for r in rep['references']:
            out.append(f'[{r["kind"]}] {r["site"]}: {r["text"]}')
        return '\n'.join(out)
