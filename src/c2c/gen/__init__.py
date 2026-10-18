"""Books and truth generation, reproducible from a seed.

`generate(preset, seed, out)` writes `<out>/books/<preset>/`, `<out>/truth/<preset>/` and
`<out>/books/<preset>.manifest.sha256`; `verify(out)` lists the files that no longer match
their manifest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import manifest, small

PRESETS = {"small": small.write}


def generate(preset: str, seed: int, out: str | Path) -> manifest.Manifest:
    """Write the preset's files under `out`, replacing any earlier copy, and record the manifest."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r}; available: {', '.join(sorted(PRESETS))}")
    out = Path(out)
    for root in manifest.preset_dirs(out, preset):
        shutil.rmtree(root, ignore_errors=True)
    PRESETS[preset](seed, out)
    built = manifest.build(out, preset, seed)
    manifest.manifest_path(out, preset).write_text(built.render(), encoding="utf-8", newline="\n")
    return built


def verify(out: str | Path, preset: str | None = None) -> dict[str, list[str]]:
    """Drifted files per preset (empty list: in step). Every manifest under `out` is checked
    unless `preset` names one. Raises FileNotFoundError when there is no manifest to check."""
    out = Path(out)
    if preset is not None:
        presets = [preset]
    else:
        presets = sorted(p.name.removesuffix(".manifest.sha256")
                         for p in (out / "books").glob("*.manifest.sha256"))
    if not presets:
        raise FileNotFoundError(f"no books manifest under {out / 'books'}")
    result = {}
    for name in presets:
        path = manifest.manifest_path(out, name)
        if not path.is_file():
            raise FileNotFoundError(f"no books manifest at {path}")
        result[name] = manifest.drift(out, name)
    return result
