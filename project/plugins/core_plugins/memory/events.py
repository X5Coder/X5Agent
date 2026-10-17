"""Event for memory"""

SHOW_ARROW = True


def format_event(action: str = "", **_) -> str:
    a = (action or "").strip().lower()
    if a == "read":
        return "Reading memory"
    if a == "write":
        return "Writing memory"
    if a == "edit":
        return "Editing memory"
    if a == "delete":
        return "Clearing memory"
    return "Managing memory"
