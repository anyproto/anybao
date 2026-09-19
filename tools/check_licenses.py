"""Check reviewed notices, license copies, and packaged release contents."""

import argparse
import hashlib
import json
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "licenses/manifest.json"
INPUTS = (
    "runtime/Cargo.lock",
    "runtime/Cargo.toml",
    "licenses/about.toml",
    "licenses/about.hbs",
    "licenses/about-kernel.toml",
    "licenses/about-kernel.hbs",
    "runtime/guest/VENDORED.md",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def notice_paths():
    paths = [ROOT / "LICENSE", ROOT / "THIRD_PARTY_NOTICES.md"]
    for directory in ("licenses", "runtime/guest/licenses"):
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file())
    return sorted(p for p in paths if p != MANIFEST)


def check_copies():
    license_text = (ROOT / "LICENSE").read_text()
    require((ROOT / "runtime/LICENSE").read_text() == license_text, "runtime/LICENSE differs")
    for overlay in ("_agent", "_connectors"):
        path = f"repos/{overlay}/README.md"
        require((ROOT / path).read_text().endswith(license_text), f"{path} omits the MIT text")
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    version = next(p["version"] for p in packages if p["name"] == "componentize-py")
    expected = json.loads((ROOT / "licenses/kernel/build.json").read_text())["componentize_py"]
    require(version == expected, "componentize-py changed; review kernel notices before recording")
    for source in json.loads((ROOT / "licenses/kernel/sources.json").read_text()):
        path = ROOT / "licenses/kernel" / source["file"]
        require(digest(path.read_bytes()) == source["sha256"], f"upstream notice changed: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record", action="store_true", help="record reviewed input and notice hashes"
    )
    parser.add_argument("--archive", type=Path, help="also check a release tar.gz")
    args = parser.parse_args()
    check_copies()
    current = {
        "inputs": {name: digest((ROOT / name).read_bytes()) for name in INPUTS},
        "notices": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in notice_paths()},
    }
    if args.record:
        MANIFEST.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    expected = json.loads(MANIFEST.read_text())
    require(
        current == expected,
        "license inputs or notices changed; review and record the updated bundle",
    )
    if args.archive:
        with tarfile.open(args.archive) as archive:
            for name, checksum in expected["notices"].items():
                member = archive.extractfile(name)
                require(member is not None, f"archive omits {name}")
                require(digest(member.read()) == checksum, f"archive notice differs: {name}")
            require(archive.getmember("anyrt").isfile(), "archive omits the executable")
    print(f"License checks passed ({len(expected['notices'])} notice files)")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, KeyError, ValueError, tarfile.TarError) as error:
        print(f"License check failed: {error}", file=sys.stderr)
        sys.exit(1)
