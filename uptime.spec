# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('uptime')

# 瘦身：排除本应用用不到的库（numpy/requests 等是被环境连带拉进来的），
# 可去掉 ~8.5MB。
_EXCLUDES = [
    'numpy', 'matplotlib', 'pandas', 'scipy',
    'requests', 'urllib3', 'charset_normalizer', 'certifi', 'idna',
]

a = Analysis(
    ['D:/公司项目/claude/uptime/uptime/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[('data', 'data'), ('config.example.json', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    noarchive=False,
    optimize=0,
)

# 瘦身：去掉不用的 PIL 编解码插件二进制（只用 ImageDraw/ImageTk 画 PNG/方块），
# 可再省 ~5MB。
_DROP_PIL = ('pil\\_avif', 'pil\\_webp', 'pil\\_imagingft', 'pil\\_imagingcms')
a.binaries = [b for b in a.binaries if not any(k in b[0].lower() for k in _DROP_PIL)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='uptime',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/公司项目/claude/uptime/build/uptime.ico'],
)
