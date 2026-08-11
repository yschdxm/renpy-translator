"""Token 预算统一计算

把散落在 translator / translation_service / name_translation 里的
token 魔数收敛到一处。所有调用点共用同一模型：

    可用额度 = 上下文窗口 - 固定开销（模板 + 实测术语表/角色特征）
    按行/按原文估算占用，并为输出预留余量

四处调用点语义不同（对话批次原文上限 / 上下文行数 / 人名分析分段条数 /
输出 max_tokens），分别对应 TokenBudget 的四个方法，但常数只有这一份。
"""

import tiktoken

# BPE 编码器（cl100k_base，对 GPT 系列精确，对国产模型是误差 <15% 的近似，
# 远好于 len//3 对中文的 3 倍失真）。惰性加载：打包环境若缺 tiktoken_ext
# 插件元数据，get_encoding 会在导入期炸掉整个项目打开流程，降级为粗估即可。
_ENC = None
_ENC_FAILED = False


def count_tokens(text: str) -> int:
    """估算文本 token 数（编码器不可用时退化为 len//3 粗估）"""
    global _ENC, _ENC_FAILED
    if _ENC is None and not _ENC_FAILED:
        try:
            _ENC = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENC_FAILED = True
    if _ENC is None:
        return len(text or "") // 3
    return len(_ENC.encode(text or ""))


class TokenBudget:
    """统一 token 预算计算

    常数出处（合并前各自所在位置的估算值，语义保持不变）：
    - PROMPT_TEMPLATE_TOKENS：单句/上下文场景的系统提示词模板开销
      （原 translation_service._calc_context_count 的 270）
    - BATCH_FIXED_TOKENS：批次翻译的系统+用户提示词模板、前文参考、
      tool schema 的估算开销（原 TranslationService._BATCH_FIXED_TOKENS = 1000）
    - NAME_PROMPT_OVERHEAD / NAME_OUTPUT_RESERVE：人名分析提示词开销与
      输出预留（原 name_translation.calc_batch_size 的 800 / 2000）
    - CONTEXT_LINE_TOKENS：每行上下文（原文+译文+角色名）约 50 token
    - NAME_LINE_TOKENS：每句台词约 20 token（英文平均）
    - OUTPUT_RATIO / OUTPUT_FLOOR：译文按原文 1.2 倍 + 300 余量估算输出
      （英→中通常缩短，1.2 确保不被截断；300 覆盖 tool 调用 JSON 格式开销）
    """

    PROMPT_TEMPLATE_TOKENS = 270   # 系统提示词模板（单句/上下文场景）
    BATCH_FIXED_TOKENS = 1000      # 批次模板 + 前文参考 + tool schema
    NAME_PROMPT_OVERHEAD = 800     # 人名分析提示词开销
    NAME_OUTPUT_RESERVE = 2000     # 人名分析输出预留
    CONTEXT_LINE_TOKENS = 50       # 每行上下文（原文 + 译文 + 角色名）
    NAME_LINE_TOKENS = 20          # 每条台词（英文平均）
    OUTPUT_RATIO = 1.2             # 输出 / 原文放大系数
    OUTPUT_FLOOR = 300             # 输出基础余量（格式开销）
    MIN_OUTPUT_TOKENS = 1000       # 窗口约束下的 max_tokens 下限
    MIN_BATCH_SRC_TOKENS = 500     # 批次原文 token 下限（小窗口兜底）
    NAME_USAGE_RATIO = 0.6         # 人名分析安全系数（只用 60% 可用空间）

    def __init__(self, window_tokens: int):
        self.window_tokens = window_tokens

    def batch_src_token_budget(self, glossary_tokens: int = 0,
                               profile_tokens: int = 0,
                               declared_max_tokens: int = 0) -> int:
        """对话批次原文 token 上限

        约束：固定开销 + 原文 + 输出(原文×1.2+300) ≤ 窗口，
        且 输出 ≤ 模型声明 max_tokens。
        """
        fixed = self.BATCH_FIXED_TOKENS + glossary_tokens + profile_tokens
        window_cap = int((self.window_tokens - fixed - self.OUTPUT_FLOOR)
                         / (1 + self.OUTPUT_RATIO))
        # 声明 cap 无条件参与 min：declared_max_tokens=0（配置错误）时
        # 钉死在下限，与旧实现语义一致
        declared_cap = int((declared_max_tokens - self.OUTPUT_FLOOR)
                           / self.OUTPUT_RATIO)
        return max(self.MIN_BATCH_SRC_TOKENS, min(window_cap, declared_cap))

    def context_line_count(self, glossary_tokens: int = 0,
                           profile_tokens: int = 0,
                           min_lines: int = 3, max_lines: int = 20) -> int:
        """按剩余窗口动态计算可携带的上下文行数"""
        fixed = self.PROMPT_TEMPLATE_TOKENS + glossary_tokens + profile_tokens
        available = self.window_tokens - fixed
        count = max(min_lines, available // self.CONTEXT_LINE_TOKENS)
        return min(count, max_lines)

    def name_batch_size(self, total_lines: int) -> int:
        """人名分析每段台词条数（可用空间的 60%，安全余量）"""
        available = (self.window_tokens - self.NAME_PROMPT_OVERHEAD
                     - self.NAME_OUTPUT_RESERVE)
        size = max(10, int(available * self.NAME_USAGE_RATIO
                           / self.NAME_LINE_TOKENS))
        return min(size, total_lines)

    def output_max_tokens(self, src_tokens: int, declared_max_tokens: int,
                          input_tokens: int = None) -> int:
        """批次翻译的输出 max_tokens

        不低于模型声明值与原文估算输出的较大者；给出窗口与实际输入时，
        再钳制到 窗口 - 实际输入（deepseek 等超限直接 400），保底 1000。
        """
        budget = max(declared_max_tokens,
                     int(src_tokens * self.OUTPUT_RATIO) + self.OUTPUT_FLOOR)
        if self.window_tokens and input_tokens is not None:
            budget = min(budget, max(self.MIN_OUTPUT_TOKENS,
                                     self.window_tokens - input_tokens))
        return budget
