"""SDK 下载/安装：手动下载接口与启动时自动补装共用"""
import asyncio
import shutil
from pathlib import Path


def sdk_package(version: str) -> tuple:
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


def make_download_body(state, version: str):
    """构造 SDK 下载任务体（下载 → 解压 → 校验）。

    完成后按大版本写入对应设置槽位（sdk_path_7 / sdk_path_8），
    不影响另一个大版本的配置。
    """

    async def body(job):
        import tarfile
        import urllib.request
        import zipfile
        from rt_home import home
        from .jobs import JobCancelled

        def log(msg):
            job.emit_log(msg)
            state.logger.info(msg, panel='settings')

        fname, kind = sdk_package(version)
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
        log(f'SDK 就绪: {sdk_dir}')
        job.emit_progress(1.0, 'SDK 就绪')
        return {'sdk_path': str(sdk_dir)}

    return body
