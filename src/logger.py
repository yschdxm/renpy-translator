"""统一日志系统

日志级别定义：
    DEBUG    - 翻译 API 请求/响应详情、解析中间结果
    INFO     - 翻译完成、项目保存、导出进度
    WARNING  - 翻译失败重试、缺失配置
    ERROR    - API 调用失败、文件读写错误
"""

import logging
import sys
from typing import Callable, Optional


class UILogHandler(logging.Handler):
    """将日志推送到 NiceGUI ui.log 组件的 Handler"""

    def __init__(self):
        super().__init__()
        self._callbacks: dict[str, Callable[[str], None]] = {}

    def register(self, panel_key: str, callback: Callable[[str], None]):
        """注册 UI 日志回调"""
        self._callbacks[panel_key] = callback

    def unregister(self, panel_key: str):
        """取消注册"""
        self._callbacks.pop(panel_key, None)

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        for panel_key, callback in self._callbacks.items():
            try:
                callback(msg)
            except Exception as e:
                # 不能用 self.logger 记录，会无限递归，用 print 输出到控制台
                print(f'[日志系统] UI回调失败 ({panel_key}): {e}')


class TranslationLogger:
    """翻译工具统一日志管理器

    用法：
        logger = TranslationLogger()
        logger.info("翻译完成", panel="dialogue")
        logger.debug("API 请求详情", panel="translator")
        logger.error("翻译失败: xxx", panel="names")

        # 绑定 UI 日志面板
        logger.bind_ui("dialogue", ui_log_push_fn)
    """

    # 日志格式
    FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
    DATE_FORMAT = "%H:%M:%S"

    def __init__(self, level: int = logging.DEBUG):
        # 主日志器
        self._logger = logging.getLogger("renpy_translator")
        self._logger.setLevel(level)
        self._logger.handlers.clear()

        # 控制台 handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter(self.FORMAT, self.DATE_FORMAT))
        self._logger.addHandler(console_handler)

        # UI handler
        self._ui_handler = UILogHandler()
        self._ui_handler.setLevel(logging.INFO)
        self._ui_handler.setFormatter(logging.Formatter(self.FORMAT, self.DATE_FORMAT))
        self._logger.addHandler(self._ui_handler)

        # 独立的面板日志器缓存
        self._panel_loggers: dict[str, logging.Logger] = {}

    def _get_panel_logger(self, panel: str) -> logging.Logger:
        """获取面板专属日志器"""
        if panel not in self._panel_loggers:
            logger = self._logger.getChild(panel)
            logger.setLevel(logging.DEBUG)
            self._panel_loggers[panel] = logger
        return self._panel_loggers[panel]

    def bind_ui(self, panel_key: str, push_callback: Callable[[str], None]):
        """绑定 UI 日志面板

        Args:
            panel_key: 面板标识 (names/strings/dialogue/export/analysis)
            push_callback: ui.log 的 push 方法或等效回调
        """
        self._ui_handler.register(panel_key, push_callback)

    def unbind_ui(self, panel_key: str):
        """解绑 UI 日志面板"""
        self._ui_handler.unregister(panel_key)

    def set_level(self, level: int):
        """设置全局日志级别"""
        self._logger.setLevel(level)

    # ---- 便捷方法 ----

    def debug(self, msg: str, panel: str = ""):
        """调试信息 - API 请求/响应详情、解析中间结果"""
        logger = self._get_panel_logger(panel) if panel else self._logger
        logger.debug(msg)

    def info(self, msg: str, panel: str = ""):
        """一般信息 - 翻译完成、项目保存、导出进度"""
        logger = self._get_panel_logger(panel) if panel else self._logger
        logger.info(msg)

    def warning(self, msg: str, panel: str = ""):
        """警告 - 翻译失败重试、缺失配置"""
        logger = self._get_panel_logger(panel) if panel else self._logger
        logger.warning(msg)

    def error(self, msg: str, panel: str = ""):
        """错误 - API 调用失败、文件读写错误"""
        logger = self._get_panel_logger(panel) if panel else self._logger
        logger.error(msg)

    def exception(self, msg: str, panel: str = ""):
        """异常 - 包含堆栈信息"""
        logger = self._get_panel_logger(panel) if panel else self._logger
        logger.exception(msg)
