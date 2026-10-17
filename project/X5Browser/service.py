"""
X5Browser service — single owner of the browser instance and its lifecycle.

Lifecycle: open_browser/restart_browser -> create -> start -> use -> (close_browser stops it).
Any other tool while the browser is stopped FAILS with "browser stopped"
instead of booting anything (no WSL boot, no systemd units, no Chrome).
The library keeps ONE profile inside itself (created on first start, reused
afterwards); this service never moves it, so backup/restore stays trivial.

Sync/async bridge: the agent loop is synchronous while x5browser is async.
One daemon thread owns a dedicated event loop; every call is scheduled on it
with a bounded timeout (120s). Acting tools run exactly once (no blind retry);
read-only tools retry up to 3 times on transient transport failures only.
A transient CDP drop first tries the library's repair_session() re-attach
(1.5.1+: same browser/process/tabs, never boots, refuses after an explicit
close), so the retry — and the next call — meets a healthy session.

Live links: the library mints ONE control link and ONE preview link per
session and keeps them stable until the browser closes or restarts. This
service only READS them (live_links()); there is no local cache, no host
probing, no relay restart and no unit healing here — the library owns all
of that (LiveStack self-heals, LiveLinks revokes on close).

Screenshots: acting tools return PNG bytes. They are saved under
X5Agent/Data/files/browser/ (max 20 files, oldest pruned) and the tool output
carries has_screenshot + screenshot_path for the UI expand arrow.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Any

CALL_TIMEOUT = 120
MAX_READONLY_RETRIES = 3
MAX_SCREENSHOTS = 20

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_browser: Any = None
_started: bool = False
# Lifecycle generation: incremented on every close. A start that overlaps a
# close shuts itself back down instead of reviving the browser behind the user.
_gen: int = 0
_state_lock: Any = None
# Retained live stack for the whole session: WSL idle-shuts down within
# ~a minute when nothing holds it (verified live), which tears the display,
# VNC, relay and browser down and the UI reads it as "closed by itself".
# Holding keep_alive on this object for as long as _started keeps it up.
_stack: Any = None
_keeper_on: bool = False

# Tools allowed to boot the browser. Everything else reuses a running
# session only and fails clearly while stopped — this is the hard guarantee
# behind "the browser runs only when explicitly started".
EXPLICIT_STARTERS = frozenset({"open_browser", "restart_browser"})

STOPPED_MESSAGE = "browser stopped. Start it with open_browser when the user asks."


def _lifecycle_lock() -> Any:
    """One asyncio lock serializing browser start/stop transitions.

    Created once, used only on the browser loop thread, so concurrent tool
    calls can never interleave a start with a close (the start-then-die
    race) or stack two Chromes on one profile.
    """
    global _state_lock
    if _state_lock is None:
        with _lock:
            if _state_lock is None:
                _state_lock = asyncio.Lock()
    return _state_lock


def _workspace_dir() -> Path:
    try:
        import sys as _sys
        proj = Path(__file__).resolve().parents[1]
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        from core.paths import workspace_root as _ws
        ws = _ws()
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return ws
    except Exception:
        repo = Path(__file__).resolve().parents[2]
        ws = repo / "X5Agent" / "Workspace"
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return ws


def _shots_dir() -> Path:
    try:
        import sys as _sys
        proj = Path(__file__).resolve().parents[1]
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        from core.paths import data_subdir as _ds
        target = _ds("files", "browser")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return target
    except Exception:
        repo = Path(__file__).resolve().parents[2]
        target = repo / "X5Agent" / "Data" / "files" / "browser"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return target


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is not None and _thread is not None and _thread.is_alive():
            return _loop
        loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, name="x5browser-loop", daemon=True)
        thread.start()
        # Wait until the loop is actually running (bounded).
        for _ in range(100):
            if loop.is_running():
                break
            time.sleep(0.05)
        if not loop.is_running():
            raise RuntimeError("Browser event loop failed to start")
        _loop = loop
        _thread = thread
        return loop


def _submit(coro_factory, timeout: int = CALL_TIMEOUT) -> Any:
    """Schedule a coroutine factory on the browser loop and wait bounded."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
    try:
        return future.result(timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"Browser call timed out after {timeout}s") from e


