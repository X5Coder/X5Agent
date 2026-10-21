"""Event labels for reply_assembly (DISCOVERABLE; the part text is the reply,
never a step detail — the arrow only carries the model's reason)."""

SHOW_ARROW = False


def format_event(part: str = "", action: str = "next", part_index=None, **_) -> str:
    final = isinstance(action, str) and action.strip().lower() == "stop"
    try:
        n = int(part_index) if part_index is not None else 0
    except Exception:
        n = 0
    if n > 0:
        return f"Writing reply · {'final part' if final else 'part'} {n}"
    return "Writing reply · final part" if final else "Writing reply"
