"""Build prebuilt embedding seeds shipped with the project.

The runtime stores live in ChromaDB (X5Agent/Data/vector/chroma). This
script snapshots them as *.seed.json into project/storage/vector/
(committed, small JSON), so a fresh copy gets instant tool/trigger search
when specs are unchanged (the store imports the seed into Chroma on first
run).

INCREMENTAL: the stores keep a per-spec fingerprint on every entry —
ensure_* reuses stored embeddings whose fingerprint still matches and
embeds ONLY new/changed specs (never all from scratch). So this script
updates just what is missing, then snapshots the store into the seed.

- Fully automatic: a missing/incomplete project/embedding_model/ folder
  downloads on its own (deltas only, resume on restart).
- --snapshot-only : skip the model entirely; snapshot the current runtime
  stores as seeds after verifying their hash matches current specs
  (instant, used when embeddings are already built).

Usage (from project/):
    python scripts/build_embedding_seed.py [--snapshot-only]
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_store_module(relative: str, name: str):
    path = PROJECT_ROOT / relative
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"store not found: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def _write_seed(label: str, seed_name: str, snapshot: dict | None) -> bool:
    """Write one seed JSON after a successful snapshot. No model load."""
    dst = PROJECT_ROOT / "storage" / "vector" / seed_name
    if not isinstance(snapshot, dict) or not snapshot.get("count"):
        print(f"[seed] SKIP {label}: runtime store is empty or stale - run without --snapshot-only")
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dst)
        print(f"[seed] wrote storage/vector/{seed_name} "
              f"({dst.stat().st_size / 1024:.0f} KB, {label}: {len(snapshot.get('tools') or snapshot.get('triggers') or [])})")
        return True
    except Exception as e:
        print(f"[seed] SKIP {seed_name}: {e}", file=sys.stderr)
        return False


def main(argv: list[str]) -> int:
    tool_store = _load_store_module("plugins/tool-discovery/search/store.py", "seed_tool_store")
    trigger_store = _load_store_module(
        "plugins/trigger-discovery/search/store.py", "seed_trigger_store")

    if "--snapshot-only" in argv:
        # Instant: import any shipped seed into Chroma (never embeds), then snapshot.
        tool_store.ensure_embeddings(auto_create=False)
        trigger_store.ensure_embeddings(auto_create=False)
        ok_tools = _write_seed("tools", "tool_embeddings.seed.json", tool_store.snapshot_seed())
        ok_triggers = _write_seed(
            "triggers", "trigger_embeddings.seed.json", trigger_store.snapshot_seed())
        if not (ok_tools and ok_triggers):
            print("[seed] ERROR: seeds not written", file=sys.stderr)
            return 1
        print("[seed] done - commit the *.seed.json files with the project.")
        return 0

    from core.embedding.bootstrap import detect_device, ensure_embedding_model

    try:
        ensure_embedding_model()
    except Exception as e:
        print(f"[seed] ERROR: {e}", file=sys.stderr)
        return 1
    print(f"[seed] embedding model ready (device: {detect_device()}, auto)")

    # ensure_* is incremental (per-spec fingerprints): unchanged specs reuse
    # their stored vectors, so this embeds only what is new/changed and is
    # instant on a healthy copy.
    tools = tool_store.ensure_embeddings(auto_create=True)
    triggers = trigger_store.ensure_embeddings(auto_create=True)
    print(f"[seed] runtime stores ready: {len(tools)} tools, {len(triggers)} triggers")

    built = 0
    if _write_seed("tools", "tool_embeddings.seed.json", tool_store.snapshot_seed()):
        built += 1
    if _write_seed("triggers", "trigger_embeddings.seed.json", trigger_store.snapshot_seed()):
        built += 1
    if not built:
        print("[seed] ERROR: no seeds written", file=sys.stderr)
        return 1
    print("[seed] done - commit the *.seed.json files with the project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
