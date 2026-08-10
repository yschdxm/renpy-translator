"""项目 API：列表/编辑/删除/项目包 + 创建/导入/导出 zip（任务系统）"""
import asyncio
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..deps import get_state
from ..errors import ApiError
from ..state import AppState

router = APIRouter(prefix='/projects', tags=['projects'])


def _exports_dir(state: AppState, name: str = '') -> Path:
    """导出目录（随数据根）。传 name 时按项目分目录：exports/{name}/

    name 来自路由参数，先做字符白名单校验（与 ProjectManager._get_project_dir
    一致），防目录穿越（如 '..\\config'）；非法名直接抛 400。
    """
    d = Path(state.root) / 'exports'
    if name:
        safe = ''.join(c for c in name if c.isalnum() or c in '._- ').strip()
        if safe != name or safe in ('.', '..'):
            raise ApiError(400, 'BAD_NAME', f'非法项目名: {name}')
        d = d / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_flat_packages(state: AppState, name: str):
    """旧版平铺在 exports/ 下的包迁移到 exports/{name}/（项目包补 -project 后缀）"""
    base = Path(state.root) / 'exports'
    target_dir = _exports_dir(state, name)
    for zf in list(base.glob(f'{name}.zip')) + list(base.glob(f'{name}-*.zip')):
        new_name = zf.name if zf.name != f'{name}.zip' else f'{name}-project.zip'
        try:
            zf.rename(target_dir / new_name)
        except OSError:
            pass  # 占用/冲突时跳过，下次再迁


def _uploads_dir(state: AppState) -> Path:
    """上传暂存目录（应用临时根下，服务启动时自动清空，见 AppState.startup）"""
    d = state.temp_dir / 'uploads'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sdk_path_getter(state: AppState):
    """按游戏目录解析匹配引擎大版本的 SDK（7/8 双槽位，含旧配置回退）"""
    def _get(game_dir=None):
        return state.resolve_sdk_path(game_dir)
    return _get


class CreateRequest(BaseModel):
    name: str
    game_dir: str
    model: str = ''


def _make_create_body(state: AppState, name: str, model: str,
                      game_dir_holder: dict, cleanup_paths: list):
    """建项目任务体：game_dir_holder['dir'] 在任务内解析（可能先解压 zip）"""
    async def body(job):
        from services.project_creation import ProjectCreator, extract_game_zip
        loop = asyncio.get_event_loop()
        # 进场时记录项目是否已存在：except 清理只删本任务创建的项目，
        # 避免并发同名建项时误删对方正在创建的项目目录
        pre_existing = state.project_manager.project_exists(name)

        try:
            # zip 来源：先解压（逐项计数进度）
            if 'zip_path' in game_dir_holder:
                job.emit_progress(0.01, '正在解压游戏文件...')
                # 解压目录名带 uuid：并发任务共用固定目录会互相 rmtree
                extract_dir = (Path(game_dir_holder['zip_path']).parent
                               / f'extracted-{uuid.uuid4().hex[:8]}')
                cleanup_paths.append(extract_dir)  # 交给 finally 统一清理

                def _extract():
                    return extract_game_zip(
                        game_dir_holder['zip_path'], extract_dir,
                        lambda c, t: job.emit_progress(
                            0.01 + (c / max(t, 1)) * 0.04,
                            f'正在解压游戏文件... ({c}/{t})'))
                game_dir_holder['dir'] = await loop.run_in_executor(None, _extract)
                job.emit_progress(0.05, '解压完成')

            job.check_cancelled()
            game_dir = game_dir_holder['dir']
            if not Path(game_dir).exists():
                raise RuntimeError(f'游戏目录不存在: {game_dir}')

            creator = ProjectCreator(state.project_manager, state.logger,
                                     _sdk_path_getter(state))

            async def _confirm(count: int) -> bool:
                ans = await job.ask('confirm', {
                    'title': '检测到游戏自带中文翻译',
                    'body': f'该游戏的 tl 目录下已存在中文翻译（{count} 个文件），'
                            '可能是官方中文或第三方汉化。\n\n'
                            '如果保留，其中的译文会与 SDK 生成的待翻译模板重复导入，'
                            '导致对话条目翻倍、翻译混乱。\n\n'
                            '建议删除该中文目录，使用 SDK 模板重新翻译。',
                })
                return bool(ans.get('ok'))

            # 进度回调即取消检查点：复制完成、SDK 生成模板、解析入库等
            # 各阶段之间都会经过这里，取消后不会继续把项目建完
            def _progress(p, t):
                job.check_cancelled()
                job.emit_progress(p, t)

            job.check_cancelled()
            result = await creator.create(
                name, game_dir, model,
                progress=_progress,
                confirm_official_chinese=_confirm,
                cancel_event=job.cancel_event,
            )
            if result.get('cancelled'):
                from ..jobs import JobCancelled
                raise JobCancelled()
            return result
        except Exception as e:
            # 失败/取消（JobCancelled 也是 Exception）都清掉半成品项目目录，
            # 但只清本任务创建的：进场前项目已存在，或失败原因正是"项目已存在"
            # （create_project 抛 ValueError），说明目录属于并发任务，不能删
            name_taken = isinstance(e, ValueError) and '已存在' in str(e)
            if not pre_existing and not name_taken:
                await loop.run_in_executor(
                    None, state.project_manager.delete_project, name)
            # SDK 中止等下游取消表现为普通异常：统一转成 JobCancelled，
            # 让任务状态正确落为 cancelled 而非 failed
            from ..jobs import JobCancelled
            if job.cancel_event.is_set() and not isinstance(e, JobCancelled):
                raise JobCancelled() from e
            raise
        finally:
            for p in cleanup_paths:
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
    return body


