"""The books manifest: a SHA-256 for every generated file, and the check against drift.

Layout of `<out>/books/<preset>.manifest.sha256`: header lines `# seed: N`,
`# preset: NAME`, `# generator_sha256: HEX`, then one `HEX  relative/path` line per
file, sorted by path. Paths are relative to `<out>` and use `/`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

HEADER_KEYS = ("seed", "preset", "generator_sha256")


@dataclass(frozen=True)
class Manifest:
    seed: int
    preset: str
    generator_sha256: str
    files: dict[str, str]  # relative path -> sha256

    def render(self) -> str:
        lines = [f"# {key}: {getattr(self, key)}" for key in HEADER_KEYS]
        lines += [f"{digest}  {path}" for path, digest in sorted(self.files.items())]
        return "\n".join(lines) + "\n"


def manifest_path(out: Path, preset: str) -> Path:
    return out / "books" / f"{preset}.manifest.sha256"


def preset_dirs(out: Path, preset: str) -> list[Path]:
    """The directories whose files the manifest covers."""
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
    """SHA-256 of every file under the preset's books and truth directories."""
    files: dict[str, str] = {}
    for root in preset_dirs(out, preset):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files[path.relative_to(out).as_posix()] = sha256_file(path)
    return files


def build(out: Path, preset: str, seed: int) -> Manifest:
    return Manifest(seed=seed, preset=preset, generator_sha256=generator_sha256(),
                    files=hash_tree(out, preset))


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
                    generator_sha256=header["generator_sha256"], files=files)


def drift(out: Path, preset: str) -> list[str]:
    """Relative paths whose bytes differ from the manifest, are missing, or are not listed in it."""
    recorded = read(manifest_path(out, preset)).files
    actual = hash_tree(out, preset)
    return sorted(p for p in recorded.keys() | actual.keys() if recorded.get(p) != actual.get(p))
