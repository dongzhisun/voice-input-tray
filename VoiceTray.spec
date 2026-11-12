# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build for VoiceTray.

onedir (not onefile) is deliberate: the 239 MB int8 weights must not be
unpacked into a temp directory on every launch.
"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
MODEL_ID = "sensevoice-int8-2024-07-17"

datas = [
    (str(ROOT / "assets" / "models" / MODEL_ID),
     "assets/models/" + MODEL_ID),
    (str(ROOT / "assets" / "test_wavs"), "assets/test_wavs"),
]
binaries = []
hiddenimports = [
    # comtypes generates its UIA wrapper lazily; in a frozen app the
    # pre-generated modules must be bundled explicitly.
    "comtypes", "comtypes.client", "comtypes.gen",
    "comtypes.gen.UIAutomationClient",
    "comtypes.gen.stdole",
    "comtypes.gen._00020430_0000_0000_C000_000000000046_0_2_0",
    "comtypes.gen._944DE083_8FB8_45CF_BCB7_C477ACB2F897_0_1_0",
    "win32clipboard", "win32api", "win32con", "pywintypes", "pythoncom",
    "pystray._win32",
    "tkinter", "tkinter.ttk", "tkinter.messagebox",
    "PIL.Image", "PIL.ImageDraw", "PIL.ImageTk",
    "sounddevice", "sherpa_onnx", "numpy",
]

for _pkg in ("sherpa_onnx", "sounddevice", "pystray", "PIL", "comtypes"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    [str(ROOT / "voicetray_main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "matplotlib", "scipy", "IPython", "notebook",
              "PyInstaller", "setuptools"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoiceTray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # CLI subcommands need stdout; hidden at runtime for "run"
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VoiceTray",
)