@router.post('/create')
async def create_project(req: CreateRequest,
                         state: AppState = Depends(get_state)):
    name = req.name.strip()
    if not name:
        raise ApiError(400, 'BAD_NAME', '请填写项目名称')
    if not req.game_dir:
        raise ApiError(400, 'BAD_DIR', '请填写游戏目录')
    if state.project_manager.project_exists(name):
        raise ApiError(409, 'NAME_EXISTS', f'项目已存在: {name}')

    job = state.jobs.create(
        'project.create', f'创建项目 {name}',
        {'name': name, 'game_dir': req.game_dir, 'model': req.model},
        _make_create_body(state, name, req.model,
                          {'dir': req.game_dir}, []))
    return {'job_id': job.id}


async def _save_upload(file: UploadFile, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)


@router.post('/create-zip')
async def create_project_zip(file: UploadFile = File(...),
                             name: str = Form(...),
                             model: str = Form(''),
                             state: AppState = Depends(get_state)):
    name = name.strip()
    if not name:
        raise ApiError(400, 'BAD_NAME', '请填写项目名称')
    if state.project_manager.project_exists(name):
        raise ApiError(409, 'NAME_EXISTS', f'项目已存在: {name}')

    zip_path = _uploads_dir(state) / f'{uuid.uuid4().hex[:8]}.zip'
    await _save_upload(file, zip_path)

    job = state.jobs.create(
        'project.create', f'创建项目 {name}',
        {'name': name, 'zip': file.filename, 'model': model},
        _make_create_body(state, name, model,
                          {'zip_path': str(zip_path)},
                          [zip_path]))  # 解压目录由任务体生成（带 uuid）并自行登记清理
    return {'job_id': job.id}


@router.post('/import')
async def import_project(file: UploadFile = File(...), name: str = Form(''),
                         state: AppState = Depends(get_state)):
    import re
    # 自动命名去掉导出后缀：Foo-project.zip / Foo-translated.zip → Foo
    name = re.sub(r'-(project|translated)$', '', name.strip())
    zip_path = _uploads_dir(state) / f'{uuid.uuid4().hex[:8]}.zip'
    await _save_upload(file, zip_path)

    async def body(job):
        loop = asyncio.get_event_loop()
        job.emit_progress(0.2, '正在解压项目包...')

        def _do_import():
            with tempfile.TemporaryDirectory() as temp_dir:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(temp_dir)
                return state.project_manager.import_from_zip(
                    temp_dir, name or None)

        try:
            result = await loop.run_in_executor(None, _do_import)
        finally:
            zip_path.unlink(missing_ok=True)  # 异常也要清掉上传暂存
        if not result['success']:
            raise RuntimeError(result['message'])
        job.emit_progress(1.0, result['message'])
        return result

    job = state.jobs.create('project.import', f'导入项目 {name or file.filename}',
                            {}, body)
    return {'job_id': job.id}


