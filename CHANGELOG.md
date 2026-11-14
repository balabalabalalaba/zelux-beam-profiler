# Changelog

The changes in each released version of Beam Profiler, taken from the commit
history and the release tags. Version numbers that were never released are
folded into the next release.

**The public history starts at 0.3.6.** The public repository holds one
snapshot commit per release, from 0.3.6 on. Versions 0.3.2 to 0.3.5 were
released before the source was made public and cannot be downloaded from it.

## 0.3.9 (2026-11-14)

- Pixel style: digits and the decimal point are drawn with Press Start 2P,
  because Pixelify Sans' 5, 2 and 0 can be read as S, Z and O at the page's
  text sizes. Only the look changes, not the measurement.
- Pixel style: ligatures are off, so "fi" no longer reads as "A" (as in
  "Profiler").
- The colour-map menu says "Pixel (11 steps)", matching its palette (it said
  16).
- The four-green colour map has a neutral internal name; preferences and
  settings files that use the old name still load it.
- Documentation: screenshots and a live-view animation in the README, a
  "What it does not do" section, `CITATION.cff`, this changelog, and the
  minimum Python version for running from source (3.9) stated the same
  everywhere.

## 0.3.8 (2026-11-13)

- **Measure on/off switch** (top-bar button, M key, checkbox). Off skips the
  D4σ widths, fits and profiles; the image, level meter and auto exposure
  keep going. Quick save refuses while it is off; settings files do not store
  it.
- **Auto exposure** no longer chases noise when the beam is lost: with no
  beam in view it stays at or below 50 ms (it used to go up to 2 s per frame),
  goes back to 50 ms after a brief glimpse of the beam, and drops quickly
  when many pixels saturate. Hot pixels do not count as a beam.
- Automatic ROI falls back to the full frame while measuring is off.
- The source badge and top bar fit a 1024 px wide window.
- New engine tests, also run by the Windows build.

## 0.3.7 (2026-11-13)

- The README and the user guide credit Claude, Anthropic's AI assistant, as
  co-creator of the program.

## 0.3.6 (2026-11-13)

First version in the public repository.

- Release files have fixed names (`BeamProfiler-setup.exe`,
  `BeamProfiler-portable.zip`), so links to the latest release always work.

## 0.3.5 (2026-11-13)

Also includes 0.3.4, which was not released.

- MIT licence. The installer shows it, and the downloads include
  `LICENSE.txt` and `THIRD_PARTY_NOTICES.txt` (the licences of Python, the
  bundled packages and the fonts).
- The downloads contain a user guide (`User guide.txt`, also in the Start
  menu) instead of the developer README.
- README: download link, licence section, and a notice that the project is
  not affiliated with Thorlabs.

## 0.3.3 (2026-11-13)

- Fix: on Windows, a refused cross-site request could end with a connection
  error instead of the refusal, because the server answered before reading
  the request.

## 0.3.2 (2026-11-13)

First release. Also includes 0.1.0 to 0.3.1, which were not released.

- **Live beam profiler** for the Thorlabs Zelux CS165MU1: live image, ISO
  11146 second-moment (D4σ) widths, 1D and 2D Gaussian fits, X and Y
  profiles, size and pointing over time, and an M² / waist fit; a simulated
  beam and saved image files when there is no camera.
- **Windows program** built by GitHub Actions: `BeamProfiler.exe` in its own
  window, an installer and a portable zip. The Thorlabs SDK is taken from the
  user's ThorCam installation at run time, not bundled.
- **Quick save**: choose a folder once, then Save or Ctrl+S writes a `.json`
  with the results in µm radii, the full profiles and all settings, and adds
  a row to `quicksave_summary.csv`.
- **Settings file**: save the settings to a `.json` file and load them again
  (a quick-save file also works).
- **Pixel style**, with pixel fonts shipped with the program and stepped
  colour maps; the measurement is unchanged.
- Raw frames and snapshots are saved as losslessly compressed 16-bit TIFF
  (about 1 MB).
- The server refuses requests from other web sites.
- Fixes: *Save settings* now stores an exposure that was still being typed;
  a remembered "2D Gaussian fit" choice switches the fit back on after a
  restart (the readouts used to stay blank).
