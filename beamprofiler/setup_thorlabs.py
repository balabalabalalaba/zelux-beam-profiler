"""Check (and if needed prepare) the Thorlabs camera SDK for the beam profiler (Windows).

The profiler does this by itself on start-up; run this to see what it finds or
to point it at an SDK zip in an unusual place.

ThorCam installs the SDK as a zip file ("Scientific_Camera_Interfaces.zip")
under  C:\\Program Files\\Thorlabs\\Scientific Imaging\\Scientific Camera Support.
The Python wrapper is extracted from it into
%LOCALAPPDATA%\\BeamProfiler\\thorlabs\\python, and the 64-bit camera DLLs are
used from the ThorCam folder (or extracted next to it if only the zip exists).

    python -m beamprofiler.setup_thorlabs
    python -m beamprofiler.setup_thorlabs --zip "D:\\path\\Scientific_Camera_Interfaces.zip"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import thorlabs_sdk


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zip", help="Path to Scientific_Camera_Interfaces.zip (found automatically if omitted).")
    ap.add_argument("--search", action="append", default=[], help="Extra folder to search.")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print("Note: Thorlabs provides this camera SDK for Windows. On this computer use the simulator "
              "(python -m beamprofiler --camera sim) or open saved images.")

    if args.zip:
        try:
            thorlabs_sdk.install_from_zip(Path(args.zip))
        except Exception as e:
            print(f"Could not read {args.zip}: {e}")
            return 1
    result = thorlabs_sdk.ensure(search=args.search)

    ok = True
    if not result["python"]:
        ok = False
        print("\nCould not find the Thorlabs Python package. Install ThorCam from thorlabs.com, then run this again,\n"
              "or pass the SDK zip:  python -m beamprofiler.setup_thorlabs --zip <Scientific_Camera_Interfaces.zip>")
    else:
        import thorlabs_tsi_sdk

        print(f"Thorlabs Python SDK: {getattr(thorlabs_tsi_sdk, '__file__', '?')}")
    if result["dll_dir"] is None:
        ok = False
        print("\nCould not find the camera DLLs (thorlabs_tsi_camera_sdk.dll). Install ThorCam, or copy the SDK's\n"
              f"'Native Toolkit\\dlls\\Native_64_lib' folder contents into {thorlabs_sdk.dll_cache_dir()}")
    else:
        print(f"Camera DLLs: {result['dll_dir']}")
    if not ok:
        return 1

    print("Checking for cameras (close ThorCam first)...")
    try:
        from .cameras import ThorlabsCamera

        cam = ThorlabsCamera()
        print(f"Found: {cam.name}, {cam.width} x {cam.height} px, {cam.bit_depth} bit, {cam.pixel_size_um} um pixels")
        cam.close()
    except Exception as e:
        print(f"SDK is ready, but no camera could be opened: {e}")
        return 2
    print("Setup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
