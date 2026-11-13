"""Checks that the Thorlabs SDK is found in, and extracted from, ThorCam-style zip files.

No camera or real SDK is needed: the zips are synthetic.

Run:  python -m pytest tests   (or: python tests/test_thorlabs_sdk.py)
"""

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beamprofiler import desktop, thorlabs_sdk  # noqa: E402


def _fresh_home():
    home = Path(tempfile.mkdtemp(prefix="bp-test-"))
    os.environ["BEAMPROFILER_HOME"] = str(home)
    return home


def test_roundtrip_from_interfaces_zip():
    home = _fresh_home()
    detail = desktop._fake_sdk_roundtrip(home / "work")
    assert "thorlabs_tsi_sdk" in detail


def test_loose_package_zip():
    home = _fresh_home()
    pkg = home / thorlabs_sdk.PKG_ZIP
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("thorlabs_tsi_sdk/__init__.py", "")
        z.writestr("thorlabs_tsi_sdk/tl_camera.py", "class TLCameraSDK: pass\nclass OPERATION_MODE: pass\n")
    got_pkg, got_dlls = thorlabs_sdk.install_from_zip(pkg, log=lambda *_: None)
    assert got_pkg and not got_dlls
    assert (thorlabs_sdk.python_dir() / "thorlabs_tsi_sdk" / "tl_camera.py").is_file()


def test_rejects_path_traversal():
    home = _fresh_home()
    pkg = home / "evil.zip"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("../thorlabs_tsi_sdk/__init__.py", "")
        z.writestr("/abs/thorlabs_tsi_sdk/__init__.py", "")
    got_pkg, _ = thorlabs_sdk.install_from_zip(pkg, log=lambda *_: None)
    assert not got_pkg
    assert not (home.parent / "thorlabs_tsi_sdk").exists()


def test_no_package_no_crash():
    home = _fresh_home()
    empty = home / "Scientific_Camera_Interfaces.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("readme.txt", "nothing here")
    result = thorlabs_sdk.ensure(zip_path=empty, log=lambda *_: None)
    assert result["sources"] == []


def test_dll_choice_prefers_64_bit():
    home = _fresh_home()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SDK/Native Toolkit/dlls/Native_32_lib/" + thorlabs_sdk.DLL_NAME, desktop.fake_pe(0x14C))
        z.writestr("SDK/Native Toolkit/dlls/Native_64_lib/" + thorlabs_sdk.DLL_NAME, desktop.fake_pe(0x8664))
        z.writestr("SDK/Native Toolkit/dlls/Native_64_lib/thorlabs_tsi_usb_driver.dll", desktop.fake_pe(0x8664))
    path = home / "Scientific_Camera_Interfaces.zip"
    path.write_bytes(buf.getvalue())
    _pkg, got_dlls = thorlabs_sdk.install_from_zip(path, need_pkg=False, log=lambda *_: None)
    assert got_dlls
    assert thorlabs_sdk.is_amd64_dll(thorlabs_sdk.dll_cache_dir() / thorlabs_sdk.DLL_NAME)
    assert thorlabs_sdk.find_dll_dir() == str(thorlabs_sdk.dll_cache_dir())


def test_pe_machine_check():
    home = _fresh_home()
    x64, x86, junk = home / "a.dll", home / "b.dll", home / "c.dll"
    x64.write_bytes(desktop.fake_pe(0x8664))
    x86.write_bytes(desktop.fake_pe(0x14C))
    junk.write_bytes(b"not a dll")
    assert thorlabs_sdk.is_amd64_dll(x64)
    assert not thorlabs_sdk.is_amd64_dll(x86)
    assert not thorlabs_sdk.is_amd64_dll(junk)
    assert not thorlabs_sdk.is_amd64_dll(home / "missing.dll")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
