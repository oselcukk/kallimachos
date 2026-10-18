"""Download the official avenoxbeyin release ZIP for the optional Avenox end-to-end test.

Avenox (github.com/avenoxai/avenoxbeyin, MIT) is never vendored into this repository. This tool
fetches a release asset ``beyin-v3-<version>.zip``, checks it against the published
``.zip.sha256`` file and extracts it into a cache folder. It runs only when you call it (or when
``KALLIMACHOS_E2E_NETWORK=1`` enables ``tests/e2e/test_avenox_e2e.py``), never during a normal
test run.

    python tools/fetch_avenox.py                    # latest release -> cache folder, prints JSON
    python tools/fetch_avenox.py --version 3.11.0 --dest /tmp/avenox
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "avenoxai/avenoxbeyin"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "https://github.com/{repo}/releases/download/v{version}/beyin-v3-{version}.zip"
USER_AGENT = "kallimachos-e2e (+https://github.com/oselcukk/kallimachos)"
TIMEOUT_S = 60


def default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "kallimachos-e2e" / "avenox"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # fixed https URLs only
        data: bytes = resp.read()
    return data


def latest_version() -> str:
    data = json.loads(_get(API_LATEST))
    tag = str(data["tag_name"])
    return tag[1:] if tag.startswith("v") else tag


def _safe_extract(archive: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe path in archive: {member.filename}")
        zf.extractall(dest)


def fetch(version: str | None = None, dest: Path | None = None) -> dict[str, str]:
    """Download (once), verify and extract. Returns version, zip, sha256 and folder paths."""
    version = version or latest_version()
    folder = (dest or default_cache()) / version
    archive = folder / f"beyin-v3-{version}.zip"
    extracted = folder / "release"
    url = ASSET.format(repo=REPO, version=version)
    expected = _get(url + ".sha256").decode().split()[0].lower()
    folder.mkdir(parents=True, exist_ok=True)
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        archive.write_bytes(_get(url))
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        archive.unlink()
        raise ValueError(f"sha256 mismatch for {url}: {actual} != {expected}")
    if not (extracted / "scripts" / "install_v3.py").is_file():
        extracted.mkdir(exist_ok=True)
        _safe_extract(archive, extracted)
    return {
        "version": version,
        "zip": str(archive),
        "sha256": actual,
        "release": str(extracted),
        "installer": str(extracted / "scripts" / "install_v3.py"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the avenoxbeyin release ZIP (MIT).")
    parser.add_argument("--version", help="release version such as 3.11.0 (default: latest)")
    parser.add_argument("--dest", type=Path, help="cache folder (default: ~/.cache/...)")
    args = parser.parse_args(argv)
    print(json.dumps(fetch(args.version, args.dest), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