class UpdateRequest(BaseModel):
    game_dir: str


def _check_update_allowed(state: AppState, name: str):
    """更新前置校验：项目存在 + 无活动任务占用（翻译/导出中更新会丢数据）

    同一时刻只有一个打开的项目，翻译/导出/内嵌等任务都作用在它上面，
    但它们的 payload 不一定带项目名——所以不按 payload 匹配项目，
    只要存在 running/waiting_input 任务就拒绝更新。
    （本校验在创建更新任务之前执行，不会把更新任务自己误判为占用。）
    """
    if not state.project_manager.project_exists(name):
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')
    active = state.app_db.list_jobs(active_only=True)
    if active:
        rec = active[0]
        raise ApiError(
            409, 'PROJECT_BUSY',
            f'有正在进行的任务（{rec.get("label") or rec["kind"]}），'
            '请等待完成或取消后再更新')


def _make_update_body(state: AppState, name: str,
                      game_dir_holder: dict, cleanup_paths: list):
    """版本更新任务体：game_dir_holder['dir'] 在任务内解析（可能先解压 zip）

    失败/取消由 ProjectUpdater 内部回滚——这里绝不 delete_project。
    """
    async def body(job):
        from services.project_creation import extract_game_zip
        from services.project_update import ProjectUpdater
        from ..jobs import JobCancelled
        loop = asyncio.get_event_loop()

        was_current = name == state.current_project
        try:
            # zip 来源：先解压（逐项计数进度）
            if 'zip_path' in game_dir_holder:
                job.emit_progress(0.005, '正在解压游戏文件...')
                # 解压目录名带 uuid：并发任务共用固定目录会互相 rmtree
                extract_dir = (Path(game_dir_holder['zip_path']).parent
                               / f'extracted-{uuid.uuid4().hex[:8]}')
                cleanup_paths.append(extract_dir)  # 交给 finally 统一清理

                def _extract():
                    return extract_game_zip(
                        game_dir_holder['zip_path'], extract_dir,
                        lambda c, t: job.emit_progress(
                            0.005 + (c / max(t, 1)) * 0.035,
                            f'正在解压游戏文件... ({c}/{t})'))
                game_dir_holder['dir'] = await loop.run_in_executor(None, _extract)
                job.emit_progress(0.04, '解压完成')

            game_dir = game_dir_holder['dir']
            if not Path(game_dir).exists():
                raise RuntimeError(f'游戏目录不存在: {game_dir}')

            # 更新会替换 game/ 与重建 db——当前打开的项目先关库（Windows 文件锁）
            if was_current:
                await state.close_project()

            async def _confirm(count: int) -> bool:
                ans = await job.ask('confirm', {
                    'title': '新版本自带中文翻译',
                    'body': f'新版本的 tl 目录下已存在中文翻译（{count} 个文件），'
                            '可能是官方中文或第三方汉化。\n\n'
                            '如果保留，其中的译文会与继承的译文及 SDK 模板重复，'
                            '导致对话条目翻倍、翻译混乱。\n\n'
                            '建议删除该中文目录，继续使用现有翻译。',
                })
                return bool(ans.get('ok'))

            updater = ProjectUpdater(state.project_manager, state.logger,
                                     _sdk_path_getter(state))

            def _progress(p, t):
                job.check_cancelled()
                job.emit_progress(0.04 + p * 0.96, t)

            result = await updater.update(
                name, game_dir,
                progress=_progress,
                confirm_official_chinese=_confirm,
                cancel_event=job.cancel_event,
            )
            if result.get('cancelled'):
                raise JobCancelled()
            return result
        except Exception as e:
            # SDK 中止等下游取消表现为普通异常：统一转成 JobCancelled，
            # 让任务状态正确落为 cancelled 而非 failed
            if job.cancel_event.is_set() and not isinstance(e, JobCancelled):
                raise JobCancelled() from e
            raise
        finally:
            # 更新前是当前项目的，无论成败都重新打开（失败时回滚已恢复原状）
            if was_current and state.project_manager.project_exists(name):
                try:
                    await state.open_project(name)
                except Exception:
                    pass
            for p in cleanup_paths:
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
    return body


