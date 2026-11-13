"""Write THIRD_PARTY_NOTICES.txt for the packaged program.

The Windows build bundles Python and the packages in requirements.txt (and
their dependencies). Their licences allow redistribution but ask for their
licence texts to be included; this collects them from the build environment.

    python packaging/third_party_notices.py dist/BeamProfiler/THIRD_PARTY_NOTICES.txt
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
LICENSE_NAME = re.compile(r"(^|/)(licen[cs]e|copying|notice|authors)[^/]*$", re.IGNORECASE)


def runtime_distributions() -> list[metadata.Distribution]:
    """requirements.txt and everything it needs on this platform (not the build tools)."""
    wanted: list[tuple[str, frozenset]] = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            r = Requirement(line)
            wanted.append((r.name, frozenset(r.extras)))
    seen: dict[str, metadata.Distribution] = {}
    while wanted:
        name, extras = wanted.pop()
        key = name.lower().replace("_", "-")
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        if key in seen and not extras:
            continue
        seen[key] = dist
        for req in dist.requires or []:
            r = Requirement(req)
            envs = [{"extra": e} for e in extras] or [{"extra": ""}]
            if r.marker is None or any(r.marker.evaluate(env) for env in envs):
                wanted.append((r.name, frozenset(r.extras)))
    return sorted(seen.values(), key=lambda d: d.metadata["Name"].lower())


def licence_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        p = str(f).replace("\\", "/")
        if ".dist-info/" in p and LICENSE_NAME.search(p):
            try:
                out.append((p.split(".dist-info/", 1)[1], Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return out


def python_licence() -> str:
    for cand in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if cand.is_file():
            return cand.read_text(encoding="utf-8", errors="replace")
    return "Python Software Foundation License; see https://docs.python.org/3/license.html\n"


def main(out: str) -> None:
    rule = "=" * 78
    parts = [
        "Beam Profiler includes the following third-party software. Each is used under its own\n"
        "licence, reproduced below. Beam Profiler itself is under the MIT licence (LICENSE).\n"
        "The Thorlabs camera SDK is NOT included: it is used from the user's ThorCam installation.\n",
        f"{rule}\nPython {sys.version.split()[0]}\n{rule}\n{python_licence()}",
    ]
    fonts = ROOT / "beamprofiler" / "static" / "fonts"
    for ofl in sorted(fonts.glob("OFL-*.txt")):
        parts.append(f"{rule}\nFont: {ofl.stem.removeprefix('OFL-')} (SIL Open Font License 1.1)\n{rule}\n"
                     + ofl.read_text(encoding="utf-8", errors="replace"))
    for dist in runtime_distributions():
        name, version = dist.metadata["Name"], dist.version
        expr = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        expr = expr.strip().splitlines()[0] if expr.strip() else ""
        head = f"{rule}\n{name} {version}" + (f"  ({expr})" if expr and len(expr) < 80 else "") + f"\n{rule}\n"
        texts = licence_texts(dist)
        body = "\n".join(f"--- {fname} ---\n{text}" for fname, text in texts) if texts else \
            f"Licence: {expr or 'see the project page'} (no licence file shipped in the package metadata)\n"
        parts.append(head + body)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n\n".join(parts), encoding="utf-8")
    print(f"wrote {out}: Python, {len(list(fonts.glob('OFL-*.txt')))} fonts, "
          f"{len(parts) - 2 - len(list(fonts.glob('OFL-*.txt')))} packages")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "THIRD_PARTY_NOTICES.txt")
