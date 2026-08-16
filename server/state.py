"""应用状态单例：替代 NiceGUI 的 main.App

单用户本地工具：一个当前项目、一条项目库连接，语义与旧版一致。
所有阻塞调用（sqlite/文件/AI）由路由层放 executor 执行。
"""
import asyncio
import logging
import shutil
import sys
import tempfile
from collections import deque
from functools import partial
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


def _move_tree(src: Path, dst: Path) -> list:
    """移动目录树；返回未能从源删除的文件相对路径列表（被占用等）。

    先尝试整体 shutil.move；失败（跨盘回退复制时撞到占用文件、
    目录内有打开句柄等）则逐文件复制——读别的进程打开的文件没问题，
    删不掉的（如本进程/GUI 持有的 logs/*.log）留在源目录。
    """
    try:
        shutil.move(str(src), str(dst))
        return []
    except OSError:
        pass
    leftover = []
    dst.mkdir(parents=True, exist_ok=True)
    for p in src.rglob('*'):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, target)
            p.unlink()
        except OSError:
            leftover.append(str(rel))
    # 能删的空目录清掉；含占用文件的骨架留在源目录
    for d in sorted((d for d in src.rglob('*') if d.is_dir()),
                    key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        src.rmdir()
    except OSError:
        pass
    return leftover


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
        self.name_service = None      # NameTranslationService（随项目会话缓存）
        self._name_service_key = None
        self.db_lock = asyncio.Lock()
        self.interrupted_count = 0    # 启动时标记的中断任务数（前端汇总提示用）

        # 临时文件统一进数据根/temp（系统 TEMP 删除失败会撑爆），
        # 并通过 tempfile.tempdir 覆盖所有 stdlib 临时文件落点
        self.temp_dir = Path(self.root) / 'temp'
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(self.temp_dir)

    # ---- 生命周期 ----

    async def startup(self):
        loop = asyncio.get_event_loop()
        # 清空上次会话残留的临时文件（崩溃/强杀时删除失败的兜底）
        await loop.run_in_executor(None, self._clean_temp)
        # 上次迁移时被占用而留下的旧目录文件（迁移后自动重启时走到这里）
        await loop.run_in_executor(None, self._run_pending_cleanup)
        # 上次退出时未结束的任务 → interrupted（终态，仅作历史记录；
        # 计数透给前端做一次性汇总提示，不逐条弹窗）
        n = await loop.run_in_executor(None, self.app_db.mark_interrupted)
        self.interrupted_count = n
        if n:
            self.logger.info(f'检测到 {n} 个被中断的任务（上次服务退出时未结束）')
        last = await loop.run_in_executor(
            None, self.app_db.get_setting, 'current_project', '')
        if last and self.project_manager.project_exists(last):
            try:
                await self.open_project(last)
            except Exception as e:
                # project.db 损坏等情况：记日志并保持无项目状态，
                # 服务照常启动，用户可在界面上重新打开或删除坏项目
                self.logger.error(f'自动打开上次项目失败（已跳过）: {last} - {e}')
        # 常备 8.x + 7.x 两个 SDK：缺哪个大版本自动后台下载补齐
        await self._ensure_sdks()

    async def _ensure_sdks(self):
        """检测默认目录下的 SDK，缺的大版本以任务形式自动后台下载。

        Ren'Py 7 与 8 的 .rpyc 互不兼容，创建项目/导出校验按游戏引擎
        选 SDK，两个大版本都得有。
        """
        from sdk_manager import (DEFAULT_SDK_7, DEFAULT_SDK_8)
        loop = asyncio.get_event_loop()
        majors = set(await loop.run_in_executor(None, self.sdk_paths))
        from .sdk_download import make_download_body
        for major, version in ((8, DEFAULT_SDK_8), (7, DEFAULT_SDK_7)):
            if major in majors:
                continue
            self.logger.info(
                f"未检测到 Ren'Py {major}.x SDK，自动后台下载 {version}")
            self.jobs.create(
                'settings.sdk-download',
                f"自动下载 Ren'Py SDK {version}",
                {'version': version, 'auto': True},
                make_download_body(self, version))

    def _clean_temp(self):
        """清空临时目录内容（保留目录本身；占用中的文件跳过）"""
        for item in self.temp_dir.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except OSError:
                pass

    async def shutdown(self):
        loop = asyncio.get_event_loop()
        # 先取消所有未完成任务并等其退出，再关库（否则任务收尾写库撞关库）
        for rec in self.app_db.list_jobs(active_only=True):
            self.jobs.cancel(rec['id'])
        for _ in range(100):  # 最多等 10s
            active = [self.jobs.get(r['id'])
                      for r in self.app_db.list_jobs(active_only=True)]
            if all(j is None or j.done for j in active):
                break
            await asyncio.sleep(0.1)
        # 超时兜底：仍未退出的任务直接标终态，不留 running 僵尸
        # （任务收尾写库已被 registry._finalize 防御，这里覆盖其失败窗口）
        leftover = self.app_db.list_jobs(active_only=True)
        for rec in leftover:
            try:
                self.app_db.update_job(rec['id'], status='interrupted')
            except Exception as e:
                self.logger.warning(f'关停兜底标记任务失败: {rec["id"]} - {e}')
        if leftover:
            self.logger.warning(
                f'关停等待超时，{len(leftover)} 个任务被标记为 interrupted')
        self._close_session_services()
        if self.db is not None:
            db = self.db
            self.db = None
            await loop.run_in_executor(None, db.close)
        await loop.run_in_executor(None, self.app_db.close)

    def _close_session_services(self):
        """关闭项目会话内持有线程池的服务（翻译服务 + 人名服务缓存）"""
        if self.name_service is not None:
            self.name_service.close()
            self.name_service = None
            self._name_service_key = None
        if self.translation_service is not None:
            self.translation_service.close()
            self.translation_service = None

    def get_name_service(self, max_context_k: int = 8):
        """人名翻译服务（随项目会话缓存复用，hooks 由调用方接）

        服务内部持有 ThreadPoolExecutor，每次新建而不 shutdown 会泄漏线程，
        因此按 (db, translator, translation_service, max_context_k) 缓存复用；
        项目切换/模型配置变化导致任一依赖变更时，关闭旧实例线程池后重建。
        """
        from .errors import ApiError
        if not self.translation_service or not self.translator:
            raise ApiError(409, 'NO_TRANSLATOR', '请先配置翻译器（模型配置）')
        key = (id(self.db), id(self.translator),
               id(self.translation_service), max_context_k)
        if self.name_service is not None and self._name_service_key == key:
            return self.name_service
        # 依赖变化：释放旧实例线程池后重建
        if self.name_service is not None:
            self.name_service.close()
        from services.name_translation import NameTranslationService
        self.name_service = NameTranslationService(
            db=self.db, translator=self.translator,
            translation_service=self.translation_service,
            logger=self.logger, max_context_k=max_context_k)
        self._name_service_key = key
        return self.name_service

    # ---- SDK 路径（固定默认目录 tools/，不支持自定义） ----

    def sdk_paths(self) -> dict:
        """{8: path, 7: path}：默认目录下已安装的 SDK，每个大版本取最新"""
        from sdk_manager import find_installed_sdks
        best = {}
        for v, p in find_installed_sdks():
            if v[0] not in best or v > best[v[0]][0]:
                best[v[0]] = (v, str(p))
        return {m: p for m, (v, p) in best.items()}

    def resolve_sdk_path(self, game_dir=None) -> str:
        """按游戏目录的引擎大版本选 SDK；无游戏目录时返回主 SDK（8.x 优先）。

        游戏版本可探测但对应大版本未安装时返回 ''（调用方给出指引）。
        """
        from sdk_manager import detect_engine_version
        paths = self.sdk_paths()
        if game_dir is not None:
            gv = detect_engine_version(Path(game_dir))
            if gv:
                return paths.get(gv[0], '')
        return paths.get(8) or next(iter(paths.values()), '')

    # ---- 项目会话 ----

    async def open_project(self, name: str):
        """移植自旧 main.py::_open_project（去掉 UI 部分）"""
        loop = asyncio.get_event_loop()
        if not self.project_manager.project_exists(name):
            from .errors import ApiError
            raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')

        # 先关旧会话服务（释放线程池）与旧库（Windows 上不关会导致文件锁）
        self._close_session_services()
        if self.db is not None:
            old = self.db
            self.db = None
            await loop.run_in_executor(None, old.close)

        db = await loop.run_in_executor(
            None, self.project_manager.open_project, name)

        from translator import AITranslator, TranslationConfig
        from translation_service import TranslationService
        translator = None
        max_context_k, max_tokens, batch_lines = 8, 1000, 100
        cfg = await loop.run_in_executor(None, self._current_model_config, db)
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
            service.config_provider = self._make_config_provider()

        self.db = db
        self.translator = translator
        self.translation_service = service
        self.current_project = name
        await loop.run_in_executor(
            None, self.app_db.set_setting, 'current_project', name)
        self.logger.info(f'已打开项目: {name}')

    def _current_model_config(self, db=None):
        """当前生效的模型配置：全局激活的配置优先；
        未激活过时回退项目内保存的选择（旧版项目兼容）"""
        db = db if db is not None else self.db
        cfg = self.config_manager.get_active_config()
        if cfg or not db:
            return cfg
        legacy = db.get_meta('model_config_name')
        return (self.config_manager.get_config_by_name(legacy)
                if legacy else None)

    def _make_config_provider(self):
        """每次批量翻译前重读模型配置，配置保存后立即生效"""
        def config_provider():
            mc = self._current_model_config()
            if not mc:
                return None
            return (getattr(mc, 'max_context', 8),
                    getattr(mc, 'max_tokens', 1000),
                    getattr(mc, 'batch_lines', 100))
        return config_provider

    def refresh_translator(self):
        """模型配置保存后刷新当前会话的翻译器（同步方法，经 run_sync 调用）

        翻译器只在打开项目时构建一次；api_key/api_base/model 变更后若不刷新，
        运行中的会话仍持旧 key 调用 LLM（表现为配置已保存但调用失败）。
        已有翻译器时原地更新（不打断进行中的批量任务），没有时现建。
        """
        if not self.db:
            return
        cfg = self._current_model_config()
        if not cfg:
            return  # 未激活模型配置：保留现状，翻译时会有明确报错

        from translator import AITranslator, TranslationConfig
        tconf = TranslationConfig(
            api_base=cfg.api_base, api_key=cfg.api_key,
            model=cfg.model, temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            context_lines=cfg.context_lines, timeout=cfg.timeout,
        )
        max_context_k = getattr(cfg, 'max_context', 8)
        max_tokens = getattr(cfg, 'max_tokens', 1000)
        batch_lines = getattr(cfg, 'batch_lines', 100)

        if self.translator is not None:
            self.translator.update_config(tconf)
            if self.translation_service is not None:
                self.translation_service.max_context_k = max_context_k
                self.translation_service.max_tokens = max_tokens
                self.translation_service.batch_lines = batch_lines
            return

        # 打开项目时未配置模型（或配置当时无效）：现在补齐翻译器与服务
        from translation_service import TranslationService
        translator = AITranslator(tconf)
        translator.api_log_callback = self._on_api_log
        service = TranslationService(
            translator=translator, db=self.db, logger=self.logger,
            max_context_k=max_context_k, max_tokens=max_tokens,
            batch_lines=batch_lines,
        )
        service.config_provider = self._make_config_provider()
        self.translator = translator
        self.translation_service = service
        self.logger.info('模型配置已生效，翻译器已就绪')

    async def close_project(self):
        loop = asyncio.get_event_loop()
        self._close_session_services()
        if self.db is not None:
            db = self.db
            self.db = None
            await loop.run_in_executor(None, db.close)
        self.translator = None
        self.current_project = ''
        await loop.run_in_executor(
            None, self.app_db.set_setting, 'current_project', '')

    async def run_sync(self, fn, *args, **kwargs):
        """阻塞函数放默认 executor 执行的通用桥（不持 db_lock）"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, partial(fn, *args, **kwargs))

    async def db_call(self, fn, *args, **kwargs):
        """项目库调用：executor + 锁，避免并发写"""
        async with self.db_lock:
            return await self.run_sync(fn, *args, **kwargs)

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
            moved, leftover = [], []
            for name in self.MIGRATABLE_DIRS:
                src = old_home / name
                if not src.exists():
                    continue
                dst = new_home / name
                if dst.exists():  # 空目录直接替换
                    dst.rmdir()
                for rel in _move_tree(src, dst):
                    leftover.append((f'{name}/{rel}', str(src / rel)))
                moved.append(name)
            return moved, leftover

        from .appdb import AppDatabase
        try:
            moved, leftover = await loop.run_in_executor(None, _migrate)
        except Exception:
            # 迁移中途失败（权限/杀软占用等）：重开旧应用库恢复可用状态，
            # 否则 self.app_db 指向已关闭的库，后续接口全部 500
            self.app_db = AppDatabase(old_home / 'data' / 'app.db')
            self.jobs.app_db = self.app_db
            # 项目库迁移前已关，尽量恢复原项目（目录可能已部分搬走，失败仅记日志）
            if was_open:
                try:
                    await self.open_project(was_open)
                except Exception as e:
                    self.logger.error(f'迁移失败后恢复原项目失败: {was_open} - {e}')
            raise
        set_home(new_home)

        # 重建管理器与状态
        from config_manager import ConfigManager
        from project_manager import ProjectManager
        self.root = new_home
        self.app_db = AppDatabase(new_home / 'data' / 'app.db')
        # JobRegistry 内部缓存了 app_db 引用，重建后需同步刷新
        self.jobs.app_db = self.app_db
        self.config_manager = ConfigManager()
        self.project_manager = ProjectManager()
        # 临时目录随数据根迁移
        self.temp_dir = new_home / 'temp'
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(self.temp_dir)

        if was_open and self.project_manager.project_exists(was_open):
            await self.open_project(was_open)

        self.logger.info(f'数据目录已迁移: {old_home} -> {new_home}（{len(moved)} 项）')
        result = {'home': str(new_home), 'moved': moved}
        if leftover:
            # 被自身/GUI 进程占用的文件（logs/*.log 等）：内容已复制到新目录，
            # 记录待清理清单，由「自动重启 → 启动时清理」流程收尾
            names = [n for n, _ in leftover]
            self.logger.warning(
                f'{len(names)} 个文件被占用未能从旧目录移除（已复制到新目录）: '
                + '、'.join(names[:5]))
            result['leftover'] = names
            result['leftover_dir'] = str(old_home)
            import json
            self.app_db.set_setting('pending_cleanup', json.dumps(
                {'old_home': str(old_home),
                 'files': [a for _, a in leftover]}))
        return result

    # ---- 迁移残留清理（重启后启动时执行） ----

    def _run_pending_cleanup(self):
        """清理上次迁移时被占用而留在旧目录的文件。

        仍有文件被占用（如 GUI 进程持有的旧 gui.log）则保留记录，
        下次启动再试；清完即删除记录。
        """
        import json
        raw = self.app_db.get_setting('pending_cleanup', '')
        if not raw:
            return
        try:
            rec = json.loads(raw)
        except ValueError:
            self.app_db.set_setting('pending_cleanup', '')
            return
        old_home = Path(rec.get('old_home', ''))
        remaining = []
        for f in rec.get('files', []):
            try:
                Path(f).unlink(missing_ok=True)
            except OSError:
                remaining.append(f)
        # 空目录自下而上修剪；含占用文件的骨架留下次
        if old_home.is_dir():
            for d in sorted((d for d in old_home.rglob('*') if d.is_dir()),
                            key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            try:
                old_home.rmdir()
            except OSError:
                pass
        if remaining:
            self.app_db.set_setting('pending_cleanup', json.dumps(
                {'old_home': str(old_home), 'files': remaining}))
            self.logger.warning(
                f'迁移残留清理：仍有 {len(remaining)} 个文件被占用，下次启动重试')
        else:
            self.app_db.set_setting('pending_cleanup', '')
            self.logger.info(f'迁移残留已清理: {old_home}')

    # ---- API 日志（移植自旧 main.py::_on_api_log）----

    _api_log_lock = None
    # api.log 超过该尺寸滚动为 api.log.1（只保留两代，防长期开启撑爆磁盘）
    _API_LOG_MAX_BYTES = 5 * 1024 * 1024

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
                if (log_path.exists()
                        and log_path.stat().st_size > self._API_LOG_MAX_BYTES):
                    log_path.replace(log_path.with_name(log_path.name + '.1'))
                with open(log_path, 'a', encoding='utf-8') as f:
                    f.write(entry)
            except OSError as e:
                print(f'[API日志] 写入失败: {e}')
