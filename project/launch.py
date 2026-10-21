"""Console entry point for compiled releases.

``build_release.py`` ships this file as ``launch.pyc`` (bytecode, not source),
so a compiled tree still has a start command:

    python launch.pyc            # console on 0.0.0.0:7777
    X5_PORT=9000 python launch.pyc

It contains no product logic: it only puts the release folder on ``sys.path``
and calls the console's own ``run()`` (or ``main()`` for a headless tree).
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)

# A release ships its own packages; the machine's global site-packages must
# never shadow them (a system torch/numpy of another version breaks the run).
os.environ.setdefault("PYTHONNOUSERSITE", "1")
try:
    import site as _site

    _user_site = _site.getusersitepackages()
    if isinstance(_user_site, str) and _user_site in sys.path:
        sys.path.remove(_user_site)
    _site.ENABLE_USER_SITE = False
except Exception:
    pass

DEFAULT_PORT = 7777


def _port() -> int:
    raw = (os.getenv("X5_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PORT
    return value if 1 <= value <= 65535 else DEFAULT_PORT


def _start() -> int:
    # The headless flavour ships without web/, so it starts the terminal agent
    # loop instead of the console.
    if os.path.isdir(os.path.join(HERE, "web")):
        from web.server.server import run

        return run(host="0.0.0.0", port=_port())

    from main import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_start())
