"""Load a module from an explicit file path.

The released build ships compiled extension modules (``.pyd`` / ``.so``)
instead of ``.py`` files, so a hardcoded ``store.py`` path no longer exists.
``load_from_path`` resolves the compiled sibling first and falls back to the
source file, which keeps one call site working in both trees.
"""

from __future__ import annotations

import importlib.util
import sysconfig
from pathlib import Path
from types import ModuleType


def _compiled_candidates(path: Path) -> list[Path]:
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    names = (path.stem + suffix, path.stem + ".pyd", path.stem + ".so")
    return [path.with_name(name) for name in names]


def resolve_module_file(path: Path) -> Path:
    """Return the file that actually holds the module (source or compiled)."""
    if path.is_file():
        return path
    for candidate in _compiled_candidates(path):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"module not found: {path}")


def load_from_path(path: Path | str, name: str) -> ModuleType:
    """Import a module by file path, tolerating a compiled release.

    A compiled extension exports ``PyInit_<stem>``, so the module name handed
    to importlib must be the file stem (``name`` is only a label used for
    plain sources, where it can be anything unique).
    """
    module_path = resolve_module_file(Path(path))
    if module_path.suffix != ".py":
        # ``store.cp312-win_amd64.pyd`` -> ``store`` (the ABI tag is not part
        # of the module name).
        name = module_path.name.split(".", 1)[0]
    spec = importlib.util.spec_from_file_location(name, str(module_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module
