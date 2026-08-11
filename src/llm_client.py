"""OpenAI 兼容接口客户端封装

独立于 AITranslator 的底层组件：只依赖 config（api_key/api_base/model/timeout），
负责 client 生命周期、统一重试与错误分类。AITranslator 组合它对外提供
chat_completion；调用方不应再直接戳 translator._call_api。
"""

import time
from typing import Optional, Callable

import openai
from openai import OpenAI


class FatalAPIError(Exception):
    """不可重试的 API 错误（认证失败、余额不足等），批量任务应立即中止"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


# 致命错误码（不可重试）及中文说明
_FATAL_REASONS = {
    400: '请求格式错误（请检查模型名称是否正确、参数是否合法、消息格式是否符合接口要求）',
    401: 'API Key 无效或认证失败（请检查 API Key 与 Base URL 是否匹配、请求头格式是否正确）',
    402: '账户余额不足，请及时充值',
    403: '拒绝访问（服务暂不支持当前地区，或 API Key 被风控，请尝试新建 API Key）',
    404: '接口或模型不存在（请检查模型名称与 Base URL 是否正确）',
}

# 可重试错误码及中文说明
_RETRYABLE_REASONS = {
    421: '内容审核拦截',
    429: '请求频率超限',
    500: '服务器内部故障',
    502: '网关错误',
    503: '服务器负载过高',
    504: '网关超时',
}


class LLMClient:
    """OpenAI 兼容接口客户端（含统一重试与错误分类）"""

    MAX_RETRIES = 3  # 可重试错误的最大尝试次数

    def __init__(self, config, api_log_callback: Optional[Callable[[dict, dict, str], None]] = None):
        self.config = config
        self.client: Optional[OpenAI] = None
        self.api_log_callback = api_log_callback
        # api_log_callback(request_body, response_body, task_type)
        self._init_client()

    def _init_client(self):
        if self.config.api_key:
            self.client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base,
                max_retries=0,  # 关闭 SDK 自带重试，由 chat_completion 统一控制
            )

    def update_config(self, config):
        self.config = config
        self._init_client()

    def chat_completion(self, messages: list, temperature: float = None,
                        max_tokens: int = None,
                        tools: list = None, tool_choice=None,
                        return_message: bool = False, task_type: str = ''):
        """统一的 API 调用：按错误码分类处理

        - 致命错误（400/401/402/403/404）：抛出 FatalAPIError，批量任务应立即中止
        - 可重试错误（421/429/5xx/超时/连接错误/空内容拦截）：指数退避重试，最多 MAX_RETRIES 次
        - 重试耗尽：抛出普通异常，调用方记失败并跳过该条目
        - return_message=True 时返回完整 message（用于 tool calls），否则返回 content 文本
        - api_log_callback 存在时，成功响应后将完整请求体/返回体交给回调记录
        - temperature / max_tokens 为 None 时取 config 默认值
        """
        if temperature is None:
            temperature = self.config.temperature
        if max_tokens is None:
            max_tokens = self.config.max_tokens

        reason = '未知错误'
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                kwargs = dict(
                    model=self.config.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=self.config.timeout,
                )
                if tools:
                    kwargs['tools'] = tools
                    kwargs['tool_choice'] = tool_choice
                response = self.client.chat.completions.create(**kwargs)
                if self.api_log_callback:
                    try:
                        self.api_log_callback(kwargs, response.model_dump(), task_type)
                    except Exception:
                        pass  # 日志失败不影响翻译
                message = response.choices[0].message
                if return_message:
                    return message
                content = message.content
                if content is not None:
                    return content.strip()
                # 内容审核拦截时部分服务返回空内容，按可重试错误处理
                reason = '内容审核拦截（返回空内容）'
            except openai.APIStatusError as e:
                code = e.status_code
                if code in _FATAL_REASONS:
                    raise FatalAPIError(code, f'API 错误 {code}：{_FATAL_REASONS[code]}') from e
                reason = _RETRYABLE_REASONS.get(code, f'HTTP {code} 错误')
            except openai.APITimeoutError:
                reason = '请求超时'
            except openai.APIConnectionError:
                reason = '网络连接错误'

            if attempt < self.MAX_RETRIES:
                delay = min(2 ** attempt, 30)
                print(f'[API] {reason}，{delay} 秒后进行第 {attempt + 1}/{self.MAX_RETRIES} 次尝试...')
                time.sleep(delay)

        raise Exception(f'API 调用失败（已重试 {self.MAX_RETRIES} 次）：{reason}')