def _endpoint_alive_now(timeout: float = 2.0) -> bool:
    """One CDP endpoint probe. Pure read, never boots. Never raises."""
    try:
        import json as _json
        import urllib.request as _url
        with _url.urlopen("http://127.0.0.1:9334/json/list", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            targets = _json.loads(resp.read().decode("utf-8"))
        return isinstance(targets, list) and any(
            isinstance(t, dict) and t.get("type") == "page" for t in targets)
    except Exception:
        return False


def is_running(quick: bool = False) -> bool:
    """True while the browser answers. Pure read-only probe, never boots anything.

    quick=True: a single probe (no retry sleeps) for hot paths that run
    constantly — UI polls and per-turn context building. A relay blip may
    read as down; callers that need certainty (close verification) use the
    default retrying form (~6s worst case).
    """
    try:
        if _launcher_alive(_browser) and _started:
            return True
    except Exception:
        pass
    attempts = 1 if quick else 3
    for attempt in range(attempts):
        try:
            if _endpoint_alive_now():
                return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(2.0)
    return False


def _stack_ports_up(stack: Any) -> bool:
    """True when the CDP relay AND the viewer already answer on Windows.

    Cheap two-port probe (no WSL call, no boot). Answers the common case
    without ever touching the stack.
    """
    import socket as _socket
    try:
        ports = (int(getattr(stack, "cdp_port", 9334)),
                 int(getattr(stack, "novnc_port", 6080)))
    except Exception:
        return False
    for port in ports:
        try:
            sock = _socket.create_connection(("127.0.0.1", port), timeout=2.0)
            try:
                sock.close()
            except Exception:
                pass
        except Exception:
            return False
    return True


# Environment units = everything the viewer needs (Chrome excluded on purpose).
_ENV_UNITS = ("x5-xvfb.service", "x5-vnc.service", "x5-novnc.service",
              "x5-cdp-relay.service", "x5-clipboard.service")


def _parse_cmdline(pid: int) -> str:
    """One process command line (single line) or ''. Never raises."""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def reap_stale_viewers() -> int:
    """Kill duplicate viewer relays + orphaned headless Chromes. Returns kills.

    One browser only: the websockify relay is a singleton on 6080 — when two
    or more answer, the newest is kept and the rest are killed (a double relay
    is how one click arrives twice / a minute late). Orphaned headless
    Chromes (dead --user-data-dir under /tmp, parent gone) are killed too and
    their stale profile dirs removed, so leaked /tmp profiles never pile up.
    Scoped narrowly (cmdline match), bounded, best-effort. Never raises, and
    never touches the X5 live Chrome (its profile lives under
    /var/lib/x5live-profile, never /tmp).
    """
    kills = 0
    try:
        import os as _os
        import shutil as _shutil
        import signal as _signal
        import subprocess as _sp

        me = _os.getpid()
        # 1) Duplicate websockify relays on 6080: keep the newest, kill extras.
        try:
            proc = _sp.run(["pgrep", "-f", "websockify.*6080"],
                           capture_output=True, text=True, timeout=15)
            pids = sorted({int(t) for t in (proc.stdout or "").split()
                           if t.strip().isdigit() and int(t) != me})
        except Exception:
            pids = []
        if len(pids) > 1:
            try:
                starts = []
                for pid in pids:
                    try:
                        with open(f"/proc/{pid}/stat", "r",
                                  encoding="utf-8") as fh:
                            starts.append((int(fh.read().split()[21]), pid))
                    except Exception:
                        continue
                keep = max(starts)[1] if starts else pids[-1]
            except Exception:
                keep = pids[-1]
            for pid in pids:
                if pid == keep:
                    continue
                try:
                    _os.kill(pid, _signal.SIGKILL)
                    kills += 1
                except Exception:
                    continue
        # 2) Orphaned headless Chromes: --user-data-dir under /tmp that no
        # longer exists (leaked profile) or whose parent is gone (init-held).
        try:
            proc = _sp.run(["pgrep", "-f", "user-data-dir=/tmp/"],
                           capture_output=True, text=True, timeout=15)
            suspects = [int(t) for t in (proc.stdout or "").split()
                        if t.strip().isdigit() and int(t) != me]
        except Exception:
            suspects = []
        import re as _re
        for pid in suspects:
            try:
                cmd = _parse_cmdline(pid)
                if "user-data-dir=/tmp/" not in cmd:
                    continue
                m = _re.search(r"user-data-dir=(\S+)", cmd)
                profile = m.group(1) if m else ""
                try:
                    with open(f"/proc/{pid}/stat", "r",
                              encoding="utf-8") as fh:
                        ppid = int(fh.read().split()[3])
                except Exception:
                    ppid = -1
                orphan = (ppid in (0, 1))
                stale_profile = bool(profile) and not Path(profile).exists()
                if not (orphan or stale_profile):
                    continue
                try:
                    _os.kill(pid, _signal.SIGKILL)
                    kills += 1
                except Exception:
                    continue
                try:
                    if profile and profile.startswith("/tmp/") and len(profile) > 5:
                        _shutil.rmtree(profile, ignore_errors=True)
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass
    return kills


def _env_units_active(stack: Any) -> bool:
    """True when every viewer unit is active (one bounded status call)."""
    try:
        states = stack.status()
    except Exception:
        return False
    if not isinstance(states, dict):
        return False
    return all(states.get(unit) == "active" for unit in _ENV_UNITS)


def _stack_backend(stack: Any) -> str:
    """Live-stack backend name ('wsl', ...). Never raises, never boots."""
    try:
        return str(getattr(stack, "backend", "") or "").strip().lower()
    except Exception:
        return ""


def _wsl_chrome_up(timeout: float = 15.0) -> bool:
    """True when the WSL Chrome + CDP relay units are both active.

    Ground truth that the live browser really runs hidden inside WSL
    (a bare port probe cannot tell a relay from a squatter window).
    Windows/WSL only; bounded; never raises, never boots.
    """
    try:
        import subprocess as _sp
        try:
            from x5browser.platform import wsl_distro, quiet_flags
            distro = wsl_distro()
            flags = quiet_flags()
        except Exception:
            distro, flags = "Ubuntu", {}
        if not isinstance(flags, dict):
            flags = {}
        proc = _sp.run(["wsl", "-d", distro, "--", "systemctl", "is-active",
                        "x5-chrome.service", "x5-cdp-relay.service"],
                       capture_output=True, text=True,
                       timeout=timeout, **flags)
        lines = [l.strip() for l in (proc.stdout or "").splitlines()
                 if l.strip()]
        return len(lines) >= 2 and all(l == "active" for l in lines[:2])
    except Exception:
        return False


def _evict_squatter_chrome(cdp_port: int) -> int:
    """Kill library-profile Chromes squatting on the relay port.

    When the relay is down but 9334 answers, a headed local Chrome spawned
    by an earlier broken boot owns the port (the visible popup window):
    the relay can never bind again until it is gone, and the viewer stays
    black. Scoped narrowly — only chrome.exe processes whose command line
    carries the library profile AND the relay port are killed; anything
    under the real Google Chrome User Data is never touched. Windows only,
    bounded, best-effort. Returns kills; never raises, never boots.
    """
    kills = 0
    try:
        import os as _os
        if _os.name != "nt":
            return 0
        import json as _json
        import subprocess as _sp
        rows: list[tuple[int, str]] = []
        # PowerShell CIM (wmic is gone on current Windows): full command
        # lines as JSON, bounded. Falls back to wmic when unavailable.
        try:
            _ps = "powershell"
            _cmd = ("Get-CimInstance Win32_Process "
                    "| Where-Object {$_.Name -eq 'chrome.exe'} "
                    "| Select-Object ProcessId,CommandLine "
                    "| ConvertTo-Json -Compress")
            try:
                proc = _sp.run([_ps, "-NoProfile", "-NonInteractive",
                                "-Command", _cmd],
                               capture_output=True, text=True, timeout=45)
                out = (proc.stdout or "").strip()
            except Exception:
                proc = _sp.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                                "-NoProfile", "-NonInteractive",
                                "-Command", _cmd],
                               capture_output=True, text=True, timeout=45)
                out = (proc.stdout or "").strip()
            if out:
                try:
                    data = _json.loads(out)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    data = [data]
                if isinstance(data, list):
                    for item in data:
                        try:
                            if not isinstance(item, dict):
                                continue
                            rows.append((int(item.get("ProcessId") or 0),
                                         str(item.get("CommandLine") or "")))
                        except Exception:
                            continue
        except Exception:
            rows = []
        if not rows:
            # Legacy fallback: wmic CSV (proper quoting via the csv module).
            try:
                import csv as _csv
                import io as _io
                proc = _sp.run(["wmic", "process", "where", "name='chrome.exe'",
                                "get", "CommandLine,ProcessId", "/format:csv"],
                               capture_output=True, text=True, timeout=30)
                try:
                    for row in _csv.reader(_io.StringIO(proc.stdout or "")):
                        try:
                            if len(row) < 3 or row[0].strip().lower() == "node":
                                continue
                            rows.append((int(str(row[2]).strip()), row[1] or ""))
                        except Exception:
                            continue
                except Exception:
                    pass
            except Exception:
                pass
        me = _os.getpid()
        port_flag = f"remote-debugging-port={int(cdp_port)}"
        targets: list[int] = []
        try:
            for pid, cmd in rows:
                try:
                    if not isinstance(pid, int) or pid <= 0:
                        continue
                    if not isinstance(cmd, str) or not cmd:
                        continue
                except Exception:
                    continue
                if pid == me or pid <= 0:
                    continue
                low = cmd.lower()
                if "x5browser" not in low or port_flag not in low:
                    continue
                if "google\\chrome\\user data" in low or "google/chrome/user data" in low:
                    continue
                targets.append(pid)
        except Exception:
            return 0
        for pid in targets[:25]:
            try:
                _sp.run(["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=15)
                kills += 1
            except Exception:
                continue
    except Exception:
        pass
    return kills


def _ensure_wsl_download_bridge(stack: Any) -> None:
    """Route WSL-Chrome downloads into the watched library folder.

    Linux Chrome resolves the library's Windows download path relative to
    its working directory, creating a dead backslash-named dir at WSL root
    where finished files rot unseen (verified live: an mp3 sat there while
    Workspace/downloads stayed empty). A symlink with that exact name
    pointing at the library folder (via /mnt/<drive>) routes every future
    download straight into the watched folder; strays found inside are
    moved there first. wsl.exe expands `$` and halves `\\` in argv, so this
    uses separate calls, no `$`, doubled backslashes, validated bare names.
    wsl backend only; bounded (~8s per call); never raises, never boots.
    """
    try:
        if _stack_backend(stack) != "wsl":
            return
        try:
            from x5browser.config import LIBRARY_DOWNLOADS_DIR as _lib
            win = str(_lib)
        except Exception:
            return
        import re as _re
        m = _re.match(r"^([A-Za-z]):[\\/](.*)$", win)
        if not m:
            return
        rest = m.group(2).replace("\\", "/").strip("/")
        if not rest:
            return
        mnt = "/mnt/" + m.group(1).lower() + "/" + rest
        trap = ("/" + win).replace("\\", "\\\\")
        try:
            from x5browser.platform import wsl_distro, quiet_flags
            distro = wsl_distro()
            flags = quiet_flags()
        except Exception:
            distro, flags = "Ubuntu", {}
        if not isinstance(flags, dict):
            flags = {}
        import subprocess as _sp

        def _wsl(*args: str) -> Any:
            try:
                return _sp.run(["wsl", "-d", distro, "--", "sudo", "-n", *args],
                               capture_output=True, text=True,
                               timeout=8.0, **flags)
            except Exception:
                return None

        proc = _wsl("readlink", trap)
        if proc is not None and proc.returncode == 0 and (proc.stdout or "").strip() == mnt:
            return
        proc = _wsl("test", "-L", trap)
        if proc is not None and proc.returncode == 0:
            _wsl("rm", trap)
        else:
            proc = _wsl("test", "-d", trap)
            if proc is not None and proc.returncode == 0:
                proc = _wsl("ls", "-1", trap)
                if proc is not None and proc.returncode == 0:
                    _wsl("mkdir", "-p", mnt)
                    moved = 0
                    for line in (proc.stdout or "").splitlines():
                        if moved >= 20:
                            break
                        nm = line.strip()
                        if (nm and nm not in (".", "..") and "/" not in nm
                                and "\\" not in nm and "\x00" not in nm
                                and len(nm) <= 200):
                            ok = _wsl("mv", "--", trap + "/" + nm, mnt + "/")
                            if ok is not None and ok.returncode == 0:
                                moved += 1
                    _wsl("rmdir", trap)
        _wsl("mkdir", "-p", mnt)
        _wsl("ln", "-s", mnt, trap)
    except Exception:
        pass


def _boot_wsl_units(stack: Any, timeout: float = 90.0) -> None:
    """Boot the WSL live units directly (bounded). Raises when unusable.

    Workaround kept from the x5browser 1.5.1 era: ``LiveStack.start()`` checked
    ``Path("/etc/systemd/system")`` on the WINDOWS filesystem, so on
    Windows every unit read "missing" and start() always raised
    ``units_not_installed`` — then fell back to process mode and left
    Chrome to a local HEADED spawn (visible popup + black preview). Fixed
    upstream in 1.5.2 (units probed inside the distro), but booting the
    units directly over ``wsl sudo -n systemctl`` stays the robust path (the
    only privileged path used anywhere):
    env units ``enable --now``, Chrome ``start`` (never enabled: an
    explicit close must keep it closed). Never raises without a clear
    cause; never spawns anything local.
    """
    import subprocess as _sp
    import time as _time
    try:
        from x5browser.platform import wsl_distro, quiet_flags
        distro = wsl_distro()
        flags = quiet_flags()
    except Exception:
        distro, flags = "Ubuntu", {}
    if not isinstance(flags, dict):
        flags = {}

    def _ctl(*args: str, wait: float) -> bool:
        try:
            proc = _sp.run(["wsl", "-d", distro, "--", "sudo", "-n",
                            "systemctl", *args],
                           capture_output=True, text=True,
                           timeout=wait, **flags)
            return proc.returncode == 0
        except Exception:
            return False

    deadline = _time.monotonic() + max(30.0, timeout)
    try:
        stack.keep_alive()
    except Exception:
        pass
    _ctl("enable", "--now", *_ENV_UNITS, wait=60.0)
    _ctl("disable", "x5-chrome.service", wait=30.0)
    _ctl("start", "x5-chrome.service", wait=60.0)
    while _time.monotonic() < deadline:
        try:
            if _wsl_chrome_up(timeout=10.0):
                return
        except Exception:
            pass
        try:
            _time.sleep(2.0)
        except Exception:
            break
    raise RuntimeError("live browser failed to start (hidden browser did not come up)")


def _ensure_stack_up(stack: Any) -> None:
    """Bring the hidden live stack up (idempotent). Raises when unusable.

    The library owns the whole stack: units, install, self-heal
    (``Restart=always``) and the WSL keep-alive. This only decides whether a
    boot is even worth attempting:

    - WSL backend with a dead browser but a live 9334 -> a squatter headed
      Chrome hijacked the relay port (visible popup + black preview):
      evict it first so the relay can bind again;
    - ports already answer AND the WSL browser is up -> nothing to do;
    - ports answer but the WSL browser is down -> attaching would reuse a
      squatter window, so refuse instead of showing a black preview;
    - units active but ports dead -> the Windows<->WSL route is broken, which
      waiting cannot fix -> fail fast so the caller falls back at once
      instead of burning the whole stack timeout;
    - otherwise -> boot the units (bounded) + bounded verification that
      the WSL browser really came up (never hand a headed local fallback
      to the user behind a dead stack).
    """
    wsl = _stack_backend(stack) == "wsl"
    try:
        cdp_port = int(getattr(stack, "cdp_port", 9334))
    except Exception:
        cdp_port = 9334
    if wsl:
        try:
            if not _wsl_chrome_up() and _endpoint_alive_now(timeout=2.0):
                _evict_squatter_chrome(cdp_port)
        except Exception:
            pass
        # Keep finished downloads arriving: bridge the WSL trap dir into
        # the watched library folder before Chrome can use it (best-effort).
        try:
            _ensure_wsl_download_bridge(stack)
        except Exception:
            pass
    if _stack_ports_up(stack):
        if wsl and not _wsl_chrome_up():
            raise RuntimeError("live browser down (viewer answers, browser does not). "
                               "Close it with close_browser and open again.")
        return
    if _env_units_active(stack):
        raise RuntimeError("live stack reachable inside WSL but not from Windows")
    if wsl:
        # Direct unit boot: the library's stack.start() can never pass its
        # own install check on Windows (see _boot_wsl_units) and would fall
        # back to process mode + a local headed spawn.
        _boot_wsl_units(stack, timeout=90.0)
        return
    stack.start(90.0)


async def _boot_home_tab(browser: Any) -> None:
    """Fresh boot lands on the homepage, never about:blank. Best-effort.

    Chrome itself opens an empty tab on start. A blank tab holds no work,
    navigating it home is always safe. Real pages are never touched."""
    try:
        listing = await browser.manage_tabs(action="list")
    except Exception:
        return
    try:
        import re as _re
        active_url = None
        for line in str(listing or "").splitlines():
            if line.strip().startswith("active "):
                m = _re.search(r"url=(\S+)", line)
                if m:
                    active_url = m.group(1)
                break
        if active_url is None:
            return
        low = active_url.strip().lower()
        if low and low != "about:blank" and not low.startswith("chrome://"):
            return
        try:
            await browser.navigate(to="https://www.google.com/", wait_until="none")
        except Exception:
            pass
    except Exception:
        pass


def _launcher_alive(browser: Any) -> bool:
    """True while this browser object answers. Never raises, never boots."""
    try:
        return bool(browser is not None and browser._launcher.is_alive())
    except Exception:
        return False


def _revert_spawned(browser: Any) -> bool:
    """Kill a local Chrome born from a no-boot call. Never raises, never boots.

    Adopt/attach must only REUSE a live endpoint. When the endpoint dies under
    us, ChromeLauncher.start() spawns a local Chrome instead — that is the
    automatic restart behind an explicit close_browser. Returns True when such
    a spawn was found and quietly killed (the caller must then report stopped
    instead of handing out the revived session). Runs only for freshly adopted
    objects on no-boot paths, so an explicitly opened local browser is never
    touched here.
    """
    try:
        launcher = getattr(browser, "_launcher", None)
        if launcher is None:
            return False
        try:
            spawned = bool(launcher.owns_process())
        except Exception:
            return False
        if not spawned:
            return False
    except Exception:
        return False
    try:
        launcher.stop()
    except Exception:
        try:
            launcher.close_profile()
        except Exception:
            pass
    return True


def _hold_distro(stack: Any) -> None:
    """Retain the live stack + hold WSL open for the whole session.

    keep_alive() is idempotent (early-returns while its keeper runs), so
    calling it here and from the re-arm loop is safe. Pass None to drop a
    stale hold (local-fallback sessions have no WSL to hold).
    Never raises, never boots.
    """
    global _stack
    try:
        _stack = stack
        if stack is None:
            return
        keep = getattr(stack, "keep_alive", None)
        if callable(keep):
            try:
                keep()
            except Exception:
                pass
    except Exception:
        pass
    _ensure_keeper_loop()


def _ensure_keeper_loop() -> None:
    """One daemon thread: re-arm a dead keeper every 60s while started.

    A single keep_alive() at boot is not enough — if its keeper process
    ever dies, nothing re-arms it and WSL idle-shuts down mid-session.
    Started once per process; never raises, never boots.
    """
    global _keeper_on
    try:
        if _keeper_on:
            return
        _keeper_on = True
    except Exception:
        return

    def _run() -> None:
        while True:
            try:
                import time as _time
                _time.sleep(60.0)
            except Exception:
                break
            try:
                if _started and _stack is not None:
                    keep = getattr(_stack, "keep_alive", None)
                    if callable(keep):
                        keep()
            except Exception:
                pass

    try:
        t = threading.Thread(target=_run, name="x5browser-keeper", daemon=True)
        t.start()
    except Exception:
        try:
            _keeper_on = False
        except Exception:
            pass


def _release_hold() -> None:
    """Drop the retained stack + stop holding WSL open. Close paths only.

    An explicit close must free WSL to idle-stop again; otherwise the
    keeper would hold the distro (and its ports) forever behind a close.
    Never raises, never boots.
    """
    global _stack
    try:
        stack = _stack
        _stack = None
        if stack is not None:
            rel = getattr(stack, "release_keep_alive", None)
            if callable(rel):
                try:
                    rel()
                except Exception:
                    pass
    except Exception:
        pass


def _alive_no_boot() -> bool:
    """True while a session is usable without booting. Pure probe: no loop,
    no WSL, no stack.

    The live CDP endpoint is ground truth: a local handle may be missing
    (fresh process) while Chrome answers — that session is adoptable via a
    pure attach, still without booting anything.
    """
    try:
        return bool(_endpoint_alive_now())
    except Exception:
        return False


async def _get_browser_async(allow_boot: bool = True) -> Any:
    global _browser, _started
    lock = _lifecycle_lock()
    async with lock:
        # Publish the gated live-viewer port BEFORE the library builds this
        # session's links: the links must name the addressed viewer, not the
        # raw loopback relay (which carries no authentication at all).
        await _ensure_live_viewer()
        if _launcher_alive(_browser) and _started:
            # Reuse only while the endpoint truly answers: the relay flaps
            # (units stay active), and a blind reuse hands out a dead link.
            try:
                if await asyncio.to_thread(_endpoint_alive_now):
                    return _browser
            except Exception:
                pass
        if not allow_boot:
            # Explicit lifecycle: non-starter tools never boot (no WSL, no
            # units, no Chrome). But a live endpoint with no local handle
            # (fresh process after a server restart) is ADOPTED with a pure
            # CDP attach — the launcher reuses a live endpoint instead of
            # CDP attach — the launcher reuses a live endpoint instead of
            # spawning anything, so this still boots nothing. Tabs and
            # session survive; nothing is navigated on adoption.
            try:
                adopted = await asyncio.to_thread(_endpoint_alive_now)
            except Exception:
                adopted = False
            if not adopted:
                _browser = None
                _started = False
                raise RuntimeError(STOPPED_MESSAGE)
            my_adopt_gen = _gen
            try:
                from x5browser import BrowserConfig, LiveStack, X5Browser
            except ImportError as e:
                raise RuntimeError(
                    "X5Browser library is not installed. Run: pip install X5Browser"
                ) from e
            adopt_stack = None
            try:
                adopt_stack = LiveStack()
            except Exception:
                adopt_stack = None
            if adopt_stack is not None and _stack_backend(adopt_stack) == "wsl":
                # Never adopt a squatter headed window (visible popup + black
                # preview): the endpoint may answer while the hidden WSL
                # browser is down. Evict the squatter, then re-probe — a dead
                # endpoint reports stopped and the next open boots cleanly.
                try:
                    if not _wsl_chrome_up():
                        try:
                            _evict_squatter_chrome(int(adopt_stack.cdp_port))
                        except Exception:
                            pass
                        try:
                            still = await asyncio.to_thread(_endpoint_alive_now)
                        except Exception:
                            still = False
                        if not still:
                            _browser = None
                            _started = False
                            raise RuntimeError(STOPPED_MESSAGE)
                except RuntimeError:
                    raise
                except Exception:
                    pass
            try:
                adopt_config = BrowserConfig.default()
            except Exception:
                from x5browser import BrowserConfig as _BC  # type: ignore
                adopt_config = _BC()
            try:
                import dataclasses as _dc
                # Never a visible window: a spawn here only happens when the
                # endpoint dies under us, and it must stay invisible (the
                # revert below then reports stopped). Attaches ignore this.
                adopt_kwargs: dict = {"workspace_dir": str(_workspace_dir()),
                                      "headless": True}
                if adopt_stack is not None:
                    adopt_kwargs["debug_port"] = int(adopt_stack.cdp_port)
                adopt_config = _dc.replace(adopt_config, **adopt_kwargs)
            except Exception:
                pass
            adopted_browser = (X5Browser(config=adopt_config, stack=adopt_stack,
                                         **_live_links_kwarg())
                               if adopt_stack is not None
                               else X5Browser(config=adopt_config,
                                              **_live_links_kwarg()))
            try:
                await adopted_browser.start()
            except Exception:
                _browser = None
                _started = False
                raise RuntimeError(STOPPED_MESSAGE)
            if my_adopt_gen != _gen or not _launcher_alive(adopted_browser):
                # A close landed mid-adopt, or the endpoint died under us:
                # never hand out a dead session, never revive behind a close.
                _browser = None
                _started = False
                raise RuntimeError(STOPPED_MESSAGE)
            if _revert_spawned(adopted_browser):
                # The endpoint died under us and adopt spawned a local Chrome
                # instead of attaching: an automatic boot behind an explicit
                # close. Reverted above — report stopped, keep it stopped.
                _browser = None
                _started = False
                raise RuntimeError(STOPPED_MESSAGE)
            _browser = adopted_browser
            _started = True
            _housekeep_viewers()
            _hold_distro(adopt_stack)
            return adopted_browser
        _started = False
        my_gen = _gen
        try:
            from x5browser import BrowserConfig, LiveStack, X5Browser
        except ImportError as e:
            raise RuntimeError(
                "X5Browser library is not installed. Run: pip install X5Browser"
            ) from e
        # Live stack first: hidden WSL browser + working stream links (no local window).
        # Falls back to a HEADLESS local browser only when the stack is
        # unavailable (no WSL on Windows): no visible window ever, profile
        # stays inside the library (never the real Chrome User Data).
        stack = None
        try:
            stack = LiveStack()
            await asyncio.to_thread(_ensure_stack_up, stack)
        except Exception:
            stack = None
        try:
            config = BrowserConfig.default()
        except Exception:
            from x5browser import BrowserConfig as _BC  # type: ignore
            config = _BC()
        try:
            import dataclasses as _dc
            # Never a visible window on this machine: healthy boots attach
            # to the hidden WSL browser (this flag is then unused); any
            # local spawn — stack or fallback — stays headless. The old
            # stack-only conditional left headed spawns that popped a real
            # window in front of the user with a black preview behind it.
            replace_kwargs: dict = {"workspace_dir": str(_workspace_dir()),
                                    "headless": True}
            if stack is not None:
                replace_kwargs["debug_port"] = int(stack.cdp_port)
            # Safety: never launch with the real user profile even if a
            # caller built such a config upstream; the library profile is
            # the only allowed home.
            try:
                prof_low = str(getattr(config, "profile_dir", "")).lower()
                if ("google\\chrome" in prof_low and "user data" in prof_low) or prof_low.endswith("user data"):
                    from x5browser import BrowserConfig as _BC2  # type: ignore
                    replace_kwargs["profile_dir"] = str(_BC2.default().profile_dir)
            except Exception:
                pass
            config = _dc.replace(config, **replace_kwargs)
        except Exception:
            pass
        browser = (X5Browser(config=config, stack=stack, **_live_links_kwarg())
                   if stack is not None
                   else X5Browser(config=config, **_live_links_kwarg()))
        try:
            await browser.start()
        except Exception:
            _browser = None
            _started = False
            raise
        if my_gen != _gen:
            # A close landed mid-start: shut the just-started browser back
            # down instead of reviving it behind the user.
            try:
                await browser.close_browser()
            except Exception:
                pass
            _browser = None
            _started = False
            raise RuntimeError("Browser closed during startup")
        if not _launcher_alive(browser):
            # Start honestly: never hand out a half-born browser that dies
            # on the next call.
            _browser = None
            _started = False
            raise RuntimeError("Browser failed to start (process died immediately)")
        _browser = browser
        _started = True
        _housekeep_viewers()
        _hold_distro(stack)
        # Fresh boot only (reached only via open_browser/restart_browser).
        await _boot_home_tab(browser)
        return browser


def _is_readonly(name: str) -> bool:
    try:
        from x5browser.browser.tools import READ_ONLY_TOOLS
        return name in READ_ONLY_TOOLS
    except Exception:
        pass
    try:
        from .catalog import get_spec
        # Read-only set mirrors the library catalog (no screenshot producers).
        return name in {
            "page_snapshot", "read_text", "find_elements", "inspect_element",
            "extract_data", "get_page_info", "get_events", "read_console",
            "list_requests", "verify_state", "live_preview",
        }
    except Exception:
        return False


def _transient(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    markers = ("timeout", "connection", "reset", "refused", "unreachable",
               "temporarily", "cdp", "websocket", "closed", "eof")
    return any(m in text for m in markers)


async def _try_repair(browser: Any) -> bool:
    """Best-effort CDP re-attach after a transient drop. Never raises, never boots.

    The library's repair_session() (new in 1.5.1) only re-reads the debugger
    URL and re-attaches on the same browser (same process, profile, tabs):
    one bounded attempt, refuses when the browser was explicitly closed.
    Missing on older libraries -> False (retry path unchanged).
    """
    try:
        repair = getattr(browser, "repair_session", None)
        if repair is None or not callable(repair):
            return False
        return bool(await asyncio.wait_for(repair(), timeout=15.0))
    except Exception:
        return False


def _stop_stack_units_sync(timeout: float = 60.0) -> bool:
    """Stop the stack Chrome + CDP relay units (sync, best-effort).

    The library's stop_browser() checks for unit files on the LOCAL
    filesystem, which never exist on Windows, so it silently stops nothing —
    an explicit close left Chrome running (and systemd's Restart=always
    revived anything killed behind it). Stopping the units directly over WSL
    keeps a close closed: an explicit systemctl stop is never resurrected.
    Never raises; True when the CDP endpoint went quiet.
    """
    try:
        from x5browser import LiveStack as _LiveStack
        stack = _LiveStack()
    except Exception:
        return False
    try:
        backend = getattr(stack, "backend", "")
        if backend == "wsl":
            import subprocess as _sp
            try:
                from x5browser.platform import wsl_distro, quiet_flags
                distro = wsl_distro()
                flags = quiet_flags()
            except Exception:
                return False
            try:
                _sp.run(["wsl", "-d", distro, "--", "sudo", "-n", "systemctl",
                         "stop", "x5-chrome.service", "x5-cdp-relay.service"],
                        capture_output=True, timeout=timeout, **flags)
            except Exception:
                return False
        else:
            try:
                stack._run("sudo", "-n", "systemctl", "stop",
                           "x5-chrome.service", "x5-cdp-relay.service",
                           timeout=timeout)
            except Exception:
                return False
    except Exception:
        return False
    try:
        import time as _time
        deadline = _time.monotonic() + 15.0
        while _time.monotonic() < deadline:
            try:
                if not _endpoint_alive_now(timeout=2.0):
                    return True
            except Exception:
                return True
            try:
                _time.sleep(1.0)
            except Exception:
                break
    except Exception:
        pass
    try:
        return not bool(_endpoint_alive_now(timeout=2.0))
    except Exception:
        return True


async def _close_async() -> Any:
    """Close the browser for real and keep it closed. Never boots anything.

    Runs under the lifecycle lock: a close overlapping a start wins, and the
    overlapped start shuts itself back down (see _get_browser_async).
    """
    try:
        return await _close_browser_async()
    finally:
        # No live session is left to serve, so the gated viewer port closes
        # with the browser (the next open publishes it again). Its session
        # tokens die with the session, exactly like the library's links.
        await _stop_live_viewer()


async def _close_browser_async() -> Any:
    """The close itself (see _close_async for the contract and the lock)."""
    global _browser, _started, _gen
    lock = _lifecycle_lock()
    async with lock:
        _gen += 1
        if not _launcher_alive(_browser):
            _browser = None
            _started = False
            # Orphaned session: our handle is gone (e.g. server restarted)
            # but Chrome may still answer from the live stack. Reporting
            # "stopped" while the endpoint answers would deadlock close
            # forever, so stop the stack units directly (no boot: the
            # endpoint is alive, hence WSL is already up).
            try:
                orphaned = await asyncio.to_thread(_endpoint_alive_now)
            except Exception:
                orphaned = False
            if not orphaned:
                _release_hold()
                return "Browser already stopped."
            # Stop the units directly: the library short-circuits on Windows
            # (no local unit files) and Restart=always would revive a kill.
            await asyncio.to_thread(_stop_stack_units_sync)
            try:
                for _ in range(20):
                    try:
                        if not await asyncio.to_thread(_endpoint_alive_now):
                            break
                    except Exception:
                        break
                    await asyncio.sleep(1.0)
            except Exception:
                pass
            _release_hold()
            return "browser closed quietly, data kept."
        browser = _browser
        try:
            result = await browser.close_browser(**{})
            # Belt and suspenders: the library close ends the session, but on
            # Windows it cannot stop the stack units — do it here so systemd's
            # Restart=always can't revive Chrome behind the close.
            try:
                await asyncio.to_thread(_stop_stack_units_sync)
            except Exception:
                pass
        finally:
            _browser = None
            _started = False
            _release_hold()
        # Wait (bounded) until the endpoint is actually gone, so the next
        # probe reports stopped instead of a dying half-state.
        try:
            for _ in range(25):
                if not _launcher_alive(browser):
                    break
                await asyncio.sleep(0.2)
        except Exception:
            pass
        return result


async def _call_async(name: str, kwargs: dict) -> Any:
    global _browser, _started
    from .catalog import is_dangerous  # noqa: F401 (kept for future gating)
    if name == "close_browser":
        return await _close_async()
    fresh_adopt = False
    if name in EXPLICIT_STARTERS:
        browser = await _get_browser_async(allow_boot=True)
    else:
        # Fast fail without touching WSL/stack/Chrome: a stopped browser
        # never boots from a page/read/live tool, on any OS.
        if not _alive_no_boot():
            raise RuntimeError(STOPPED_MESSAGE)
        prev = _browser
        browser = await _get_browser_async(allow_boot=False)
        # Only a freshly adopted object can hide a spawn: a reused session was
        # already alive (explicitly opened) before this call, so it is never
        # checked here.
        fresh_adopt = browser is not prev
    method = getattr(browser, name, None)
    if method is None or not callable(method):
        raise ValueError(f"Unknown browser tool: {name}")
    attempts = MAX_READONLY_RETRIES if _is_readonly(name) else 1
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            out = await method(**kwargs)
        except (ValueError, TypeError):
            raise
        except Exception as e:
            last = e
            if not _transient(e):
                raise
            # Self-heal the dropped session: read-only retries then meet a
            # healthy handle, and acting tools (exactly once, still raised
            # below) leave a repaired session for the next call.
            try:
                await _try_repair(browser)
            except Exception:
                pass
            if attempt + 1 >= attempts:
                raise
            await asyncio.sleep(min(1.0 * (attempt + 1), 3.0))
            continue
        if fresh_adopt and _revert_spawned(browser):
            # The library auto-started a local Chrome mid-call (endpoint died
            # and _ensure_browser/launcher spawned behind us): revert to
            # stopped instead of returning a revived session.
            _browser = None
            _started = False
            _release_hold()
            raise RuntimeError(STOPPED_MESSAGE)
        return out
    if last is not None:
        raise last
    raise RuntimeError(f"Browser tool {name} failed without details")


def call_tool(name: str, kwargs: dict) -> Any:
    """Run one browser tool by name with validated kwargs. Returns raw result."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tool name must be non-empty string")
    if not isinstance(kwargs, dict):
        raise TypeError("tool kwargs must be dict")
    return _submit(lambda: _call_async(name.strip(), dict(kwargs)))


def _housekeep_viewers() -> None:
    """Best-effort viewer housekeeping after a successful boot/adopt.

    Kills stale/extra viewer websockify processes and orphaned headless
    Chrome profiles so the live viewer stays healthy (the X5 live Chrome is
    never touched). Runs once per boot/adopt in a daemon thread (never blocks
    the boot). Never raises.
    """
    def _run() -> None:
        try:
            from X5Browser.service import reap_stale_viewers
            reap_stale_viewers()
        except Exception:
            pass

    try:
        import threading as _threading

        _threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


async def _ensure_live_viewer() -> bool:
    """Publish the ONE token-gated live-viewer port (best-effort, never fatal).

    With the gate up, the library mints this session's viewer links on the
    gated port instead of the raw loopback relay, so the live view can be
    watched from another machine without exposing the unauthenticated relay.
    Any failure leaves the library's own links untouched — the browser keeps
    working exactly as before. Never raises.
    """
    try:
        from . import liveviewer

        if not await liveviewer.start_async():
            return False
        liveviewer.publish_base()
        return True
    except Exception:
        return False


async def _stop_live_viewer() -> bool:
    """Close the gated viewer port (no session left to serve). Never raises."""
    try:
        from . import liveviewer

        return await liveviewer.stop_async()
    except Exception:
        return False


def _live_links_kwarg() -> dict:
    """``{"links": <owner>}`` for the library constructor, or ``{}``.

    The library's agent API otherwise builds a default token owner whose links
    name the raw loopback relay (unauthenticated). Handing it the gate's owner
    keeps the whole session — control, preview, human_control, show_preview —
    on the addressed viewer. Empty while the gate is not really up, so a failed
    gate never leaves the browser with dead links.
    """
    try:
        from . import liveviewer

        if not liveviewer.is_started():
            return {}
        owner = liveviewer.links_owner()
        return {"links": owner} if owner is not None else {}
    except Exception:
        return {}


def _empty_links() -> dict:
    """The one shape every live_links caller gets while there is no session."""
    return {"running": False, "control": "", "preview": "", "owner": "agent"}


def live_links() -> dict:
    """The session's live links, read straight from the library. Never raises.

    Returns {"running", "control", "preview", "owner"}:
    - the two links are the library's own tokens (stable until close/restart),
      so repeated reads never revoke what the user is watching;
    - an empty pair with running True means a live endpoint exists but no
      local handle holds it yet (fresh process) — the UI adopts by minting;
    - running False means there is nothing to show.

    This service never mints, caches, repairs or forgets a link.
    """
    try:
        if not (_started and _browser is not None) or not _launcher_alive(_browser):
            # No usable handle: report a live endpoint (so the UI can adopt it
            # through a normal tool call) but never invent a link for it.
            try:
                if _endpoint_alive_now():
                    return {"running": True, "control": "", "preview": "",
                            "owner": "agent"}
            except Exception:
                pass
            return _empty_links()
        report = _browser.live_report()
        if not isinstance(report, dict):
            return _empty_links()
        control = report.get("url")
        preview = report.get("preview_url")
        mode = report.get("current_mode")
        return {
            "running": True,
            "control": control[:2000] if isinstance(control, str) else "",
            "preview": preview[:2000] if isinstance(preview, str) else "",
            "owner": "user" if mode == "control" else "agent",
        }
    except Exception:
        return _empty_links()


def status_report() -> dict:
    """Read-only browser status: running flag, owner and links. Never boots.

    Built from the endpoint probe + the handle we already own, so asking for
    status can never adopt, start or revive anything (the library's own
    ``browser_status`` is async and this call must stay synchronous).
    """
    links = live_links()
    uptime = 0.0
    try:
        if _started and _browser is not None:
            uptime = round(float(_browser.uptime_s()), 1)
    except Exception:
        uptime = 0.0
    return {"running": bool(links.get("running")),
            "uptime_s": uptime,
            "control": str(links.get("owner") or "agent"),
            "url": str(links.get("control") or ""),
            "preview_url": str(links.get("preview") or ""),
            "view_only": False}


def save_screenshot(png: bytes | None, tool: str) -> str | None:
    """Persist screenshot bytes (bounded, pruned). Returns path or None."""
    if not png or not isinstance(png, (bytes, bytearray)) or len(png) == 0:
        return None
    if len(png) > 8 * 1024 * 1024:
        return None
    safe_tool = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in str(tool))[:30] or "shot"
    target_dir = _shots_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    name = f"{safe_tool}_{time.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
    path = target_dir / name
    try:
        path.write_bytes(bytes(png))
    except Exception:
        return None
    try:
        # Never prune a screenshot still referenced by conversation history —
        # otherwise old steps show a broken image after reload.
        referenced: set[str] = set()
        try:
            import json as _json
            candidates = []
            try:
                from core.paths import data_db_file as _ddf
                candidates.append(_ddf("conversation.json"))
            except Exception:
                pass
            candidates.append(Path(__file__).resolve().parents[1] / "storage" / "database" / "conversation.json")
            for cand in candidates:
                try:
                    if not cand.exists():
                        continue
                    data = _json.loads(cand.read_text(encoding="utf-8"))
                    if not isinstance(data, list):
                        continue
                    for msg in data[-100:]:
                        if not isinstance(msg, dict):
                            continue
                        for step in msg.get("steps") or []:
                            if isinstance(step, dict) and isinstance(step.get("shot"), str):
                                referenced.add(Path(step["shot"]).name)
                    if referenced:
                        break
                except Exception:
                    continue
        except Exception:
            referenced = set()
        shots = sorted(target_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        unreferenced = [s for s in shots if s.name not in referenced]
        while len(shots) > MAX_SCREENSHOTS and unreferenced:
            victim = unreferenced.pop(0)
            try:
                victim.unlink(missing_ok=True)
            except Exception:
                break
            try:
                shots.remove(victim)
            except ValueError:
                pass
    except Exception:
        pass
    return str(path)