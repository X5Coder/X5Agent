"""
Auto Memory Service - the LLM pipeline behind the four memories.

Every TRIGGER_EVERY (30) messages one background pass runs:
  1. For each internal memory ("user", "agent", "facts"): read the current
     memory plus the recent conversation through that memory's own system
     prompt, and store the FULL updated memory as "- " bullet text.
  2. For every memory that now exceeds MAX_MEMORY_TOKENS: run the single
     unified summarizer (memory_summary_system.md) to compress it back under
     the cap without losing information.

The agent-owned memory ("notes") is written by the `memory` tool, not here,
but it is summarized here too as a safety net.

Ownership: core/memory. Lifecycle: called after _persist_conversation, runs in
a daemon thread, bounded (3 retries max), never raises to the caller.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import service

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS_DIR = _PROJECT_ROOT / "prompts"
_LEGACY_DB = _PROJECT_ROOT / "storage" / "database"

# One system prompt per internal memory.
_EXTRACT_PROMPTS = {
    service.USER: "user_memory_system.md",
    service.AGENT: "agent_memory_system.md",
    service.FACTS: "facts_memory_system.md",
}
# One unified summarizer for every memory (including the agent's own notes).
SUMMARY_PROMPT = "memory_summary_system.md"

TRIGGER_EVERY = 30  # messages between auto-memory passes
_RECENT_MESSAGES = 30  # conversation tail given to each extractor
_MAX_MESSAGE_CHARS = 400
# A background extractor that returns a suspiciously smaller memory than it
# started with is treated as a failure and the existing memory is kept.
_SHRINK_GUARD = 0.4


def _db_dirs() -> tuple[Path, Path]:
    # Unified: Data first, legacy fallback (read-only).
    legacy = _PROJECT_ROOT / "storage" / "database"
    try:
        import sys as _sys
        if str(_PROJECT_ROOT) not in _sys.path:
            _sys.path.insert(0, str(_PROJECT_ROOT))
        from core.paths import data_db_dir as _dd
        return _dd(), legacy
    except Exception:
        return legacy, legacy


def _resolve_db(name: str) -> Path:
    data_db, legacy_db = _db_dirs()
    cand = data_db / name
    if cand.exists():
        return cand
    return legacy_db / name


def _read_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _load_conversation() -> list[dict]:
    for p in (_resolve_db("conversation.json"), _LEGACY_DB / "conversation.json"):
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [m for m in data if isinstance(m, dict)]
        except Exception:
            continue
    return []


def _recent_block(msgs: list[dict]) -> str:
    lines: list[str] = []
    for m in msgs[-_RECENT_MESSAGES:]:
        role = m.get("role", "user")
        content = str(m.get("content", ""))[:_MAX_MESSAGE_CHARS]
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "No messages"


def _call_llm(system_prompt: str, user_content: str) -> str:
    from core.ai_models.store import require_text_model
    from core.ai_models.engine import chat as universal_chat

    _record = require_text_model()  # refuses clearly when no text model is set
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            content = universal_chat(
                _record,
                [{"role": "user", "content": user_content}],
                {"system": system_prompt},
            )
        except Exception as e:  # noqa: BLE001 - retried below, re-raised after 3
            last_err = e
            if attempt == 3:
                raise
            time.sleep(attempt)
            continue
        if isinstance(content, str) and content.strip():
            return content.strip()
        last_err = RuntimeError("LLM returned empty for auto memory")
        if attempt < 3:
            time.sleep(attempt)
    raise RuntimeError(f"LLM returned empty for auto memory after 3 tries: {last_err}")


def _extract(kind: str) -> bool:
    """Update one internal memory from the recent conversation. Returns changed."""
    system_prompt = _read_prompt(_EXTRACT_PROMPTS[kind])
    if not system_prompt:
        raise FileNotFoundError(f"memory prompt missing for {kind}")
    current = service.get_text(kind)
    user_content = (
        "[Current memory]\n"
        + (current if current else "(empty)")
        + "\n\n[Recent conversation]\n"
        + _recent_block(_load_conversation())
    )
    raw = _call_llm(system_prompt, user_content)
    if not raw.strip():
        return False
    before = service.estimate_tokens(current)
    after = service.estimate_tokens(raw)
    if before >= 50 and after < before * _SHRINK_GUARD:
        # Suspect truncated/failed output: keep the existing memory.
        return False
    service.set_text(kind, raw)
    return True


def _summarize(kind: str) -> bool:
    """Compress one memory with the unified summarizer. Returns changed."""
    system_prompt = _read_prompt(SUMMARY_PROMPT)
    if not system_prompt:
        raise FileNotFoundError("memory_summary_system.md is missing")
    current = service.get_text(kind)
    if not current.strip():
        return False
    raw = _call_llm(system_prompt, current)
    if not raw.strip():
        return False
    if service.estimate_tokens(raw) >= service.estimate_tokens(current):
        # No compression gained: keep the original, never grow it.
        return False
    service.set_text(kind, raw)
    return True


def summarize_if_needed(kind: str) -> bool:
    """Summarize a memory only when it is over the token cap. Never raises."""
    try:
        kind = service._validate_kind(kind)
    except Exception:
        return False
    try:
        if not service.is_over_limit(kind):
            return False
        return _summarize(kind)
    except Exception as e:  # noqa: BLE001 - summarize is best-effort
        print(f"[auto_memory] summarize {kind} failed: {e}")
        return False


def _should_trigger() -> bool:
    conv = _load_conversation()
    n = len(conv)
    marker = (_db_dirs()[0]) / ".auto_memory_last_trigger"
    legacy_marker = _LEGACY_DB / ".auto_memory_last_trigger"
    last = 0
    for cand in (marker, legacy_marker):
        if cand.exists():
            try:
                last = int(cand.read_text(encoding="utf-8").strip())
                break
            except Exception:
                continue
    if n >= TRIGGER_EVERY and n % TRIGGER_EVERY == 0 and n != last:
        return True
    return n - last >= TRIGGER_EVERY


def _mark_triggered(n: int) -> None:
    try:
        marker = _db_dirs()[0] / ".auto_memory_last_trigger"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(n), encoding="utf-8")
    except Exception:
        pass


def _run_pass() -> dict:
    """Extract the three internal memories then compress anything over cap."""
    result: dict[str, bool] = {}
    for kind in (service.USER, service.AGENT, service.FACTS):
        try:
            result[f"{kind}_extracted"] = _extract(kind)
        except Exception as e:  # noqa: BLE001
            print(f"[auto_memory] extract {kind} failed: {e}")
            result[f"{kind}_extracted"] = False
        time.sleep(1)
        result[f"{kind}_summarized"] = summarize_if_needed(kind)
    # Safety net: the agent's own memory is tool-written, summarize here too.
    result["notes_summarized"] = summarize_if_needed(service.NOTES)
    return result


def maybe_trigger_auto_memory() -> dict | None:
    """Run the auto-memory pass in the background when the 30-message boundary
    is reached. Returns None when not triggered, else a small status dict."""
    if not _should_trigger():
        return None
    n = len(_load_conversation())

    import threading

    def background() -> None:
        try:
            outcome = _run_pass()
            print(f"[auto_memory] pass at {n} msgs: {outcome}")
        except Exception as e:  # noqa: BLE001 - background must never crash
            print(f"[auto_memory] pass failed at {n}: {e}")
        finally:
            _mark_triggered(n)

    threading.Thread(target=background, daemon=True, name="auto-memory").start()
    return {"triggered_at": n, "status": "background started"}


def trigger_now_for_test() -> dict:
    """Synchronous pass for tests (no thread, no network when _call_llm is mocked)."""
    outcome = _run_pass()
    _mark_triggered(len(_load_conversation()))
    return outcome
