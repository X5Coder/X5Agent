"""
VPS entrypoint - zero-knowledge user: just clone and run.
Responsibility: download built-in resources -> start agent loop.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    print("=" * 70)
    print("  X5Agent - Starting")
    print("=" * 70)

    # 1. Bundled embedding model (auto-download on first run, instant after).
    # Device (GPU/CPU) is detected once and cached. Missing/incomplete files
    # are fetched automatically (deltas only, resume on restart).
    try:
        from core.embedding.bootstrap import ensure_embedding_model
    except ImportError as e:
        print(f"[FATAL] embedding bootstrap not found: {e}", file=sys.stderr)
        return 1
    try:
        ensure_embedding_model()
    except Exception as e:
        print(f"\n❌ Embedding model not ready: {e}", file=sys.stderr)
        return 1

    # 2. Unify sandbox: ensure X5Agent dirs + one-time legacy storage -> Data migration
    try:
        from core.paths import ensure_x5agent, migrate_legacy_to_data_once

        ensure_x5agent()
        migrated = migrate_legacy_to_data_once()
        if any(migrated.values()):
            print(f"[OK] Migrated legacy storage to X5Agent/Data: {migrated}")
        else:
            print("[OK] X5Agent sandbox ready (Data is authoritative)")
    except Exception as e:
        print(f"[WARN] sandbox ensure failed: {e}", file=sys.stderr)

    # 3. Auto-embed tool descriptions for tool_search (detect change -> rebuild)
    try:
        from core.module_loader import load_from_path

        store_path = Path(__file__).resolve().parent / "plugins" / "tool-discovery" / "search" / "store.py"
        mod = load_from_path(store_path, "tool_discovery_store_startup")
        tools = mod.ensure_embeddings(auto_create=True)
        print(f"[OK] Tool embeddings ready: {len(tools)} discoverable tools indexed")
        for t in tools:
            print(f"  - {t['name']:15} -> {t['description'][:60]}...")
    except FileNotFoundError as e:
        print(f"[WARN] tool embeddings skipped (resources missing): {e}", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] tool embeddings failed: {e}", file=sys.stderr)

    # 4. Start agent loop (interactive terminal chat for VPS).
    print("\n" + "=" * 70)
    print("  ✅ Everything ready - starting agent loop (Ctrl+C or empty line quits)")
    print("=" * 70)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from core.agent.loop.engine import run_loop
    except Exception as e:
        print(f"[FATAL] agent loop not available: {e}", file=sys.stderr)
        return 1

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not user_input:
            break
        try:
            result = run_loop(user_input=user_input)
            print(f"\nAgent: {result.get('final_answer', '')}")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
