# PyInstaller recipe for the Windows program.
#
#   pip install -r requirements.txt -r packaging/requirements-build.txt
#   pyinstaller packaging/BeamProfiler.spec --noconfirm
#
# Output: dist/BeamProfiler/ with
#   BeamProfiler.exe         the program (no console; opens its own window)
#   BeamProfilerConsole.exe  the same program with a console, for troubleshooting
#   _internal/               Python, NumPy, SciPy, the page files
#
# The Thorlabs SDK is NOT bundled: it is loaded at run time from the user's
# ThorCam installation (see beamprofiler/thorlabs_sdk.py).

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(SPECPATH, "BeamProfiler.ico")

hidden = []
# uvicorn picks its protocol, loop and lifespan modules by name at run time.
hidden += collect_submodules("uvicorn")
hidden += collect_submodules("websockets")
# Standard-library modules used by the Thorlabs Python SDK, which is imported
# from disk at run time and so is invisible to PyInstaller's analysis.
hidden += [
    "ctypes", "ctypes.util", "ctypes.wintypes", "enum", "typing", "logging", "platform",
    "warnings", "abc", "struct", "traceback", "threading", "weakref", "queue", "decimal",
    "numpy.ctypeslib",
]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "beamprofiler", "static"), os.path.join("beamprofiler", "static"))],
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BeamProfiler",
    console=False,
    icon=ICON,
    upx=False,
)
exe_console = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BeamProfilerConsole",
    console=True,
    icon=ICON,
    upx=False,
)
coll = COLLECT(
    exe,
    exe_console,
    a.binaries,
    a.datas,
    upx=False,
    name="BeamProfiler",
)
