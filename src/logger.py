"""统一日志系统

日志级别定义：
    DEBUG    - 翻译 API 请求/响应详情、解析中间结果
    INFO     - 翻译完成、项目保存、导出进度
    WARNING  - 翻译失败重试、缺失配置
    ERROR    - API 调用失败、文件读写错误
"""

import logging
import sys
from logging.handlers import RotatingFileHandler


class TranslationLogger:
    """翻译工具统一日志管理器

    用法：
        logger = TranslationLogger()
        logger.info("翻译完成", panel="dialogue")
        logger.debug("API 请求详情", panel="translator")
        logger.error("翻译失败: xxx", panel="names")

    handler 构成：控制台（stdout）+ 轮转文件（数据根 logs/app.log）。
    内存环形缓冲 handler 由 server/state.py 挂接到 _logger（/api/logs 数据源）。
    """

    # 日志格式
    FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
    DATE_FORMAT = "%H:%M:%S"
    # 文件日志带日期与面板名（renpy_translator.<panel>）
    FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
    FILE_MAX_BYTES = 5 * 1024 * 1024  # 5MB
    FILE_BACKUP_COUNT = 3

    def __init__(self, level: int = logging.DEBUG):
        # 主日志器
        self._logger = logging.getLogger("renpy_translator")
        self._logger.setLevel(level)
        # 重复实例化时先关掉旧 handler（文件句柄不释放会导致轮转失败）
        for h in self._logger.handlers[:]:
            self._logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

        # 控制台 handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter(self.FORMAT, self.DATE_FORMAT))
        self._logger.addHandler(console_handler)

        # 轮转文件 handler（数据根/logs/app.log；目录不可写时静默跳过，
        # 不影响控制台与 ring buffer）
        file_handler = self._make_file_handler()
        if file_handler is not None:
            self._logger.addHandler(file_handler)

        # 独立的面板日志器缓存
        self._panel_loggers: dict[str, logging.Logger] = {}

    def _make_file_handler(self):
        """构建轮转文件 handler；数据根解析/目录创建失败时返回 None"""
        try:
            from rt_home import home
            log_dir = home() / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_dir / 'app.log', maxBytes=self.FILE_MAX_BYTES,
                backupCount=self.FILE_BACKUP_COUNT, encoding='utf-8')
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter(self.FILE_FORMAT, self.FILE_DATE_FORMAT))
            return handler
        except Exception as e:
            print(f'[日志系统] 文件日志初始化失败（仅控制台输出）: {e}')
            return None

    def _get_panel_logger(self, panel: str) -> logging.Logger:
        """获取面板专属日志器"""
        if panel not in self._panel_loggers:
            logger = self._logger.getChild(panel)
            logger.setLevel(logging.DEBUG)
            self._panel_loggers[panel] = logger
        return self._panel_loggers[panel]

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
