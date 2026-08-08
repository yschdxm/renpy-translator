# PyInstaller 打包配置：renpy-translator (onedir)
# 用法: .venv/Scripts/pyinstaller renpy-translator.spec --noconfirm
#
# 布局：
#   dist/renpy-translator/renpy-translator.exe   入口（GUI 默认）
#   dist/renpy-translator/_internal/            解释器 + 依赖 + src/ + web/dist
#   <exe 目录>/projects|config|logs|data|exports|fonts|tools   用户数据（RT_HOME，运行时生成/自带）

from PyInstaller.utils.hooks import collect_all, collect_submodules
import sys

block_cipher = None

datas = [('src', 'src'), ('web/dist', 'web/dist'),
         ('installer/icon.png', 'assets')]
# tiktoken 的编码表（cl100k_base 等）在命名空间包 tiktoken_ext 里，
# 由 pkgutil.iter_modules 动态发现 importlib 导入，静态分析追踪不到，
# 冻结后 "Plugins found: []"。整体收集 tiktoken_ext 即可（它随 tiktoken
# wheel 分发，没有独立的 dist 元数据，不要 copy_metadata）。
binaries = []
hiddenimports = [
    'anyio._backends._asyncio',
    'uvicorn.lifespan.on',
    'tiktoken_ext.openai_public',
    'webview.platforms.gtk' if sys.platform.startswith('linux') else
    'webview.platforms.cocoa' if sys.platform == 'darwin' else
    'webview.platforms.winforms',
]
if sys.platform == 'win32':
    hiddenimports.append('uvicorn.protocols.http.httptools_impl')

# pythonnet/clr_loader 仅 Windows（pywebview winforms 后端）；
# tiktoken/pystray/pillow 的动态部分整体收集
for pkg in (['pythonnet', 'clr_loader'] if sys.platform == 'win32' else []) \
        + ['tiktoken', 'tiktoken_ext', 'pystray', 'PIL']:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

if sys.platform == 'darwin':
    # pywebview cocoa 后端（pyobjc）
    hiddenimports += ['objc', 'Foundation', 'AppKit', 'WebKit',
                      'PyObjCTools', 'security']

hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('uvicorn')

a = Analysis(
    ['run.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'pytest_asyncio', 'nicegui'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='renpy-translator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 无控制台窗口：默认托盘模式，日志写 logs/
    icon='installer/icon.icns' if sys.platform == 'darwin'
         else 'installer/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='renpy-translator',
)

# macOS：打成 .app 捆绑包（分发用 DMG 封装）
if sys.platform == 'darwin':
    app_bundle = BUNDLE(
        coll,
        name="RenPyTranslator.app",
        icon=None,
        bundle_identifier='dev.yschdxm.renpy-translator',
        info_plist={
            'CFBundleName': "Ren'Py 翻译工具",
            'CFBundleDisplayName': "Ren'Py 翻译工具",
            'NSHighResolutionCapable': True,
        },
    )
