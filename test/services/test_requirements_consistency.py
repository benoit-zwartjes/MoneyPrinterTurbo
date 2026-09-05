"""
requirements.txt and pyproject.toml have to list the same runtime packages.

Two installers are in play and they read different files: `uv sync` resolves
pyproject.toml, while both Dockerfiles — and so every Coolify deployment — run
`pip install -r requirements.txt`. A dependency added to one and not the other
passes every local test and every CI run, then fails only on the deployed
server, as a feature that quietly reports itself unavailable.

That is exactly how yt-dlp shipped broken: added to pyproject.toml, missing
from requirements.txt, so the image had no downloader and the page said so.
"""

import re
import tomllib
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent.parent
REQUIREMENTS = ROOT_DIR / "requirements.txt"
PYPROJECT = ROOT_DIR / "pyproject.toml"

# Package name at the front of a requirement line, before any version specifier,
# extra or environment marker.
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    """PEP 503: '_' and '.' and '-' are the same character in a package name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirements_names() -> set[str]:
    names = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _NAME.match(line)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _pyproject_names() -> set[str]:
    payload = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    names = set()
    for entry in payload["project"]["dependencies"]:
        match = _NAME.match(entry.strip())
        if match:
            names.add(_normalize(match.group(1)))
    return names


def test_every_runtime_dependency_is_in_both_files():
    missing_from_requirements = _pyproject_names() - _requirements_names()
    assert not missing_from_requirements, (
        "these are in pyproject.toml but not requirements.txt, so the Docker "
        f"image will not have them: {sorted(missing_from_requirements)}"
    )


def test_requirements_adds_nothing_pyproject_does_not_have():
    extra = _requirements_names() - _pyproject_names()
    assert not extra, (
        "these are in requirements.txt but not pyproject.toml, so `uv sync` "
        f"will not install them: {sorted(extra)}"
    )


def test_yt_dlp_is_installed_by_the_docker_image():
    """
    Pinned by name because the gameplay downloader is the reason this file
    exists, and the symptom of losing it is a feature that just says it is
    unavailable rather than anything failing loudly.
    """
    assert "yt-dlp" in _requirements_names()