@router.post('/{name}/update')
async def update_project(name: str, req: UpdateRequest,
                         state: AppState = Depends(get_state)):
    _check_update_allowed(state, name)
    if not req.game_dir:
        raise ApiError(400, 'BAD_DIR', '请填写新版本游戏目录')

    job = state.jobs.create(
        'project.update', f'更新项目 {name}',
        {'name': name, 'game_dir': req.game_dir},
        _make_update_body(state, name, {'dir': req.game_dir}, []))
    return {'job_id': job.id}


@router.post('/{name}/update-zip')
async def update_project_zip(name: str, file: UploadFile = File(...),
                             state: AppState = Depends(get_state)):
    _check_update_allowed(state, name)

    zip_path = _uploads_dir(state) / f'{uuid.uuid4().hex[:8]}.zip'
    await _save_upload(file, zip_path)

    job = state.jobs.create(
        'project.update', f'更新项目 {name}',
        {'name': name, 'zip': file.filename},
        _make_update_body(state, name,
                          {'zip_path': str(zip_path)},
                          [zip_path]))  # 解压目录由任务体生成（带 uuid）并自行登记清理
    return {'job_id': job.id}


@router.get('/{name}/update-report')
async def update_report(name: str, state: AppState = Depends(get_state)):
    """最近一次版本更新的报告 + 失效译文 + 复核列表"""
    if not state.project_manager.project_exists(name):
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')
    loop = asyncio.get_event_loop()

    def _load():
        import json as _json
        db = state.project_manager.open_project(name)
        if not db:
            return None
        raw = db.get_meta('last_update_report', '')
        report = _json.loads(raw) if raw else None
        obsolete = db.get_obsolete()
        review = db.get_update_review()
        db.close()
        return report, obsolete, review

    loaded = await loop.run_in_executor(None, _load)
    if loaded is None:
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')
    report, obsolete, review = loaded
    return {'report': report, 'obsolete': obsolete, 'review': review}


class ReviewActionRequest(BaseModel):
    action: str  # 'apply' | 'dismiss'


@router.post('/{name}/update-review/{row_id}')
async def update_review_action(name: str, row_id: int, req: ReviewActionRequest,
                               state: AppState = Depends(get_state)):
    """复核行操作：apply 把旧译文写入目标条目并标记已译；dismiss 忽略"""
    if not state.project_manager.project_exists(name):
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')
    if req.action not in ('apply', 'dismiss'):
        raise ApiError(400, 'BAD_ACTION', 'action 必须是 apply 或 dismiss')
    loop = asyncio.get_event_loop()

    def _apply():
        db = state.project_manager.open_project(name)
        if not db:
            return None
        rows = [r for r in db.get_update_review() if r['id'] == row_id]
        if not rows:
            db.close()
            return 'not_found'
        row = rows[0]
        if req.action == 'apply':
            if row['target_kind'] == 'dialogue':
                db.update_dialogue(row['target_id'], row['old_translation'])
            else:
                db.update_ui_text(row['target_id'], row['old_translation'])
            db.set_review_status(row_id, 'applied')
        else:
            db.set_review_status(row_id, 'dismissed')
        db.close()
        return 'ok'

    result = await loop.run_in_executor(None, _apply)
    if result is None:
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')
    if result == 'not_found':
        raise ApiError(404, 'REVIEW_NOT_FOUND', f'复核条目不存在: {row_id}')
    return {'ok': True}


