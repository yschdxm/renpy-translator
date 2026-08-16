"""配置 API：模型配置 CRUD、连接测试、SDK 路径/下载、数据目录迁移"""
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


def _validate(req: 'ConfigIn'):
    # 不设上限：各家模型差异太大（deepseek-v4 输出可达 384K），
    # 超限时 API 的 400 报错会带合法范围，保存时只拦无意义的非正值
    if req.max_tokens < 1:
        raise ApiError(400, 'BAD_MAX_TOKENS', 'max_tokens 必须为正整数')


def _mask(cfg, active_name: str = '') -> dict:
    d = asdict(cfg)
    d['api_key'] = '***' if cfg.api_key else ''
    d['is_active'] = cfg.name == active_name
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
    cm = state.config_manager
    configs = await state.run_sync(cm.load_all_configs)
    return [_mask(c, cm.active_config_name) for c in configs]


@router.post('/configs/{name}/activate')
async def activate_config(name: str, state: AppState = Depends(get_state)):
    """激活指定模型配置（全局生效，所有项目共用）"""
    ok = await state.run_sync(state.config_manager.set_active, name)
    if not ok:
        raise ApiError(404, 'NOT_FOUND', f'配置不存在: {name}')
    await state.run_sync(state.refresh_translator)
    return {'ok': True}


@router.get('/configs/{name}/key')
async def reveal_key(name: str, state: AppState = Depends(get_state)):
    """读取配置的真实 API Key（编辑表单回显用；列表接口仍只给掩码）"""
    cfg = await state.run_sync(state.config_manager.get_config_by_name, name)
    if not cfg:
        raise ApiError(404, 'NOT_FOUND', f'配置不存在: {name}')
    return {'api_key': cfg.api_key}


@router.post('/configs')
async def add_config(req: ConfigIn, state: AppState = Depends(get_state)):
    _validate(req)
    if not req.name.strip():
        raise ApiError(400, 'BAD_NAME', '配置名称不能为空')
    if await state.run_sync(
            state.config_manager.get_config_by_name, req.name.strip()):
        raise ApiError(409, 'NAME_EXISTS', f'配置已存在: {req.name}')
    ok = await state.run_sync(state.config_manager.add_config, _to_model_config(req))
    if not ok:
        raise ApiError(500, 'SAVE_FAILED', '配置保存失败')
    await state.run_sync(state.refresh_translator)
    return {'ok': True}


@router.put('/configs/{name}')
async def update_config(name: str, req: ConfigIn,
                        state: AppState = Depends(get_state)):
    _validate(req)
    cfg = _to_model_config(req)
    if req.api_key == '***':
        # 掩码原样提交 = 未修改，保留旧 key
        old = await state.run_sync(state.config_manager.get_config_by_name, name)
        cfg.api_key = old.api_key if old else ''
    ok = await state.run_sync(state.config_manager.update_config, name, cfg)
    if not ok:
        raise ApiError(404, 'NOT_FOUND', f'配置不存在: {name}')
    # 保存后立即刷新当前会话的翻译器，否则仍持旧 key 调用 LLM
    await state.run_sync(state.refresh_translator)
    return {'ok': True}


@router.delete('/configs/{name}')
async def delete_config(name: str, state: AppState = Depends(get_state)):
    ok = await state.run_sync(state.config_manager.delete_config, name)
    if not ok:
        raise ApiError(404, 'NOT_FOUND', f'配置不存在: {name}')
    return {'ok': True}


@router.post('/configs/test')
async def test_config(req: ConfigIn, state: AppState = Depends(get_state)):
    """测试连接（用表单里的配置临时建翻译器；api_key='***' 时回退用已保存的 key）"""
    _validate(req)
    api_key = req.api_key
    if api_key == '***':
        old = await state.run_sync(
            state.config_manager.get_config_by_name, req.name.strip())
        if not old or not old.api_key:
            raise ApiError(400, 'MASKED_KEY', '该配置尚未保存过 API Key，请先输入再测试')
        api_key = old.api_key
    from translator import AITranslator, TranslationConfig
    translator = AITranslator(TranslationConfig(
        api_base=req.api_base, api_key=api_key, model=req.model,
        temperature=req.temperature, max_tokens=req.max_tokens,
        context_lines=req.context_lines, timeout=req.timeout,
    ))
    result = await state.run_sync(translator.test_connection)
    if not result.get('success'):
        raise ApiError(502, 'TEST_FAILED', result.get('error', '连接测试失败'))
    return result


# ---- 全局设置（sdk_path 等，存 data/app.db settings 表） ----

@router.get('/settings')
async def get_settings(state: AppState = Depends(get_state)):
    # 只读展示：SDK 固定从默认目录 tools/ 扫描，不支持自定义路径
    paths = await state.run_sync(state.sdk_paths)
    from rt_home import exe_dir, home
    return {'sdk_path_8': paths.get(8, ''), 'sdk_path_7': paths.get(7, ''),
            'data_dir': str(home()), 'exe_dir': str(exe_dir())}


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


class SdkDownloadIn(BaseModel):
    version: str = '8.5.3'


@router.post('/settings/sdk/download')
async def download_sdk(req: SdkDownloadIn, state: AppState = Depends(get_state)):
    """一键下载 Ren'Py SDK 到默认目录（任务：流式下载进度 → 解压 → 校验）

    下载/解压循环都响应取消；关键步骤同时写服务端日志（开发控制台可见）。
    """
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+', req.version):
        raise ApiError(400, 'BAD_VERSION', '版本号格式应为 x.y.z（如 8.5.3）')
    from ..sdk_download import make_download_body
    job = state.jobs.create(
        'settings.sdk-download', f'下载 Ren\'Py SDK {req.version}',
        {'version': req.version}, make_download_body(state, req.version),
        exclusive=True)
    return {'job_id': job.id}
