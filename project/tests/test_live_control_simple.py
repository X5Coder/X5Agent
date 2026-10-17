"""Live/control simplified path — deterministic, boots nothing.

Guards the refactor that handed ALL live logic to the x5browser library:
- the old project-side live machinery is gone (no live-URL cache, no
  live_state.json, no viewer/relay healing, no WSL probing);
- the two links are read from the library (service.live_links);
- read-only status never boots (service.status_report / browser_status);
- the server derives its live card from the library + an in-memory pause.

The browser-stopped branch is simulated explicitly (endpoint probe patched to
False), so the test is identical whether or not a real browser is running.
Run: python tests/test_live_control_simple.py
"""
import queue
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from X5Browser import catalog                                    # noqa: E402
from X5Browser import service as svc                             # noqa: E402
from X5Browser import tools as bt                                # noqa: E402
from X5Browser.events import format_event                        # noqa: E402

# ---- the old live machinery must be gone --------------------------------
for dead in ("live_cached", "note_live", "_LIVE_URLS", "_LIVE_LOCK",
             "_browser_alive_now", "_restart_relay", "_revive_relay",
             "_boot_wsl", "_windows_can_reach_stack"):
    assert not hasattr(svc, dead), f"old live helper still present: {dead}"
for needed in ("live_links", "status_report", "_ensure_stack_up",
               "reap_stale_viewers"):
    assert hasattr(svc, needed), f"missing new helper: {needed}"
print("old live cache/heal helpers removed OK")

import web.server.server as srv                                  # noqa: E402
for dead in ("_read_live_state", "_write_live_state", "_set_live_state",
             "_clear_live_state", "_live_state_path", "_events_maybe_heal_viewer",
             "_events_maybe_heal_boundary", "_live_browser_alive_inside",
             "_viewer_distro_ip", "_viewer_url_with_host", "_tcp_reachable",
             "_wsl_run_kwargs"):
    assert not hasattr(srv, dead), f"old server live helper still present: {dead}"
for needed in ("_live_snapshot", "_live_active", "_live_pause_entry",
               "_live_pause_clear", "_LIVE_PAUSE"):
    assert hasattr(srv, needed), f"missing new server helper: {needed}"
print("old server live_state/heal helpers removed OK")

# ---- catalog surface (41 mirrored library tools + our show_preview = 42) ----
# (library 1.6.0+ ship 42 tools; its `image_search` is deliberately not
#  mirrored because the project's own image_search plugin owns that name)
assert len(catalog.ALL_TOOLS) == 42, len(catalog.ALL_TOOLS)
assert "browser_status" in catalog.ALL_TOOLS
assert "show_preview" in catalog.ALL_TOOLS
for _new in ("quick_search", "fetch_pages", "read_markdown", "extract_image",
             "manage_triggers"):
    assert _new in catalog.ALL_TOOLS, _new
    assert catalog.get_spec(_new) is not None, _new
assert catalog.get_spec("browser_status") is not None
assert catalog.get_spec("show_preview") is not None
# 1.5.2 handoff cleanup: take_control/agent_control are internal-only now.
assert catalog.get_spec("take_control") is None
assert catalog.get_spec("agent_control") is None
assert format_event("browser_status") == "Checking the browser"
assert format_event("show_preview") == "Showing you the live preview"
# new library params are exposed to the model
for tool, param in (("choose_option", "multiple"), ("read_console", "since"),
                    ("list_requests", "parts"), ("list_requests", "since_last_action"),
                    ("click_verify", "modifiers"), ("click_verify", "hold_ms")):
    spec = catalog.get_spec(tool) or {}
    assert param in (spec.get("inputs") or {}), f"{tool}.{param} missing"
print("catalog updated to 41 mirrored library tools + show_preview OK")

# ---- stopped browser, simulated deterministically ------------------------
_real_probe = svc._endpoint_alive_now
_real_browser, _real_started = svc._browser, svc._started
svc._endpoint_alive_now = lambda *a, **k: False
svc._browser, svc._started = None, False
try:
    links = svc.live_links()
    assert set(links) == {"running", "control", "preview", "owner"}, links
    assert links["running"] is False and links["control"] == "" and links["preview"] == ""
    print("live_links() stopped shape OK")

    state_before = (svc._browser, svc._started, svc._gen)
    out = bt.execute_browser_tool("browser_status", {})
    assert out["ok"] is True and out["running"] is False, out
    assert out["message"] == "browser stopped.", out
    assert out["event"] == "Checking the browser"
    assert (svc._browser, svc._started, svc._gen) == state_before, "status booted something"
    print("browser_status answers stopped without booting OK")

    for name in ("live_preview", "human_control",
                 "navigate", "page_snapshot"):
        res = bt.execute_browser_tool(name, {"to": "https://example.com"}
                                      if name == "navigate" else {})
        assert res["ok"] is False, (name, res)
        assert "browser stopped" in str(res.get("error") or ""), (name, res)
    # get_page_info is read-only too: it answers 'stopped' instead of failing.
    gpi = bt.execute_browser_tool("get_page_info", {})
    assert gpi["ok"] is True and gpi["message"] == "browser stopped.", gpi
    assert (svc._browser, svc._started) == (None, False), "a live call booted the browser"
    print("every live/page call refuses while stopped (no boot) OK")

    snap = srv._live_snapshot()
    assert snap["running"] is False and snap["control"] == "" and snap["preview"] == ""
    assert srv._live_active() is None, "a live card appeared with no browser"
    assert srv._live_pause_entry() is None
    print("server live snapshot/active read from the library OK")

    # Pause memory: invisible without a pending question, visible with one.
    with srv._PQ_LOCK:
        srv._LIVE_PAUSE["q-test"] = {"tool": "human_control", "view_only": False,
                                     "reason": "verify"}
    assert srv._live_pause_entry() is None
    with srv._PQ_LOCK:
        srv._PENDING_QUESTIONS["q-test"] = queue.Queue()
    assert srv._live_pause_entry()["qid"] == "q-test"
    srv._live_pause_clear("q-test")
    assert srv._live_pause_entry() is None
    with srv._PQ_LOCK:
        srv._PENDING_QUESTIONS.pop("q-test", None)
    print("in-memory live pause OK")
