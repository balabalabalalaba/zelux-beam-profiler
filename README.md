# Beam Profiler for the Thorlabs Zelux CS165MU1

A live laser beam profiler. A Python program reads the camera and does the
analysis. Its window (a web page) shows the live image, beam widths, profiles
and trends, and lets you set a region of interest, exposure and the rest.

## Download

**[Download the installer for Windows](https://github.com/balabalabalalaba/zelux-beam-profiler/releases/latest/download/BeamProfiler-setup.exe)**
(`BeamProfiler-setup.exe`) ·
[portable zip](https://github.com/balabalabalalaba/zelux-beam-profiler/releases/latest/download/BeamProfiler-portable.zip)
(no installation) · [all releases](https://github.com/balabalabalalaba/zelux-beam-profiler/releases)

Needs Windows 10 or 11, and ThorCam from thorlabs.com for the camera driver.
The links always give the newest version. To update, run the new installer
over the old one.

The live camera needs **Windows** (Thorlabs has no macOS driver). Everything
runs on the one PC that the camera is plugged into.

## Windows: install the program (recommended)

1. Install **ThorCam** from thorlabs.com. It installs the camera's USB driver
   and the camera SDK, which the profiler uses from there.
2. Download **`BeamProfiler-setup.exe`** from the
   [latest release](https://github.com/balabalabalalaba/zelux-beam-profiler/releases/latest)
   and run it. No administrator rights and no Python are needed. Windows may
   warn that the publisher is unknown (the program is not code-signed): choose
   *More info* → *Run anyway*. To update, run a newer installer over the old
   version: there is no need to uninstall, and your settings and save folder
   are kept.
3. Close ThorCam (only one program can use the camera at a time) and start
   **Beam Profiler** from the Start menu.

The profiler opens in its own window. Closing the window stops the program
and releases the camera. Other things to know:

- **Without a camera:** the Start menu also has *Beam Profiler (simulated
  beam)*.
- **Portable zip:** unzip `BeamProfiler-portable.zip` anywhere and run
  `BeamProfiler.exe`. Nothing is installed.
- **Where files go:** snapshots and logs go to `Documents\BeamProfiler`. The
  *open folder* link in the snapshot message opens that folder.
- **Program log:** `%LOCALAPPDATA%\BeamProfiler\beamprofiler.log`.
- **Troubleshooting:** the *troubleshooting console* shortcut runs
  `BeamProfilerConsole.exe`, which shows the messages in a window.

### If the camera is not found

- **"Thorlabs camera SDK was not found":** ThorCam is not installed, or is in
  an unusual place. Copy ThorCam's `Scientific_Camera_Interfaces.zip`
  (normally in `C:\Program Files\Thorlabs\Scientific Imaging\Scientific
  Camera Support`) into the program's folder and press *Retry camera*. The
  program copies the Python part of the SDK to
  `%LOCALAPPDATA%\BeamProfiler\thorlabs` once.
- **"No Thorlabs camera found":** check the USB 3.0 cable and that ThorCam is
  closed.

The Thorlabs SDK is Thorlabs' software under its own licence, so the program
does not include it. It uses the copy from your ThorCam installation instead.

### Options

`BeamProfiler.exe` takes the options listed under *Options* below, plus
`--browser`, which opens the default browser instead of an app window. For
example, a shortcut to `BeamProfiler.exe --lan` shares the page with other
computers on the lab network.

## Windows: run from source (Python)

1. Install **ThorCam** and **Python 3.12 or newer** from python.org. Tick
   "Add python.exe to PATH".
2. Copy this whole folder to the PC, close ThorCam, and double-click
   **`Start Beam Profiler.bat`**.

The first run creates a Python environment and installs the packages. Later
runs start in a few seconds. To check the SDK set-up by hand:

```
.venv\Scripts\python -m beamprofiler.setup_thorlabs
.venv\Scripts\python -m beamprofiler.setup_thorlabs --zip "C:\...\Scientific_Camera_Interfaces.zip"
```

**View from another computer (for example your Mac):** start
`Start Beam Profiler (network).bat` instead, allow it through the Windows
firewall, and open `http://<PC's IP address>:8765/` on the other computer.
There is no password, so only do this on a trusted lab network.

## Building the Windows program

GitHub Actions builds it (`.github/workflows/build-windows.yml`) on every push
to `main`. Each build:

1. runs the tests;
2. builds with PyInstaller (`packaging/BeamProfiler.spec`);
3. runs the built `.exe` in self-test mode with the simulated beam;
4. makes the portable zip and the Inno Setup installer
   (`packaging/installer.iss`).

To publish a release, set `__version__` in `beamprofiler/__init__.py`, then
push a matching tag:

```
git tag v0.2.0 && git push origin v0.2.0
```

To build by hand on a Windows PC:

```
pip install -r requirements.txt -r packaging/requirements-build.txt
pyinstaller packaging/BeamProfiler.spec --noconfirm
dist\BeamProfiler\BeamProfilerConsole.exe --smoke-test
```

## Mac: simulator and saved images

Double-click `start_simulator_mac.command`, or:

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m beamprofiler --camera sim
```

To analyse a saved image, drop it on the image area or use
Source → Open image file (PNG, TIFF, BMP, JPEG, `.npy`).

## Using it

| Area | What it does |
| --- | --- |
| Top bar | Source, frame rate, exposure, peak level meter; Freeze, Snapshot, Start log |
| Camera | Exposure, auto exposure, gain, frame averaging (up to 32) |
| Background | Dark-frame capture and subtraction; automatic baseline from the ROI corners |
| Region of interest | Draw on the image (ROI tool), type numbers, or let it track the beam |
| Measurement | Which width is shown (D4σ or Gaussian fit), radius/diameter, units, profile type |
| Display | Colour map, linear/sqrt/log scale, contrast stretch, overlays |
| Calibration | Pixel size, magnification, wavelength, beam power for peak intensity and I/Isat |
| Bottom tabs | Size over time, pointing stability, M² / waist fit |

Mouse: drag to draw the ROI, drag its corners to resize, drag inside to move.
Scroll to zoom, Alt-drag (or the Pan tool) to move the view, double-click to
fit. Keys: `R` `P` `C` tools, `F` fit view, `Space` freeze.

**M² / waist:** move the camera along the beam, type its position z, click
"Add point" at each place. With three or more points it fits
w(z) = sqrt(w0² + θ²(z − z0)²) and reports w0, z0, zR and M².

**Quick save:** choose a folder once in the *Quick save* panel (*Choose
folder…*, or type the path). It is remembered between runs. After that,
*Save* in the top bar (or **Ctrl+S**) saves at once, with no dialog. You are
asked only if a file with that name already exists (save under the next free
name, overwrite, or cancel). The file name counts up by itself (`beam_001`,
`beam_002`, …), and the *Note* field (e.g. `z = 25 mm`) goes into the file.

Each quick save writes:

- **`<name>.json`**, with:
  - a `summary` in µm: centroid, D4σ radii along X/Y and the major/minor axes,
    the angle, and the Gaussian-fit radii;
  - both profiles at full resolution (positions in µm and pixels, signal in
    counts) with their Gaussian fits;
  - the raw pixel results, the status (peak level, saturation, baseline,
    averaging), the camera, every setting, the display choices, and the
    M² points if you have any.
- **a new row in `quicksave_summary.csv`** in the same folder: one row per
  save, all widths as 1/e² radii in µm, ready for a spreadsheet or Python.
- **`<name>.tif`** with the raw frame, only if *Also save the raw frame* is
  ticked. It is a 16-bit TIFF in camera counts (the same kind of file ThorCam
  saves) with lossless compression, about 1 MB. With *Average frames* above 1
  it holds the mean of those frames, rounded to whole counts (at most 0.5
  count off, below the camera noise).

Read a save in Python with `json.load(open("beam_001.json"))["summary"]`, or
the whole table with `pandas.read_csv("quicksave_summary.csv")`.

**Settings file:** *Settings file → Save settings…* writes the camera,
measurement, display and calibration settings to a `.json` file of your
choice. *Load settings…* applies such a file again. It also accepts a
quick-save `.json`, to repeat the settings of an earlier measurement.

**Style:** *Display → Style → Pixel* (or the ✚ button in the top bar) switches
to a pixel-art look with retro fonts and colours. Only the look changes, not
the measurement. The *Pixel* and *Handheld* colour maps show the image in a
few flat colour steps.

Snapshots go to `data/snapshots` (a 16-bit `.tif` of the raw frame, as for
quick save, plus a `.json` with all results and settings). Logs go to `data/logs`
as CSV.

## Measurement notes

- **Attenuate the beam.** The sensor saturates at microwatt levels and can be
  damaged by a focused beam. Use absorptive ND filters, slightly tilted.
- **Keep the peak at 50–90 %.** Saturated pixels are drawn in magenta and make
  every width read too large. Fix the gain at 0 dB and use exposure.
- **Definitions.** Gaussian `w` is the 1/e² intensity radius. D4σ is the ISO
  11146 second-moment diameter with the iterative 3× integration window; for
  an ideal Gaussian D4σ = 2w. "Radius" shows D4σ/2.
- **Baseline matters for D4σ.** Leave "Auto baseline" on, and capture a dark
  frame (beam blocked) if the background is uneven. Recapture it after
  changing exposure.
- **Size range.** With 3.45 µm pixels and a 4.97 × 3.73 mm sensor, 1/e² radii
  from about 30 µm to 1 mm work. A warning appears when the beam is cut off by
  the ROI or the sensor edge enough to bias D4σ; the Gaussian fit is less
  sensitive to that.
- **Fringes** from the sensor window or ND filters disturb D4σ less than the
  eye suggests, but averaging frames and tilting the filter help.
- **"Gaussian fit X, Y"** fits the two profiles along the sensor axes. For a
  tilted elliptical beam use "D4σ major, minor" or the 2D Gaussian fit.

## Checking the analysis

```
.venv/bin/python tests/test_analysis.py        (Windows: .venv\Scripts\python tests\test_analysis.py)
.venv/bin/python tests/test_thorlabs_sdk.py
.venv/bin/python tests/test_quicksave.py
```

The first runs the analysis on synthetic beams of known size. The second
checks that the SDK is found in ThorCam-style zip files. The third checks
quick save against the simulated beam.

## Options

```
python -m beamprofiler --help
  --camera thorlabs|sim   real camera (default) or simulated beam
  --serial SN             choose a camera when several are connected
  --dll-dir DIR           folder with the Thorlabs 64-bit DLLs
  --port 8765             web port
  --lan                   allow other computers to connect
  --data-dir DIR          where snapshots and logs go (default: ./data, or Documents\BeamProfiler for the .exe)
  --no-browser            do not open the browser automatically
```


## Licence

Beam Profiler is under the [MIT licence](LICENSE).

The Windows downloads also contain Python and the packages listed in
`requirements.txt`, each under its own licence (MIT, BSD, Apache-2.0, PSF and
others). Their licence texts are in `THIRD_PARTY_NOTICES.txt` next to the
program. The pixel-style fonts are under the SIL Open Font License (see
`beamprofiler/static/fonts`).

This project is not affiliated with or endorsed by Thorlabs. Thorlabs, Zelux
and ThorCam are trademarks of Thorlabs, Inc. The Thorlabs camera SDK is not
part of this project and is not redistributed: the program uses the copy
installed with ThorCam.
