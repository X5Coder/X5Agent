"""
reply_assembly - deliver one very long reply in live parts (DISCOVERABLE).

The model writes the final reply as an ordered series of parts. Every call
carries one part plus an action:

  action="next"  -> more parts follow; the result tells the model to continue
  action="stop"  -> this is the last part; the engine ends the turn at once

The ENGINE (not this tool) is what relays each accepted part to the console
the moment it lands (as if the model had streamed it) and what joins the
parts into the single persisted reply. This tool only validates the call and
answers with a short, decisive continuation sentence — it never echoes the
part back, because the part is already in the model's own tool-call arguments
(echoing would double a very long reply in the context).

Bounds: part 1..30000 chars, action next|stop (missing defaults to next),
part index 1..200 (forwarded by the loop, never trusted for anything but the
label). No network, no files, stdlib only.
"""
from __future__ import annotations

from typing import Any

MAX_PART_CHARS = 30_000
MAX_PART_INDEX = 200
ACTIONS = ("next", "stop")


def _validate_part(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("part must be a string with this part's final text")
    if not value.strip():
        raise ValueError("part is empty — send the next part's text")
    if len(value) > MAX_PART_CHARS:
        raise ValueError(f"part too long ({len(value)} chars, max {MAX_PART_CHARS}) — "
                         "split it into smaller parts")
    return value


def _validate_action(value: Any) -> str:
    if value is None:
        return "next"
    if not isinstance(value, str):
        raise TypeError('action must be "next" or "stop"')
    action = value.strip().lower()
    if not action:
        return "next"
    if action not in ACTIONS:
        raise ValueError('action must be "next" (more parts follow) or '
                         '"stop" (last part)')
    return action


def _part_index(value: Any) -> int:
    """Loop-forwarded 1-based index (label only). Never raises."""
    try:
        n = int(value)
    except Exception:
        return 1
    if n < 1 or n > MAX_PART_INDEX:
        return 1
    return n


def execute(part: Any = None, action: Any = None, part_index: Any = None) -> dict:
    text = _validate_part(part)
    act = _validate_action(action)
    index = _part_index(part_index)
    chars = len(text)
    final = act == "stop"
    label = f"{'final part' if final else 'part'} {index}"
    if final:
        message = (f"Final part {index} received ({chars:,} chars). The complete answer is "
                   "already shown to the user. Do not repeat or summarize it — end your "
                   "turn now with no text and no more tool calls.")
    else:
        message = (f"Part {index} received ({chars:,} chars). Continue NOW with part "
                   f"{index + 1}: write only the next untold content of the same answer, "
                   "never repeat earlier parts, no 'Part N' heading, and call "
                   "reply_assembly again with it. Use action=\"stop\" when this new part "
                   "is the last one.")
    return {
        "event": f"Writing reply · {label}",
        "ok": True,
        "accepted": True,
        "action": act,
        "part_index": index,
        "chars": chars,
        "final": final,
        "message": message,
    }
