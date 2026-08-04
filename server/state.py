"""应用状态单例：替代 NiceGUI 的 main.App

单用户本地工具：一个当前项目、一条项目库连接，语义与旧版一致。
所有阻塞调用（sqlite/文件/AI）由路由层放 executor 执行。
"""
import asyncio
import logging
import shutil
import sys
from collections import deque
from pathlib import Path


class _RingBufferHandler(logging.Handler):
    """把 TranslationLogger 的日志写入内存环形缓冲（供 /api/logs 读取）"""

    def __init__(self, buffer: deque):
        super().__init__(level=logging.INFO)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord):
        try:
            name = record.name  # renpy_translator.<panel>
            panel = name.split('.', 1)[1] if '.' in name else ''
            self._buffer.append({
                'time': self.formatter.formatTime(record, '%H:%M:%S'),
                'level': record.levelname,
                'panel': panel,
                'message': record.getMessage(),
            })
        except Exception:
            pass


class AppState:
    def __init__(self, root: Path):
        self.root = Path(root)
        src = self.root / 'src'
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))

        from .appdb import AppDatabase
        from .jobs import EventBus, JobRegistry
        # 核心模块（src/ 在 sys.path 上）
        from config_manager import ConfigManager
        from logger import TranslationLogger
        from project_manager import ProjectManager
        from sdk_manager import SDKManager

        self.app_db = AppDatabase(self.root / 'data' / 'app.db')
        self.bus = EventBus()
        self.jobs = JobRegistry(self.app_db, self.bus)
        self.config_manager = ConfigManager()
        self.project_manager = ProjectManager()
        self.sdk_manager = SDKManager()
        self.logger = TranslationLogger()
        self.log_buffer = deque(maxlen=500)
        handler = _RingBufferHandler(self.log_buffer)
        handler.setFormatter(logging.Formatter())
        self.logger._logger.addHandler(handler)

        # 当前项目会话
        self.current_project: str = ''
        self.db = None                # ProjectDatabase
        self.translator = None        # AITranslator
        self.translation_service = None
        self.db_lock = asyncio.Lock()
        self.interrupted_count = 0    # 启动时标记的中断任务数（前端汇总提示用）

    # ---- 生命周期 ----

    async def startup(self):
        loop = asyncio.get_event_loop()
        # 上次退出时未结束的任务 → interrupted（终态，仅作历史记录；
        # 计数透给前端做一次性汇总提示，不逐条弹窗）
        n = await loop.run_in_executor(None, self.app_db.mark_interrupted)
        self.interrupted_count = n
        if n:
            self.logger.info(f'检测到 {n} 个被中断的任务（上次服务退出时未结束）')
        last = await loop.run_in_executor(
            None, self.app_db.get_setting, 'current_project', '')
        if last and self.project_manager.project_exists(last):
            await self.open_project(last)

    async def shutdown(self):
        loop = asyncio.get_event_loop()
        # 先取消所有未完成任务并等其退出，再关库（否则任务收尾写库撞关库）
        for rec in self.app_db.list_jobs(active_only=True):
            self.jobs.cancel(rec['id'])
        for _ in range(30):
            active = [self.jobs.get(r['id'])
                      for r in self.app_db.list_jobs(active_only=True)]
            if all(j is None or j.done for j in active):
                break
            await asyncio.sleep(0.1)
        if self.db is not None:
            db = self.db
            self.db = None
            await loop.run_in_executor(None, db.close)
        await loop.run_in_executor(None, self.app_db.close)

    # ---- 项目会话 ----

    async def open_project(self, name: str):
        """移植自旧 main.py::_open_project（去掉 UI 部分）"""
        loop = asyncio.get_event_loop()
        if not self.project_manager.project_exists(name):
            from .errors import ApiError
            raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')

        # 先关旧库（Windows 上不关会导致文件锁）
        if self.db is not None:
            old = self.db
            self.db = None
            await loop.run_in_executor(None, old.close)

        db = await loop.run_in_executor(
            None, self.project_manager.open_project, name)
        model_name = await loop.run_in_executor(
            None, db.get_meta, 'model_config_name')

        from translator import AITranslator, TranslationConfig
        from translation_service import TranslationService
        translator = None
        max_context_k, max_tokens, batch_lines = 8, 1000, 100
        if model_name:
            cfg = self.config_manager.get_config_by_name(model_name)
            if cfg:
                translator = AITranslator(TranslationConfig(
                    api_base=cfg.api_base, api_key=cfg.api_key,
                    model=cfg.model, temperature=cfg.temperature,
                    max_tokens=cfg.max_tokens,
                    context_lines=cfg.context_lines, timeout=cfg.timeout,
                ))
                translator.api_log_callback = self._on_api_log
                max_context_k = getattr(cfg, 'max_context', 8)
                max_tokens = getattr(cfg, 'max_tokens', 1000)
                batch_lines = getattr(cfg, 'batch_lines', 100)

        service = None
        if translator:
            service = TranslationService(
                translator=translator, db=db, logger=self.logger,
                max_context_k=max_context_k, max_tokens=max_tokens,
                batch_lines=batch_lines,
            )

            # 每次批量翻译前重读模型配置，配置保存后立即生效
            def config_provider():
                if not self.db:
                    return None
                name = self.db.get_meta('model_config_name')
                mc = self.config_manager.get_config_by_name(name) if name else None
                if not mc:
                    return None
                return (getattr(mc, 'max_context', 8),
                        getattr(mc, 'max_tokens', 1000),
                        getattr(mc, 'batch_lines', 100))
            service.config_provider = config_provider

        self.db = db
        self.translator = translator
        self.translation_service = service
        self.current_project = name
        await loop.run_in_executor(
            None, self.app_db.set_setting, 'current_project', name)
        self.logger.info(f'已打开项目: {name}')

    async def close_project(self):
        loop = asyncio.get_event_loop()
        if self.db is not None:
            db = self.db
            self.db = None
            await loop.run_in_executor(None, db.close)
        self.translator = None
        self.translation_service = None
        self.current_project = ''
        await loop.run_in_executor(
            None, self.app_db.set_setting, 'current_project', '')

    async def db_call(self, fn, *args, **kwargs):
        """项目库调用：executor + 锁，避免并发写"""
        async with self.db_lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, lambda: fn(*args, **kwargs))

    # ---- 数据目录迁移 ----

    MIGRATABLE_DIRS = ('projects', 'config', 'logs', 'data',
                       'exports', 'fonts', 'tools')

    async def relocate_home(self, new_home: Path) -> dict:
        """迁移用户数据到新目录并切换。失败在动手前抛出（预检），不打断运行状态。

        流程：预检（活跃任务/路径合法性/目标冲突）→ 关项目库 → 关应用库 →
        逐目录移动 → 写指针 → 重建管理器 → 重开项目。
        """
        from .errors import ApiError
        from rt_home import set_home

        old_home = Path(self.root).resolve()
        new_home = Path(new_home).expanduser().resolve()

        # 活跃任务禁止迁移
        active = self.app_db.list_jobs(active_only=True)
        if active:
            raise ApiError(409, 'JOBS_RUNNING',
                           f'有 {len(active)} 个任务正在进行，请完成或取消后再迁移')

        if new_home == old_home:
            raise ApiError(400, 'SAME_DIR', '新目录与当前数据目录相同')
        if new_home.is_relative_to(old_home) or old_home.is_relative_to(new_home):
            raise ApiError(400, 'NESTED_DIR',
                           '新旧数据目录不能互为子目录')

        # 目标冲突预检（不动手）
        conflicts = []
        for name in self.MIGRATABLE_DIRS:
            src = old_home / name
            dst = new_home / name
            if src.exists() and dst.exists() and any(dst.iterdir()):
                conflicts.append(f'{name}（{dst} 非空）')
        if conflicts:
            raise ApiError(409, 'DEST_CONFLICT',
                           '目标目录已存在同名数据：' + '、'.join(conflicts))

        was_open = self.current_project
        loop = asyncio.get_event_loop()

        # 关库（顺序：项目库 → 应用库）
        if self.db is not None:
            await self.close_project()
        await loop.run_in_executor(None, self.app_db.close)

        def _migrate():
            new_home.mkdir(parents=True, exist_ok=True)
            moved = []
            for name in self.MIGRATABLE_DIRS:
                src = old_home / name
                if not src.exists():
                    continue
                dst = new_home / name
                if dst.exists():  # 空目录直接替换
                    dst.rmdir()
                shutil.move(str(src), str(dst))
                moved.append(name)
            return moved

        moved = await loop.run_in_executor(None, _migrate)
        set_home(new_home)

        # 重建管理器与状态
        from config_manager import ConfigManager
        from project_manager import ProjectManager
        from .appdb import AppDatabase
        self.root = new_home
        self.app_db = AppDatabase(new_home / 'data' / 'app.db')
        self.config_manager = ConfigManager()
        self.project_manager = ProjectManager()

        if was_open and self.project_manager.project_exists(was_open):
            await self.open_project(was_open)

        self.logger.info(f'数据目录已迁移: {old_home} -> {new_home}（{len(moved)} 项）')
        return {'home': str(new_home), 'moved': moved}

    # ---- API 日志（移植自旧 main.py::_on_api_log）----

    _api_log_lock = None

    def _on_api_log(self, request_body: dict, response_body: dict, task_type: str):
        """API 日志回调：完整请求体/返回体写入 logs/api.log（API_LOG 环境变量控制）

        该回调在翻译线程中触发，文件写入需加锁。
        """
        import json
        import os
        import threading
        from datetime import datetime
        if not os.environ.get('API_LOG'):
            return
        if self._api_log_lock is None:
            self._api_log_lock = threading.Lock()

        type_labels = {'name': '人名翻译', 'ui': '字符串翻译', 'dialogue': '对话翻译',
                       'analysis': '分析', 'test': '连接测试'}
        label = type_labels.get(task_type, task_type)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # messages 里可能含 openai 的 pydantic Message 对象（多轮 tool 循环时），
        # json.dumps 默认无法序列化会导致整轮日志被静默跳过
        def _json_safe(obj):
            if hasattr(obj, 'model_dump'):
                return obj.model_dump()
            return str(obj)

        entry = (
            f'{"=" * 70}\n'
            f'[{timestamp}] {label}\n'
            f'{"=" * 70}\n'
            f'[请求体]\n{json.dumps(request_body, ensure_ascii=False, indent=2, default=_json_safe)}\n\n'
            f'[返回体]\n{json.dumps(response_body, ensure_ascii=False, indent=2, default=_json_safe)}\n\n'
        )
        log_path = self.root / 'logs' / 'api.log'
        with self._api_log_lock:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(entry)
            except OSError as e:
                print(f'[API日志] 写入失败: {e}')
