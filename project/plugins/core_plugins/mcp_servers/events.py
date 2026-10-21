"""Event for mcp_servers"""

SHOW_ARROW = True


def format_event(action: str = "", server: str = "", tool: str = "", **_) -> str:
    act = (action or "").strip().lower()
    if act == "tools":
        name = " ".join(str(server or "").split())[:28]
        return f"Reading MCP tools · {name}" if name else "Reading MCP tools"
    if act == "call":
        name = " ".join(str(tool or "").split())[:32]
        return f"Calling MCP · {name}" if name else "Calling MCP"
    return "Listing MCP servers"
