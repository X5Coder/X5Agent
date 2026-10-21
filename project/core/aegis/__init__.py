"""XAP/1 protection primitives.

This package is intentionally small and side-effect free at import time.
The official launcher opts into the guards explicitly; importing the package
must never scan, repair, or touch user data.
"""

from __future__ import annotations

__all__ = [
    "import_guard",
    "launch_guard",
    "model_guard",
    "skill_guard",
    "time_guard",
]
