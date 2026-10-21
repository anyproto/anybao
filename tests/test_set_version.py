"""tools/set_version.sh: the anyrt version a release tag sets (release.yml).

Each test runs the script on a copy of the files it touches and the
license check reads, so the repo itself never changes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "tools/set_version.sh",
    "tools/check_licenses.py",
    "runtime/Cargo.toml",
    "runtime/Cargo.lock",
    "runtime/LICENSE",
    "runtime/guest/VENDORED.md",
    "repos/_agent/README.md",
    "repos/_connectors/README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "uv.lock",
)
DIRS = ("licenses", "runtime/guest/licenses")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, tmp_path / name)
    for name in DIRS:
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path


def run(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "tools/set_version.sh"), *args],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )


def crate_version(tree: Path) -> str:
    text = (tree / "runtime/Cargo.toml").read_text()
    match = re.search(r'\[package\]\nname = "anyrt"\nversion = "([^"]+)"', text)
    assert match is not None
    return match.group(1)


def lock_version(tree: Path) -> str:
    text = (tree / "runtime/Cargo.lock").read_text()
    match = re.search(r'\[\[package\]\]\nname = "anyrt"\nversion = "([^"]+)"', text)
    assert match is not None
    return match.group(1)


def notice_versions(tree: Path) -> set[str]:
    text = (tree / "licenses/RUST_DEPENDENCIES.md").read_text()
    return set(re.findall(r"^- anyrt (\S+)$", text, re.M))


def license_check(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tree / "tools/check_licenses.py")],
        capture_output=True,
        text=True,
        check=False,
    )


def next_major(version: str) -> str:
    return f"{int(version.split('.')[0]) + 1}.0.0"


def test_tag_sets_every_recorded_version(tree: Path) -> None:
    target = next_major(crate_version(tree))
    result = run(tree, f"v{target}")
    assert result.returncode == 0, result.stderr
    assert crate_version(tree) == target
    assert lock_version(tree) == target
    assert notice_versions(tree) == {target}
    assert license_check(tree).returncode == 0


def test_only_the_version_changes(tree: Path) -> None:
    before = (tree / "runtime/Cargo.lock").read_text()
    current = crate_version(tree)
    target = next_major(current)
    assert run(tree, target).returncode == 0
    after = (tree / "runtime/Cargo.lock").read_text()
    assert after == before.replace(
        f'name = "anyrt"\nversion = "{current}"', f'name = "anyrt"\nversion = "{target}"'
    )


def test_same_version_is_a_no_op(tree: Path) -> None:
    manifest = (tree / "licenses/manifest.json").read_text()
    result = run(tree, crate_version(tree))
    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout
    assert (tree / "licenses/manifest.json").read_text() == manifest


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3-rc1", "latest", ""])
def test_rejects_a_version_that_is_not_x_y_z(tree: Path, bad: str) -> None:
    current = crate_version(tree)
    result = run(tree, bad)
    assert result.returncode != 0
    assert crate_version(tree) == current


def test_only_if_newer_never_lowers_the_version(tree: Path) -> None:
    current = crate_version(tree)
    result = run(tree, "--only-if-newer", "0.0.1")
    assert result.returncode == 0, result.stderr
    assert crate_version(tree) == current
    assert notice_versions(tree) == {current}


def test_only_if_newer_raises_the_version(tree: Path) -> None:
    target = next_major(crate_version(tree))
    assert run(tree, "--only-if-newer", f"v{target}").returncode == 0
    assert crate_version(tree) == target
    assert license_check(tree).returncode == 0


def test_only_if_newer_compares_numerically(tree: Path) -> None:
    major, minor, _ = crate_version(tree).split(".")
    target = f"{major}.{int(minor) + 10}.0"
    assert run(tree, "--only-if-newer", target).returncode == 0
    assert crate_version(tree) == target


def test_refuses_a_tree_whose_license_check_already_fails(tree: Path) -> None:
    notices = tree / "THIRD_PARTY_NOTICES.md"
    notices.write_text(notices.read_text() + "\nunreviewed change\n")
    current = crate_version(tree)
    result = run(tree, next_major(current))
    assert result.returncode != 0
    assert "license check fails before" in result.stderr
    assert crate_version(tree) == current
