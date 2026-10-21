"""Stable device fingerprint for license binding.

The server stores only a hash of this value, so it is not a secret. It must
stay stable across restarts and app updates: a fresh value would look like a
new device and burn the one-active-device slot.

Inputs (joined, then SHA-256):
- the OS machine id (Windows MachineGuid / Linux machine-id / macOS UUID)
- the primary MAC address (uuid.getnode)
- platform + machine + hostname

Nothing here is ever logged raw.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import uuid
from pathlib import Path

_cached: str | None = None
_details: dict[str, str] | None = None
_app_identity: dict[str, str] | None = None

# Only the shipped code is hashed: prompts, data, models, tests and caches are
# either user-owned or not part of the app, so editing them is not tampering.
_CODE_ROOTS = ("core", "plugins", "X5Browser", "gateway", "web", "main.py", "install.py")
_MAX_FILES = 4000


def _windows_machine_guid() -> str:
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip()
    except Exception:
        return ""


def _read_first_line(path: str, limit: int = 256) -> str:
    try:
        file = Path(path)
        if not file.is_file():
            return ""
        with file.open("r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit).strip()
    except Exception:
        return ""


def _mac_platform_uuid() -> str:
    try:
        import subprocess

        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in (result.stdout or "").splitlines():
            if "IOPlatformUUID" in line:
                return line.split("=", 1)[-1].strip().strip('"')
    except Exception:
        return ""
    return ""


def _hardware_id() -> str:
    system = platform.system()
    if system == "Windows":
        return _windows_machine_guid()
    if system == "Linux":
        return _read_first_line("/etc/machine-id") or _read_first_line(
            "/var/lib/dbus/machine-id"
        )
    if system == "Darwin":
        return _mac_platform_uuid()
    return ""


def _collect() -> tuple[str, dict[str, str]]:
    """The exact marks hashed into the fingerprint (collected once)."""
    global _cached, _details
    if _cached:
        return _cached, dict(_details or {})
    try:
        node = f"{uuid.getnode():012x}"
    except Exception:
        node = ""
    system = platform.system()
    machine = platform.machine()
    hostname = platform.node()
    hardware = _hardware_id()
    raw = "|".join(
        ("x5agent-device-v1", system, machine, hardware, node, hostname)
    )
    _details = {
        "machine_id": hardware[:128],
        "node_id": node[:64],
        "hostname": hostname[:128],
        "system": system[:32],
        "machine": machine[:64],
        "platform": platform.platform()[:128],
        "python": platform.python_version()[:32],
    }
    _cached = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()
    return _cached, dict(_details)


def machine_fingerprint() -> str:
    """64 lowercase hex chars (SHA-256), stable for this machine."""
    return _collect()[0]


def _project_root() -> Path:
    # project/core/licensing/device.py -> project/
    return Path(__file__).resolve().parents[2]


def _git_state(root: Path) -> str:
    """clean|modified|unknown from git, without ever failing the caller."""
    if not (root / ".git").exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return "modified" if (result.stdout or "").strip() else "clean"


def app_identity() -> dict[str, str]:
    """The watermark: build version, source hash, clean/modified state.

    Hashed once per process over the shipped code only, so a heartbeat never
    pays for it twice. Every failure degrades to empty/unknown values: this is
    reporting, never a reason to block a paying user.
    """
    global _app_identity
    if _app_identity is not None:
        return dict(_app_identity)
    version = ""
    digest = hashlib.sha256()
    try:
        root = _project_root()
        try:
            payload = json.loads(
                (root.parent / "updates.json").read_text(encoding="utf-8")
            )
            version = str(payload.get("version") or "")[:24]
        except (OSError, ValueError, TypeError):
            version = ""
        files = 0
        for entry in _CODE_ROOTS:
            target = root / entry
            if target.is_file():
                candidates = [target]
            elif target.is_dir():
                candidates = sorted(target.rglob("*.py"))
            else:
                continue
            for path in candidates:
                if files >= _MAX_FILES:
                    break
                if "__pycache__" in path.parts or "tests" in path.parts:
                    continue
                try:
                    blob = path.read_bytes()
                except OSError:
                    continue
                digest.update(str(path.relative_to(root)).encode("utf-8", "ignore"))
                digest.update(hashlib.sha256(blob).digest())
                files += 1
        state = _git_state(root)
        _app_identity = {
            "app_version": version,
            "app_hash": digest.hexdigest()[:16] if files else "",
            "app_state": state,
        }
    except Exception:
        _app_identity = {
            "app_version": version,
            "app_hash": "",
            "app_state": "unknown",
        }
    return dict(_app_identity)


def fingerprint_details() -> dict[str, str]:
    """Non-secret device marks sent with signup/login/heartbeat.

    Same inputs as the fingerprint hash, kept readable for support (which
    machine is bound), plus the app watermark (version/hash/clean-or-modified).
    Bounded and never a credential.
    """
    details = dict(_collect()[1])
    details.update(app_identity())
    return details


def fingerprint_short() -> str:
    return machine_fingerprint()[:8]


def reset_cache() -> None:
    """Tests only: forget the memoized values."""
    global _cached, _details, _app_identity
    _cached = None
    _details = None
    _app_identity = None
