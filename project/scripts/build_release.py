"""Build a source-hidden X5Agent release tree.

The release ships the application code as compiled extension modules
(``.pyd`` on Windows, ``.so`` on Linux) and keeps every non-Python asset
(prompts, specs, the web console) as plain files. No ``.py`` file of the
application ends up in the archive, so the shipped code cannot be read.

Usage (run from the repository root):

    python scripts/build_release.py --out dist --platform windows
    python scripts/build_release.py --out dist --platform linux --flavor headless

``--flavor headless`` drops the web console (agent + tools only);
``--flavor full`` (default) keeps the console and its static assets.

Cross-compilation is not possible: a Windows tree must be built on Windows,
a Linux tree on Linux, and a macOS tree on macOS.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = REPO_ROOT / "project"

# Never compiled, never shipped: tests, model weights, caches, virtualenvs.
SKIP_DIRS = {
    "tests",
    "models",
    "embedding_model",  # ~1.2GB weights, auto-downloaded on first run
    "x5agent.egg-info",
    ".cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
}
# Shipped as data only (already-built frontend bundles).
DATA_ONLY_DIRS = {"web/frontend", "web/studio-pdf"}
# Console assets the headless flavour does not need.
CONSOLE_DIRS = {"web"}


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def collect_sources(flavor: str) -> tuple[list[Path], list[Path]]:
    """Return (python_sources, data_files) for the requested flavour."""
    sources: list[Path] = []
    data: list[Path] = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        root_path = Path(root)
        rel_root = _rel(root_path) if root_path != PROJECT_ROOT else ""
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        if flavor == "headless":
            # web/ holds the console; the headless build keeps no HTTP surface.
            dirs[:] = [d for d in dirs if not (rel_root == "" and d == "web")]
        for name in sorted(files):
            path = root_path / name
            rel = _rel(path)
            if name.endswith((".pyc", ".pyo")):
                continue
            if name.endswith(".py"):
                sources.append(path)
                continue
            if any(rel.startswith(prefix + "/") for prefix in DATA_ONLY_DIRS):
                data.append(path)
                continue
            if rel.startswith("web/") and rel.endswith((".py",)):
                continue
            data.append(path)
    return sources, data


def copy_tree(flavor: str, staging: Path) -> None:
    """Copy the project into staging (sources still present at this point)."""
    ignore = shutil.ignore_patterns(*SKIP_DIRS, "*.pyc", "*.pyo")
    for item in PROJECT_ROOT.iterdir():
        if item.name in SKIP_DIRS:
            continue
        if flavor == "headless" and item.name == "web":
            continue
        target = staging / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore)
        else:
            shutil.copy2(item, target)
    # Root-level product files that live outside project/.
    for name in ("updates.json", "LICENSE", "README.md", "setup.sh"):
        source = REPO_ROOT / name
        if source.is_file():
            shutil.copy2(source, staging / name)
    if flavor == "full":
        seed_dir = REPO_ROOT / "storage" / "vector"
        if seed_dir.is_dir():
            shutil.copytree(seed_dir, staging / "storage" / "vector", dirs_exist_ok=True)


def write_requirements(staging: Path) -> None:
    """Freeze the declared dependencies into requirements.txt for the release.

    A compiled tree cannot be installed with ``pip install -e .`` (there is no
    buildable package left), so the installer installs this list instead.
    """
    pyproject = staging / "pyproject.toml"
    if not pyproject.is_file():
        return
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 build hosts
        import tomli as tomllib
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a broken file is a build error
        raise SystemExit(f"cannot read pyproject.toml: {exc}") from exc
    deps = (data.get("project") or {}).get("dependencies") or []
    if not deps:
        return
    (staging / "requirements.txt").write_text(
        "\n".join(str(dep) for dep in deps) + "\n", encoding="utf-8"
    )
    print(f"requirements.txt: {len(deps)} pinned dependencies")


def _compiler_available() -> bool:
    if platform.system() == "Windows":
        probe = shutil.which("cl")
        if probe:
            return True
        vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
        return vswhere.is_file()
    return shutil.which("gcc") is not None or shutil.which("cc") is not None


def compile_sources(staging: Path, jobs: int) -> int:
    """Compile every .py under staging to an extension module, then delete it."""
    sources = [
        p
        for p in staging.rglob("*.py")
        if not any(part in SKIP_DIRS for part in p.parts)
        # launch.py is the compiled tree's entry point and ships as bytecode
        # (see _ship_launcher), so it is never turned into an extension module.
        and p.name != "launch.py"
    ]
    if not sources:
        raise SystemExit("no python sources found in the staging tree")

    # Folder names such as ``tool-discovery`` are not importable identifiers, so
    # those files are compiled under a safe name and renamed back afterwards
    # (the app loads them by file path, never by dotted name).
    def rel(path: Path) -> str:
        return path.relative_to(staging).as_posix()

    dashed = [p for p in sources if any("-" in part for part in p.relative_to(staging).parts)]
    plain = [p for p in sources if p not in dashed]
    # The last dotted component decides the C-level init symbol, so it must
    # stay the real file name (store -> PyInit_store, __init__ -> PyInit___init__).
    safe_names = {}
    for index, path in enumerate(dashed):
        stem = path.stem
        safe_names[path] = f"x5d{index}.{'__init__' if stem == '__init__' else stem}"

    setup_py = staging / "_x5_build_setup.py"
    modules = ",\n    ".join(repr(rel(p)) for p in plain)
    extras = ",\n    ".join(
        f"Extension({safe_names[p]!r}, [{rel(p)!r}])" for p in dashed
    ) or ""
    # The __main__ guard matters: cythonize starts worker processes that
    # re-import this file, and without it Windows aborts the pool.
    setup_py.write_text(
        "from setuptools import setup, Extension\n"
        "from Cython.Build import cythonize\n"
        "\n"
        "MODULES = [\n"
        f"    {modules},\n"
        "]\n"
        "EXTRA = [\n"
        f"    {extras},\n"
        "]\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    setup(\n"
        "        name='x5agent-build',\n"
        "        ext_modules=cythonize(\n"
        "            MODULES,\n"
        "            nthreads=%d,\n"
        "            language_level=3,\n"
        "            compiler_directives={'binding': True, 'language_level': 3},\n"
        "            quiet=True,\n"
        "        ) + cythonize(\n"
        "            EXTRA,\n"
        "            nthreads=%d,\n"
        "            language_level=3,\n"
        "            compiler_directives={'binding': True, 'language_level': 3},\n"
        "            quiet=True,\n"
        "        ),\n"
        "        script_args=['build_ext', '--inplace', '--parallel', '%d'],\n"
        "    )\n" % (jobs, jobs, jobs),
        encoding="utf-8",
    )

    # --inplace needs the temporary package folders to exist already.
    for name in safe_names.values():
        package = name.rpartition(".")[0]
        if package:
            (staging / package).mkdir(parents=True, exist_ok=True)

    started = time.time()
    result = subprocess.run(
        [sys.executable, str(setup_py)],
        cwd=str(staging),
        capture_output=True,
        text=True,
    )
    setup_py.unlink(missing_ok=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit("compilation failed")

    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    built = 0
    for source in plain:
        stem = source.with_suffix("")
        found = next(
            (
                c
                for c in (
                    stem.with_name(stem.name + suffix),
                    stem.with_name(stem.name + ".pyd"),
                    stem.with_name(stem.name + ".so"),
                )
                if c.is_file()
            ),
            None,
        )
        if found is None:
            raise SystemExit(f"missing compiled output for {rel(source)}")
        source.unlink()
        built += 1
    for source in dashed:
        stem = source.with_suffix("")
        safe = safe_names[source]
        # A dashed folder is not a package, so setuptools drops it from the
        # module name and writes the file at the staging root.
        package, _, leaf = safe.rpartition(".")
        build_dir = staging / package if package else staging
        compiled = next(
            (
                c
                for c in (
                    build_dir / (leaf + suffix),
                    build_dir / (leaf + ".pyd"),
                    build_dir / (leaf + ".so"),
                )
                if c.is_file()
            ),
            None,
        )
        if compiled is None:
            raise SystemExit(f"missing compiled output for {rel(source)}")
        compiled.replace(stem.with_name(stem.name + suffix))
        source.unlink()
        built += 1
    for name in safe_names.values():
        package = name.rpartition(".")[0]
        if package:
            shutil.rmtree(staging / package, ignore_errors=True)
    for leftover in list(staging.rglob("*.c")) + list(staging.rglob("*.pyi")):
        leftover.unlink(missing_ok=True)
    for cache in staging.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    for leftover in staging.glob("build"):
        shutil.rmtree(leftover, ignore_errors=True)
    _ship_launcher(staging)
    print(f"compiled {built} modules in {time.time() - started:.0f}s")
    return built


def _ship_launcher(staging: Path) -> None:
    """Ship launch.py as launch.pyc so a compiled tree still has an entry point.

    The archive keeps zero .py files, while ``python launch.pyc`` works on the
    interpreter the build targeted.
    """
    source = staging / "launch.py"
    if not source.is_file():
        return
    import py_compile

    target = staging / "launch.pyc"
    py_compile.compile(str(source), cfile=str(target), doraise=True)
    source.unlink()
    if not target.is_file():
        raise SystemExit("launcher bytecode was not written")


def write_manifest(staging: Path, flavor: str) -> dict:
    version = "0.0.0"
    updates = staging / "updates.json"
    if updates.is_file():
        try:
            version = json.loads(updates.read_text(encoding="utf-8")).get("version", version)
        except ValueError:
            pass
    manifest = {
        "product": "X5Agent",
        "version": version,
        "flavor": flavor,
        "platform": f"{platform.system().lower()}-{platform.machine().lower()}",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "compiled": True,
    }
    (staging / "BUILD.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    parser.add_argument("--flavor", choices=("full", "headless"), default="full")
    parser.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 2)))
    parser.add_argument("--keep-tree", action="store_true", help="do not zip the tree")
    args = parser.parse_args(argv)

    if not _compiler_available():
        raise SystemExit(
            "no C compiler found: install MSVC Build Tools (Windows) or build-essential (Linux)"
        )

    tag = f"{platform.system().lower()}-{platform.machine().lower()}"
    name = f"x5agent-{tag}-{args.flavor}"
    out_dir = (REPO_ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="x5build-")) / name
    staging.mkdir(parents=True)

    print(f"staging {name} in {staging}")
    copy_tree(args.flavor, staging)
    write_requirements(staging)
    compile_sources(staging, args.jobs)
    manifest = write_manifest(staging, args.flavor)

    if args.keep_tree:
        target = out_dir / name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(staging, target)
    else:
        archive = out_dir / name
        shutil.make_archive(str(archive), "zip", root_dir=str(staging.parent), base_dir=name)
        target = archive.with_suffix(".zip")
    leaked = [p for p in staging.rglob("*.py")]
    print(f"built {target}")
    print(f"manifest {manifest}")
    print(f"python files left in tree: {len(leaked)}")
    return 0 if not leaked else 1


if __name__ == "__main__":
    raise SystemExit(main())
