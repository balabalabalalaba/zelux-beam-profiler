"""PyInstaller entry point for BeamProfiler.exe (see BeamProfiler.spec)."""

import multiprocessing
import sys

from beamprofiler.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
