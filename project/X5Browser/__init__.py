"""
X5Browser integration — versioned single-folder browser layer.

Wrapper 1.6.2 over lib x5browser==1.6.2 (see VERSION).
- catalog.py: 42 tool specs (41 library + show_preview wrapper), all core plugins (always on, behind tool_search).
  The library's own `image_search` is deliberately NOT exposed: the project's
  `image_search` core plugin already owns that name/behavior (see catalog.py).
- service.py: single browser owner + sync/async bridge + screenshots.
  Explicit lifecycle: open_browser/restart_browser boot; every other tool
  fails with 'browser stopped' while down and never boots (any OS).
  Live links are whatever the library reports (live_links()) — no local cache.
- tools.py: validation + execution + normalized output.
- events.py: plain-English UI labels per tool.

Update path: bump VERSION lib pin, adjust catalog.py to the new TOOL_SCHEMAS.
"""
from __future__ import annotations

__version__ = "1.6.2"
LIB_PIN = "x5browser==1.6.2"

from .catalog import (  # noqa: E402
    ALL_TOOLS,
    CORE_TOOLS,
    DANGEROUS_TOOLS,
    DISCOVERABLE_TOOLS,
    get_spec,
)

__all__ = [
    "__version__",
    "LIB_PIN",
    "ALL_TOOLS",
    "CORE_TOOLS",
    "DISCOVERABLE_TOOLS",
    "DANGEROUS_TOOLS",
    "get_spec",
]