finally:
    svc._endpoint_alive_now = _real_probe
    svc._browser, svc._started = _real_browser, _real_started

# ---- library "running": deterministic simulation ------------------------
class _FakeBrowser:
    """Minimal stand-in for the library's X5Browser live-report surface."""

    def __init__(self, mode: str):
        self._mode = mode

    def live_report(self):
        return {"url": "http://127.0.0.1:8790/view?token=CTRL&scale=true",
                "preview_url": "http://127.0.0.1:8790/view?token=PREV&scale=true&view_only=true",
                "current_mode": self._mode, "live": True}

    def uptime_s(self):
        return 42.5


_real_probe = svc._endpoint_alive_now
_real_alive = svc._launcher_alive
_real_browser, _real_started = svc._browser, svc._started
svc._endpoint_alive_now = lambda *a, **k: True
svc._launcher_alive = lambda *a, **k: True
try:
    # owner = agent -> watch-only preview is the current card
    svc._browser, svc._started = _FakeBrowser("preview"), True
    links = svc.live_links()
    assert links == {"running": True,
                     "control": "http://127.0.0.1:8790/view?token=CTRL&scale=true",
                     "preview": "http://127.0.0.1:8790/view?token=PREV&scale=true&view_only=true",
                     "owner": "agent"}, links
    card = srv._live_active()
    assert card and card["mode"] == "preview" and card["view_only"] is True, card
    assert card["url"] == links["preview"], card
    assert srv._live_snapshot()["running"] is True
    print("running/agent -> preview card from the library OK")

    # browser_status reports the live session without booting
    st = bt.execute_browser_tool("browser_status", {})
    assert st["running"] is True and st["url"] == links["control"], st
    assert st["preview_url"] == links["preview"], st
    assert st["message"] == "browser running.", st
    print("browser_status reports the live session OK")

    # browser start attaches the library's preview link (UI player)
    out = {"ok": True}
    bt._attach_auto_preview(out)
    assert out.get("preview", {}).get("url") == links["preview"], out
    print("auto-preview copies the library preview link OK")

    # show_preview answers the watch-only preview token only (never a
    # control link in the floating player).
    _real_call = svc.call_tool
    _real_links = svc.live_links
    try:
        svc.call_tool = lambda *a, **k: dict(_FakeBrowser("preview").live_report())
        svc.live_links = lambda: {"running": True,
                                  "control": "http://127.0.0.1:8790/view?token=CTRL&scale=true",
                                  "preview": "http://127.0.0.1:8790/view?token=PREV&scale=true&view_only=true",
                                  "owner": "agent"}
        sp = bt.execute_browser_tool("show_preview", {})
        assert sp["ok"] is True and sp["view_only"] is True, sp
        assert "token=PREV" in sp["url"] and "token=CTRL" not in sp["url"], sp
        print("show_preview answers the preview token only OK")
    finally:
        svc.call_tool = _real_call
        svc.live_links = _real_links

    # owner = user -> the interactive control link is the current card
    svc._browser = _FakeBrowser("control")
    card = srv._live_active()
    assert card and card["mode"] == "control" and card["view_only"] is False, card
    assert card["url"] == links["control"], card
    print("running/user -> control card from the library OK")

    # a pending pause wins over the plain control card and carries its qid
    with srv._PQ_LOCK:
        srv._LIVE_PAUSE["q-live"] = {"tool": "human_control", "view_only": False,
                                     "reason": "captcha"}
        srv._PENDING_QUESTIONS["q-live"] = queue.Queue()
    card = srv._live_active()
    assert card and card["qid"] == "q-live" and card["mode"] == "control", card
    assert card["reason"] == "captcha", card
    print("pending pause -> resumable control card OK")
    srv._live_pause_clear("q-live")
    with srv._PQ_LOCK:
        srv._PENDING_QUESTIONS.pop("q-live", None)
    assert srv._live_active().get("qid") is None
finally:
    svc._endpoint_alive_now = _real_probe
    svc._launcher_alive = _real_alive
    svc._browser, svc._started = _real_browser, _real_started

print("ALL LIVE/CONTROL SIMPLICITY TESTS PASSED")