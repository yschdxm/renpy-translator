"""项目 API：列表/编辑/删除/项目包 + 创建/导入/导出 zip（任务系统）"""
import asyncio
import shutil
import tempfile
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


def _exports_dir(state: AppState) -> Path:
    """项目包导出目录（随数据根，冻结/迁移后不错位）"""
    d = Path(state.root) / 'exports'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _uploads_dir(state: AppState) -> Path:
    """上传暂存目录（应用临时根下，服务启动时自动清空，见 AppState.startup）"""
    d = state.temp_dir / 'uploads'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sdk_path_getter(state: AppState):
    def _get():
        sdk_path = state.app_db.get_setting('sdk_path', '')
        if not sdk_path:
            for c in state.config_manager.load_all_configs():
                if c.sdk_path:
                    return c.sdk_path
        return sdk_path
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

        try:
            # zip 来源：先解压（逐项计数进度）
            if 'zip_path' in game_dir_holder:
                job.emit_progress(0.01, '正在解压游戏文件...')
                extract_dir = Path(game_dir_holder['zip_path']).parent / 'extracted'
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)

                def _extract():
                    return extract_game_zip(
                        game_dir_holder['zip_path'], extract_dir,
                        lambda c, t: job.emit_progress(
                            0.01 + (c / max(t, 1)) * 0.04,
                            f'正在解压游戏文件... ({c}/{t})'))
                game_dir_holder['dir'] = await loop.run_in_executor(None, _extract)
                job.emit_progress(0.05, '解压完成')

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

            result = await creator.create(
                name, game_dir, model,
                progress=lambda p, t: job.emit_progress(p, t),
                confirm_official_chinese=_confirm,
            )
            if result.get('cancelled'):
                from ..jobs import JobCancelled
                raise JobCancelled()
            return result
        except Exception:
            # 失败清理半成品项目目录（取消路径 creator 已自行删除）
            await loop.run_in_executor(
                None, state.project_manager.delete_project, name)
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

    import uuid
    zip_path = _uploads_dir(state) / f'{uuid.uuid4().hex[:8]}.zip'
    await _save_upload(file, zip_path)

    job = state.jobs.create(
        'project.create', f'创建项目 {name}',
        {'name': name, 'zip': file.filename, 'model': model},
        _make_create_body(state, name, model,
                          {'zip_path': str(zip_path)},
                          [zip_path, zip_path.parent / 'extracted']))
    return {'job_id': job.id}


@router.post('/import')
async def import_project(file: UploadFile = File(...), name: str = Form(''),
                         state: AppState = Depends(get_state)):
    import uuid
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


@router.post('/{name}/export-zip')
async def export_zip(name: str, state: AppState = Depends(get_state)):
    if not state.project_manager.project_exists(name):
        raise ApiError(404, 'PROJECT_NOT_FOUND', f'项目不存在: {name}')

    async def body(job):
        loop = asyncio.get_event_loop()
        project_dir = state.project_manager._get_project_dir(name)
        db_file = project_dir / 'project.db'
        if not db_file.exists():
            raise RuntimeError('项目数据库不存在')

        export_path = _exports_dir(state) / f'{name}.zip'

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_project = Path(temp_dir) / name
            temp_project.mkdir()

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
                                shutil.copy2(Path(root) / f, dst / f)
                                copy_progress['current'] += 1
                    finally:
                        copy_progress['done'] = True

                copy_task = loop.run_in_executor(None, _copy_game)
                while not copy_progress['done']:
                    total, current = copy_progress['total'], copy_progress['current']
                    if total > 0:
                        job.emit_progress(0.1 + (current / total) * 0.4,
                                          f'正在复制游戏文件... ({current}/{total})')
                    await asyncio.sleep(0.3)
                await copy_task

            job.emit_progress(0.6, '正在打包...')

            def _create_zip():
                all_files = []
                for root, dirs, files in __import__('os').walk(temp_project):
                    for f in files:
                        all_files.append(
                            (Path(root) / f, Path(root).relative_to(temp_dir)))
                with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for file_path, arc_dir in all_files:
                        zf.write(file_path, arc_dir / file_path.name)

            await loop.run_in_executor(None, _create_zip)

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
    exports_dir = _exports_dir(state)
    packages = []
    for zf in sorted(exports_dir.glob(f'{name}*.zip'),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        st = zf.stat()
        packages.append({'file': zf.name, 'size': st.st_size,
                         'mtime': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds')})
    return packages


@router.get('/{name}/packages/{file}')
async def download_package(name: str, file: str,
                           state: AppState = Depends(get_state)):
    exports_dir = _exports_dir(state)
    target = (exports_dir / file).resolve()
    if not str(target).startswith(str(exports_dir.resolve())) or not target.is_file():
        raise ApiError(404, 'NOT_FOUND', '项目包不存在')
    return FileResponse(target, filename=file)
