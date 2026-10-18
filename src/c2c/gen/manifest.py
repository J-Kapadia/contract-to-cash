"""The books manifest: a SHA-256 for every generated file, and the check against drift.

The full books live directly in `<out>/books/` and `<out>/truth/` with their manifest at
`<out>/books.manifest.sha256`; every other preset has its own subdirectory of each and its
manifest at `<out>/books/<preset>.manifest.sha256`. The manifest has header lines `# seed: N`,
`# preset: NAME`, `# generator_sha256: HEX`, `# config_sha256: HEX`, then one `HEX  relative/path`
line per file, sorted by path. Paths are relative to `<out>` and use `/`. A directory's manifest
covers the files directly in it, so the full books' manifest never lists another preset's files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

FULL = "full"
HEADER_KEYS = ("seed", "preset", "generator_sha256", "config_sha256")


@dataclass(frozen=True)
class Manifest:
    seed: int
    preset: str
    generator_sha256: str
    config_sha256: str
    files: dict[str, str]  # relative path -> sha256

    def render(self) -> str:
        lines = [f"# {key}: {getattr(self, key)}" for key in HEADER_KEYS]
        lines += [f"{digest}  {path}" for path, digest in sorted(self.files.items())]
        return "\n".join(lines) + "\n"


def manifest_path(out: Path, preset: str) -> Path:
    if preset == FULL:
        return out / "books.manifest.sha256"
    return out / "books" / f"{preset}.manifest.sha256"


def presets_under(out: Path) -> list[str]:
    """The presets that have a manifest under `out`."""
    found = [p.name.removesuffix(".manifest.sha256") for p in (out / "books").glob("*.manifest.sha256")]
    if (out / "books.manifest.sha256").is_file():
        found.append(FULL)
    return sorted(found)


def preset_dirs(out: Path, preset: str) -> list[Path]:
    """The directories whose files the manifest covers."""
    if preset == FULL:
        return [out / "books", out / "truth"]
    return [out / "books" / preset, out / "truth" / preset]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generator_sha256() -> str:
    """Digest of the generator's own source, so a manifest also records what wrote the books."""
    digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(source.name.encode() + b"\0" + source.read_bytes() + b"\0")
    return digest.hexdigest()


def hash_tree(out: Path, preset: str) -> dict[str, str]:
    """SHA-256 of every file directly in the preset's books and truth directories."""
    files: dict[str, str] = {}
    for root in preset_dirs(out, preset):
        for path in sorted(root.glob("*") if root.is_dir() else ()):
            if path.is_file() and not path.name.endswith(".manifest.sha256"):
                files[path.relative_to(out).as_posix()] = sha256_file(path)
    return files


def config_sha256(config: Path) -> str:
    return sha256_file(config)


def build(out: Path, preset: str, seed: int, config: Path) -> Manifest:
    return Manifest(seed=seed, preset=preset, generator_sha256=generator_sha256(),
                    config_sha256=config_sha256(config), files=hash_tree(out, preset))


def read(path: Path) -> Manifest:
    header: dict[str, str] = {}
    files: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            key, _, value = line[1:].partition(":")
            header[key.strip()] = value.strip()
        elif line.strip():
            digest, _, rel = line.partition("  ")
            files[rel] = digest
    return Manifest(seed=int(header["seed"]), preset=header["preset"],
                    generator_sha256=header["generator_sha256"],
                    config_sha256=header["config_sha256"], files=files)


def drift(out: Path, preset: str) -> list[str]:
    """Relative paths whose bytes differ from the manifest, are missing, or are not listed in it."""
    recorded = read(manifest_path(out, preset)).files
    actual = hash_tree(out, preset)
    return sorted(p for p in recorded.keys() | actual.keys() if recorded.get(p) != actual.get(p))
