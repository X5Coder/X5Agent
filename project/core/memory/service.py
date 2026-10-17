"""
Text memory store - THE single owner of the four long-term memories.

The memory system has exactly four memories, each a bounded text block of
"- " bullet lines (no blank lines, no headings):

- "user"  : the user's profile (who he is). Sent in the context every request.
- "agent" : the agent's personality + how the user wants it to work. Sent
            every request as the agent's identity (first, right after the
            system prompt), because it IS the agent's personality.
- "facts" : confirmed standalone facts about the user. Sent in the context.
- "notes" : the agent's OWN permanent memory. Never sent automatically; reached
            only through the `memory` tool. Summarized when over the cap.

Memories "user"/"agent"/"facts" are internal (the agent cannot edit them);
"notes" is the single memory tool. When any memory exceeds MAX_MEMORY_TOKENS
it is auto-summarized back under the cap (core/memory/auto_memory.py).

Ownership: core/memory. Lifecycle: read/write per request, persisted in
X5Agent/Data/database. Reads tolerate the legacy formats once and never raise.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Memory kinds (the four). Order is not a priority - callers decide order.
USER = "user"
AGENT = "agent"
FACTS = "facts"
NOTES = "notes"
KINDS = (USER, AGENT, FACTS, NOTES)

# File per memory. "notes" is a fresh name; the other three keep their
# historical filenames so a legacy install reads/upgrades in place.
_FILES = {
    USER: "user_memory.json",
    AGENT: "agent_memory.json",
    FACTS: "facts_memory.json",
    NOTES: "agent_notes.json",
}

# A memory is compressed once it reaches this many estimated tokens.
MAX_MEMORY_TOKENS = 1000
# Conservative chars-per-token estimate (safe for mixed Arabic/English text:
# Arabic runs closer to 2-3 chars/token, English near 4). Slightly
# over-estimating only triggers an earlier (lossless) compression.
CHARS_PER_TOKEN = 3
# Hard safety cap so a runaway write can never grow a single memory unbounded.
MAX_MEMORY_CHARS = 20000
# One bullet line is capped so a single fact can never dominate the memory.
MAX_LINE_CHARS = 1500


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _storage_path(kind: str) -> Path:
    # Writes ALWAYS to X5Agent/Data (zip backup); legacy fallback only when the
    # resolver is unavailable.
    name = _FILES[kind]
    try:
        import sys as _sys
        _proj = _project_root()
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from core.paths import data_db_file
        return data_db_file(name)
    except Exception:
        return _project_root() / "storage" / "database" / name


def _legacy_path(kind: str) -> Path:
    return _project_root() / "storage" / "database" / _FILES[kind]


def _validate_kind(kind: str) -> str:
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError(f"unknown memory kind: {kind!r} (expected one of {KINDS})")
    return kind


def _legacy_to_text(data: object) -> str:
    """Convert a legacy memory file body into bullet text. Never raises."""
    lines: list[str] = []
    try:
        if isinstance(data, dict):
            if isinstance(data.get("text"), str):
                return data["text"]
            for k, v in data.items():
                val = v.get("value") if isinstance(v, dict) else v
                if val is None:
                    continue
                lines.append(f"- {k}: {val}")
        elif isinstance(data, list):
            for x in data:
                if not isinstance(x, dict):
                    continue
                key = x.get("key")
                content = x.get("content") or x.get("value") or ""
                lines.append(f"- {key}: {content}" if key else f"- {content}")
    except Exception:
        return ""
    return "\n".join(lines)


def _load_raw(kind: str) -> str:
    for cand in (_storage_path(kind), _legacy_path(kind)):
        if not cand.exists():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            return data["text"]
        converted = _legacy_to_text(data)
        if converted:
            return converted
    return ""


def _normalize(text: str) -> str:
    """One bullet per line, no blank lines, bounded. Never raises."""
    if not isinstance(text, str):
        return ""
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Drop common markdown fences the model may add around the output.
        if line.startswith("```"):
            continue
        if not line.startswith("-"):
            line = "- " + line.lstrip("-•* \t")
        else:
            line = "- " + line.lstrip("- \t")
        line = line[:MAX_LINE_CHARS]
        out.append(line)
    joined = "\n".join(out)
    if len(joined) > MAX_MEMORY_CHARS:
        joined = joined[:MAX_MEMORY_CHARS]
    return joined


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for a memory text (never raises)."""
    if not isinstance(text, str) or not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def get_text(kind: str) -> str:
    """Current memory text ("" when empty). Never raises."""
    kind = _validate_kind(kind)
    try:
        return _load_raw(kind)
    except Exception:
        return ""


def set_text(kind: str, text: str) -> dict:
    """Replace a memory with normalized text. Returns {kind, text, tokens}."""
    kind = _validate_kind(kind)
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = _normalize(text)
    _write(kind, normalized)
    return {"kind": kind, "text": normalized, "tokens": estimate_tokens(normalized)}


def append_text(kind: str, text: str) -> dict:
    """Append new bullet lines to a memory (de-duplicated line-wise)."""
    kind = _validate_kind(kind)
    current = get_text(kind)
    existing = {ln.strip() for ln in current.splitlines() if ln.strip()}
    addition = _normalize(text)
    keep: list[str] = []
    for ln in addition.splitlines():
        if ln.strip() and ln.strip() not in existing:
            keep.append(ln)
    merged = current + ("\n" if current and keep else "") + "\n".join(keep)
    return set_text(kind, merged)


def clear(kind: str) -> int:
    """Empty a memory. Returns the number of bullet lines removed."""
    kind = _validate_kind(kind)
    n = sum(1 for ln in get_text(kind).splitlines() if ln.strip())
    _write(kind, "")
    return n


def token_count(kind: str) -> int:
    return estimate_tokens(get_text(kind))


def is_over_limit(kind: str) -> bool:
    return token_count(kind) > MAX_MEMORY_TOKENS


def _write(kind: str, text: str) -> None:
    path = _storage_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "text": text,
        "tokens": estimate_tokens(text),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