@router.post('/{name}/export-zip')
async def export_zip(name: str, state: AppState = Depends(get_state)):
    if not state.project_manager.project_exists(name):
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')

    async def body(job):
        from ..jobs import JobCancelled
        loop = asyncio.get_event_loop()
        project_dir = state.project_manager._get_project_dir(name)
        db_file = project_dir / 'project.db'
        if not db_file.exists():
            raise RuntimeError('项目数据库不存在')

        export_path = _exports_dir(state, name) / f'{name}-project.zip'

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_project = Path(temp_dir) / name
                temp_project.mkdir()

                job.check_cancelled()
                job.emit_progress(0.1, '正在复制数据库...')
                await loop.run_in_executor(
                    None, shutil.copy2, db_file, temp_project / 'project.db')

                game_dir = project_dir / 'game'
                if game_dir.exists():
                    copy_progress = {'current': 0, 'total': 0, 'done': False}

                    def _copy_game():
                        try:
                            total = sum(1 for _ in game_dir.rglob('*') if _.is_file())
                            copy_progress['total'] = total if total > 0 else 1
                            for root, dirs, files in __import__('os').walk(game_dir):
                                rel = Path(root).relative_to(game_dir)
                                dst = temp_project / 'game' / rel
                                dst.mkdir(parents=True, exist_ok=True)
                                for f in files:
                                    job.check_cancelled()
                                    shutil.copy2(Path(root) / f, dst / f)
                                    copy_progress['current'] += 1
                        finally:
                            copy_progress['done'] = True

                    copy_task = loop.run_in_executor(None, _copy_game)
                    while not copy_progress['done']:
                        job.check_cancelled()
                        total, current = copy_progress['total'], copy_progress['current']
                        if total > 0:
                            job.emit_progress(0.1 + (current / total) * 0.4,
                                              f'正在复制游戏文件... ({current}/{total})')
                        await asyncio.sleep(0.3)
                    await copy_task

                job.check_cancelled()
                job.emit_progress(0.6, '正在打包...')

                def _create_zip():
                    all_files = []
                    for root, dirs, files in __import__('os').walk(temp_project):
                        for f in files:
                            all_files.append(
                                (Path(root) / f, Path(root).relative_to(temp_dir)))
                    total = len(all_files) or 1
                    with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for i, (file_path, arc_dir) in enumerate(all_files, 1):
                            job.check_cancelled()
                            zf.write(file_path, arc_dir / file_path.name)
                            if i % 10 == 0 or i == total:
                                job.emit_progress(
                                    0.6 + (i / total) * 0.38,
                                    f'正在打包... ({i}/{total})')

                await loop.run_in_executor(None, _create_zip)
        except JobCancelled:
            # 删除半成品 zip，避免被当成完整项目包下载
            export_path.unlink(missing_ok=True)
            raise

        job.emit_progress(1.0, f'导出成功: {export_path.name}')
        return {'file': export_path.name, 'path': str(export_path)}

    job = state.jobs.create('project.export-zip', f'导出项目包 {name}', {}, body)
    return {'job_id': job.id}


class EditRequest(BaseModel):
    new_name: str = ''
    model: str = ''


