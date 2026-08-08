"""配置 API：模型配置 CRUD、连接测试、SDK 路径/下载、数据目录迁移"""
import asyncio
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import get_state
from ..errors import ApiError
from ..state import AppState

router = APIRouter(tags=['configs'])


class ConfigIn(BaseModel):
    name: str
    api_base: str = 'https://api.openai.com/v1'
    api_key: str = ''
    model: str = 'gpt-3.5-turbo'
    temperature: float = 0.3
    max_tokens: int = 1000
    context_lines: int = 3
    timeout: int = 30
    max_context: int = 8
    batch_lines: int = 100


class SettingsIn(BaseModel):
    sdk_path: str = ''


def _mask(cfg) -> dict:
    d = asdict(cfg)
    d['api_key'] = '***' if cfg.api_key else ''
    return d


def _to_model_config(req: ConfigIn):
    from config_manager import ModelConfig
    return ModelConfig(
        name=req.name.strip(), api_base=req.api_base, api_key=req.api_key,
        model=req.model, temperature=req.temperature,
        max_tokens=req.max_tokens, context_lines=req.context_lines,
        timeout=req.timeout, max_context=req.max_context,
        batch_lines=req.batch_lines,
    )


@router.get('/configs')
async def list_configs(state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    configs = await loop.run_in_executor(None, state.config_manager.load_all_configs)
    return [_mask(c) for c in configs]


@router.post('/configs')
async def add_config(req: ConfigIn, state: AppState = Depends(get_state)):
    if not req.name.strip():
        raise ApiError(400, 'BAD_NAME', '配置名称不能为空')
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(
            None, state.config_manager.get_config_by_name, req.name.strip()):
        raise ApiError(409, 'NAME_EXISTS', f'配置已存在: {req.name}')
    ok = await loop.run_in_executor(
        None, state.config_manager.add_config, _to_model_config(req))
    if not ok:
        raise ApiError(500, 'SAVE_FAILED', '配置保存失败')
    return {'ok': True}


@router.put('/configs/{name}')
async def update_config(name: str, req: ConfigIn,
                        state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    cfg = _to_model_config(req)
    if req.api_key == '***':
        # 掩码原样提交 = 未修改，保留旧 key
        old = await loop.run_in_executor(
            None, state.config_manager.get_config_by_name, name)
        cfg.api_key = old.api_key if old else ''
    ok = await loop.run_in_executor(
        None, state.config_manager.update_config, name, cfg)
    if not ok:
        raise ApiError(404, 'NOT_FOUND', f'配置不存在: {name}')
    return {'ok': True}


@router.delete('/configs/{name}')
async def delete_config(name: str, state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        None, state.config_manager.delete_config, name)
    if not ok:
        raise ApiError(404, 'NOT_FOUND', f'配置不存在: {name}')
    return {'ok': True}


@router.post('/configs/test')
async def test_config(req: ConfigIn):
    """测试连接（用表单里的配置临时建翻译器；api_key='***' 拒绝——无从测试）"""
    if req.api_key == '***':
        raise ApiError(400, 'MASKED_KEY', '请先重新输入 API Key 再测试（或保存后测试）')
    from translator import AITranslator, TranslationConfig
    translator = AITranslator(TranslationConfig(
        api_base=req.api_base, api_key=req.api_key, model=req.model,
        temperature=req.temperature, max_tokens=req.max_tokens,
        context_lines=req.context_lines, timeout=req.timeout,
    ))
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, translator.test_connection)
    if not result.get('success'):
        raise ApiError(502, 'TEST_FAILED', result.get('error', '连接测试失败'))
    return result


# ---- 全局设置（sdk_path 等，存 data/app.db settings 表） ----

@router.get('/settings')
async def get_settings(state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    sdk_path = await loop.run_in_executor(
        None, state.app_db.get_setting, 'sdk_path', '')
    if not sdk_path:
        # 回退：旧版 sdk_path 存在模型配置里，读第一个有的
        configs = await loop.run_in_executor(
            None, state.config_manager.load_all_configs)
        for c in configs:
            if c.sdk_path:
                sdk_path = c.sdk_path
                break
    from rt_home import exe_dir, home
    return {'sdk_path': sdk_path, 'data_dir': str(home()),
            'exe_dir': str(exe_dir())}


class DataDirIn(BaseModel):
    path: str


@router.put('/settings/data-dir')
async def migrate_data_dir(req: DataDirIn, state: AppState = Depends(get_state)):
    """迁移数据目录并切换（应用内自定义；长时间操作，前端显示迁移中模态）

    有被占用文件残留时自动重启服务：重启后旧句柄释放，启动流程完成清理。
    """
    if not req.path.strip():
        raise ApiError(400, 'EMPTY_PATH', '数据目录不能为空')
    result = await state.relocate_home(Path(req.path.strip()))
    if result.get('leftover'):
        from .system import schedule_restart
        result['restarting'] = True
        schedule_restart(state)
    return result


@router.put('/settings')
async def put_settings(req: SettingsIn, state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, state.app_db.set_setting, 'sdk_path', req.sdk_path)
    return {'ok': True}


class SdkPathIn(BaseModel):
    path: str = ''


@router.post('/settings/sdk/find')
async def find_sdk(state: AppState = Depends(get_state)):
    loop = asyncio.get_event_loop()
    found = await loop.run_in_executor(None, state.sdk_manager.find_sdk)
    if not found:
        raise ApiError(404, 'SDK_NOT_FOUND',
                       '未找到 Ren\'Py SDK，请手动指定路径')
    return {'sdk_path': str(found)}


@router.post('/settings/sdk/test')
async def test_sdk(req: SdkPathIn, state: AppState = Depends(get_state)):
    if not req.path:
        raise ApiError(400, 'EMPTY_PATH', 'SDK 路径为空')
    loop = asyncio.get_event_loop()
    valid = await loop.run_in_executor(
        None, state.sdk_manager._is_valid_sdk, Path(req.path))
    if not valid:
        raise ApiError(400, 'INVALID_SDK',
                       '无效的 SDK 路径（未找到 renpy 可执行文件）')
    return {'ok': True}


class SdkDownloadIn(BaseModel):
    version: str = '8.5.3'


def _sdk_package(version: str) -> tuple:
    """按平台/架构选择 SDK 包与解压方式（官网 dl 目录格式）"""
    import platform
    import sys
    if sys.platform == 'win32':
        return f'renpy-{version}-sdk.zip', 'zip'
    if sys.platform == 'darwin':
        return f'renpy-{version}-sdk.dmg', 'dmg'
    if platform.machine().lower() in ('aarch64', 'arm64'):
        return f'renpy-{version}-sdkarm.tar.bz2', 'tarbz2'
    return f'renpy-{version}-sdk.tar.bz2', 'tarbz2'


@router.post('/settings/sdk/download')
async def download_sdk(req: SdkDownloadIn, state: AppState = Depends(get_state)):
    """一键下载 Ren'Py SDK（任务：流式下载进度 → 解压 → 设为 sdk_path）

    下载/解压循环都响应取消；关键步骤同时写服务端日志（开发控制台可见）。
    """
    import re
    import sys
    if not re.fullmatch(r'\d+\.\d+\.\d+', req.version):
        raise ApiError(400, 'BAD_VERSION', '版本号格式应为 x.y.z（如 8.5.3）')
    version = req.version

    async def body(job):
        import tarfile
        import urllib.request
        import zipfile
        from rt_home import home
        from ..jobs import JobCancelled

        def log(msg):
            job.emit_log(msg)
            state.logger.info(msg, panel='settings')

        fname, kind = _sdk_package(version)
        url = f'https://www.renpy.org/dl/{version}/{fname}'
        tools_dir = home() / 'tools'
        tools_dir.mkdir(parents=True, exist_ok=True)
        tmp_pkg = tools_dir / fname
        sdk_dir = tools_dir / f'renpy-{version}-sdk'
        loop = asyncio.get_event_loop()

        # ---- 下载（可取消） ----
        log(f'下载 {url}')
        try:
            def _download():
                with urllib.request.urlopen(url, timeout=60) as resp:
                    total = int(resp.headers.get('Content-Length') or 0)
                    if not total:
                        raise RuntimeError('服务器未返回文件大小（下载链接可能失效）')
                    log(f'文件大小 {total >> 20} MB，开始下载')
                    with open(tmp_pkg, 'wb') as f:
                        downloaded = 0
                        while True:
                            if job.cancel_event.is_set():
                                raise JobCancelled()
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            job.emit_progress(
                                downloaded / total * 0.7,
                                f'正在下载 SDK... '
                                f'{downloaded >> 20}/{total >> 20} MB')

            await loop.run_in_executor(None, _download)
        except BaseException:
            tmp_pkg.unlink(missing_ok=True)
            raise
        log('下载完成')

        # ---- 解压（可取消，逐步进度） ----
        if sdk_dir.exists():
            shutil.rmtree(sdk_dir)

        def _extract_zip():
            with zipfile.ZipFile(tmp_pkg) as zf:
                infos = zf.infolist()
                total = len(infos)
                for i, info in enumerate(infos, 1):
                    if i % 25 == 0 and job.cancel_event.is_set():
                        raise JobCancelled()
                    zf.extract(info, tools_dir)
                    job.emit_progress(0.7 + (i / total) * 0.3,
                                      f'正在解压... ({i}/{total})')

        def _extract_tarbz2():
            with tarfile.open(tmp_pkg, 'r:bz2') as tf:
                members = tf.getmembers()
                total = len(members)
                for i, m in enumerate(members, 1):
                    if i % 25 == 0 and job.cancel_event.is_set():
                        raise JobCancelled()
                    tf.extract(m, tools_dir, filter='data')
                    job.emit_progress(0.7 + (i / total) * 0.3,
                                      f'正在解压... ({i}/{total})')

        def _extract_dmg():
            import subprocess
            import tempfile
            mp = Path(tempfile.mkdtemp())
            log('挂载 dmg...')
            subprocess.run(
                ['hdiutil', 'attach', '-nobrowse', '-mountpoint', str(mp),
                 str(tmp_pkg)], check=True, capture_output=True)
            try:
                apps = list(mp.glob('*.app'))
                if not apps:
                    raise RuntimeError('dmg 中未找到 .app')
                job.emit_progress(0.8, f'正在拷贝 {apps[0].name}...')
                sdk_dir.mkdir(parents=True, exist_ok=True)
                subprocess.run(['cp', '-a', str(apps[0]), str(sdk_dir) + '/'],
                               check=True)
                # 去掉下载隔离属性，避免 Gatekeeper 拦截
                subprocess.run(['xattr', '-dr', 'com.apple.quarantine',
                                str(sdk_dir)], capture_output=True)
            finally:
                subprocess.run(['hdiutil', 'detach', str(mp)],
                               capture_output=True)
                shutil.rmtree(mp, ignore_errors=True)

        log(f'正在解压（{kind}）...')
        try:
            await loop.run_in_executor(
                None, {'zip': _extract_zip, 'tarbz2': _extract_tarbz2,
                       'dmg': _extract_dmg}[kind])
        except BaseException:
            shutil.rmtree(sdk_dir, ignore_errors=True)
            tmp_pkg.unlink(missing_ok=True)
            raise
        tmp_pkg.unlink(missing_ok=True)
        log('解压完成')

        if not state.sdk_manager._is_valid_sdk(sdk_dir):
            raise RuntimeError(
                f'解压后未找到有效 SDK: {sdk_dir}（缺 renpy 可执行文件）')
        await loop.run_in_executor(
            None, state.app_db.set_setting, 'sdk_path', str(sdk_dir))
        log(f'SDK 就绪: {sdk_dir}')
        job.emit_progress(1.0, 'SDK 就绪')
        return {'sdk_path': str(sdk_dir)}

    job = state.jobs.create('settings.sdk-download',
                            f'下载 Ren\'Py SDK {version}', {'version': version}, body)
    return {'job_id': job.id}