@router.get('')
async def list_projects(state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    projects = await loop.run_in_executor(None, state.project_manager.list_projects)
    return [{
        **asdict(p),
        'progress_percent': p.progress_percent,
        'progress_text': p.progress_text,
        'is_current': p.name == state.current_project,
    } for p in projects]


@router.get('/{name}/meta')
async def project_meta(name: str, state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()

    def _load():
        db = state.project_manager.open_project(name)
        if not db:
            return None
        meta = db.get_all_meta()
        db.close()
        return meta

    meta = await loop.run_in_executor(None, _load)
    if not meta:
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')
    return {'name': meta.get('name', name),
            'model_config_name': meta.get('model_config_name', '')}


@router.patch('/{name}')
async def edit_project(name: str, req: EditRequest,
                       state: AppState = Depends(get_state)):
    new_name = (req.new_name or name).strip()
    if not new_name:
        raise ApiError(400, 'BAD_NAME', '项目名称不能为空')

    loop = asyncio.get_event_loop()

    # 重命名当前打开的项目：先关库（Windows 目录改名要求无占用）
    was_current = name == state.current_project
    if was_current:
        await state.close_project()

    if name != new_name:
        if await loop.run_in_executor(
                None, state.project_manager.project_exists, new_name):
            raise ApiError(409, 'NAME_EXISTS', f'项目名称已存在: {new_name}')

        def _rename():
            old_dir = state.project_manager._get_project_dir(name)
            new_dir = state.project_manager._get_project_dir(new_name)
            old_dir.rename(new_dir)

        await loop.run_in_executor(None, _rename)

    def _update_meta():
        db = state.project_manager.open_project(new_name)
        if db:
            db.set_meta('name', new_name)
            db.set_meta('model_config_name', req.model or '')
            db.set_meta('updated_at', datetime.now().isoformat())
            db.close()

    await loop.run_in_executor(None, _update_meta)

    if was_current:
        await state.open_project(new_name)

    state.logger.info(f'项目已更新: {name} -> {new_name}', panel='projects')
    return {'ok': True, 'name': new_name}


@router.delete('/{name}')
async def delete_project(name: str, state: AppState = Depends(get_state)):
    """删除项目（任务：逐文件进度 + 失败响亮报错）"""
    if not state.project_manager.project_exists(name):
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')

    async def body(job):
        loop = asyncio.get_event_loop()
        # 若删除的是当前打开的项目，先关闭其数据库连接，避免文件被占用
        if name == state.current_project:
            await state.close_project()

        project_dir = state.project_manager._get_project_dir(name)

        def _delete():
            all_files = [f for f in project_dir.rglob('*') if f.is_file()]
            total = len(all_files) or 1
            for i, f in enumerate(all_files, 1):
                try:
                    f.unlink()
                except OSError:
                    pass
                if i % 20 == 0 or i == total:
                    job.emit_progress((i / total) * 0.9,
                                      f'正在删除... ({i}/{total})')
            shutil.rmtree(project_dir, ignore_errors=True)

        await loop.run_in_executor(None, _delete)
        if project_dir.exists():
            raise RuntimeError(f'项目目录删除失败（文件可能被占用）: {project_dir}')
        state.logger.info(f'项目已删除: {name}', panel='projects')
        job.emit_progress(1.0, '删除完成')
        return {'deleted': name}

    job = state.jobs.create('project.delete', f'删除项目 {name}',
                            {'name': name}, body)
    return {'job_id': job.id}


@router.get('/{name}/packages')
async def list_packages(name: str, state: AppState = Depends(get_state)):
    _migrate_flat_packages(state, name)
    exports_dir = _exports_dir(state, name)
    packages = []
    for zf in sorted(exports_dir.glob('*.zip'),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        st = zf.stat()
        packages.append({'file': zf.name, 'size': st.st_size,
                         'mtime': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')})
    return packages


@router.get('/{name}/packages/{file}')
async def download_package(name: str, file: str,
                           state: AppState = Depends(get_state)):
    exports_dir = _exports_dir(state, name).resolve()
    target = (exports_dir / file).resolve()
    # is_relative_to 按路径段比较，避免 startswith 误命中同前缀兄弟目录
    if not target.is_relative_to(exports_dir) or not target.is_file():
        raise ApiError(404, 'NOT_FOUND', '项目包不存在')
    return FileResponse(target, filename=file)


@router.post('/{name}/packages/reveal')
async def reveal_package(name: str, file: str = '',
                         state: AppState = Depends(get_state)):
    """在系统文件管理器中打开该项目的导出目录（传 file 时选中该文件）"""
    import subprocess
    import sys
    exports_dir = _exports_dir(state, name).resolve()
    target = exports_dir
    if file:
        target = (exports_dir / file).resolve()
        if not target.is_relative_to(exports_dir) or not target.is_file():
            raise ApiError(404, 'NOT_FOUND', '项目包不存在')

    if sys.platform.startswith('win'):
        # /select 选中文件；目录则直接打开
        if file:
            subprocess.Popen(['explorer', '/select,', str(target)])
        else:
            subprocess.Popen(['explorer', str(target)])
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', str(target)] if file
                         else ['open', str(target)])
    else:
        subprocess.Popen(['xdg-open', str(exports_dir)])
    return {'ok': True}
