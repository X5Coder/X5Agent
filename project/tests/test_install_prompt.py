"""Installer safety: ordered output, a clean owner prompt, honest dep checks.

Locks the flow the user sees: every heavy step (dependencies, embedding
download, browser check, firewall) finishes first, then the owner email +
password prompt appears on a clean screen. Block-buffered stdout used to
flush AFTER the prompt, so the download output looked like it swallowed the
prompt and the email question "disappeared".

Also locks the dependency check: it must run in a FRESH interpreter. Checking
in-process after pip replaced numpy/scipy reported a healthy install as
"still missing: sentence_transformers" and aborted the install.
"""
from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PY = PROJECT_ROOT / "install.py"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import install  # noqa: E402  (path set above)


class _FakeTerminal(io.StringIO):
    """StringIO that can pretend to be (or not be) a terminal."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # type: ignore[override]
        return self._tty


def _run_with_stdout(fake: io.StringIO, fn) -> None:
    original = sys.stdout
    sys.stdout = fake  # type: ignore[assignment]
    try:
        fn()
    finally:
        sys.stdout = original


# --- ordered output -------------------------------------------------------

def test_enable_ordered_output_turns_on_line_buffering():
    stream = io.TextIOWrapper(io.BytesIO(), line_buffering=False)
    original = sys.stdout
    sys.stdout = stream  # type: ignore[assignment]
    try:
        install.enable_ordered_output()
        assert stream.line_buffering is True
    finally:
        sys.stdout = original


def test_helpers_print_with_flush():
    """Step/log helpers must not sit in a buffer while a prompt renders."""
    source = INSTALL_PY.read_text(encoding="utf-8")
    for helper in ("def _step(", "def _ok(", "def _warn(", "def _fail("):
        body = source[source.index(helper):]
        body = body[:body.index("\n\n\n", 0) if "\n\n\n" in body else len(body)]
        assert "flush=True" in body, f"{helper} must flush"


def test_enable_ordered_output_writes_through():
    """\\r progress frames carry no newline: they must flush when written.

    Line buffering alone left the last download frames pending until the next
    read, so they were drawn on top of the email prompt.
    """
    stream = io.TextIOWrapper(io.BytesIO(), line_buffering=False, write_through=False)
    original = sys.stdout
    sys.stdout = stream  # type: ignore[assignment]
    try:
        install.enable_ordered_output()
        assert stream.write_through is True
    finally:
        sys.stdout = original


class _RecordingTerminal(_FakeTerminal):
    """Terminal that records the order of writes and flushes."""

    def __init__(self, tty: bool, events: list[str]) -> None:
        super().__init__(tty)
        self.events = events

    def write(self, text):  # type: ignore[override]
        self.events.append("write:" + text)
        return super().write(text)

    def flush(self) -> None:  # type: ignore[override]
        self.events.append("flush")
        super().flush()


def test_clear_screen_flushes_pending_output_before_clearing():
    events: list[str] = []
    out = _RecordingTerminal(True, events)
    err = _RecordingTerminal(True, events)
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err  # type: ignore[assignment]
    try:
        install._clear_screen()
    finally:
        sys.stdout, sys.stderr = original_out, original_err
    assert "flush" in events, "already-written output must be pushed out"
    assert events.index("flush") < events.index("write:\033[H\033[2J\033[3J"), (
        "flush must happen before the screen is wiped, never after")


# --- clean prompt screen --------------------------------------------------

def test_clear_screen_wipes_terminal_and_scrollback():
    fake = _FakeTerminal(tty=True)
    _run_with_stdout(fake, install._clear_screen)
    out = fake.getvalue()
    assert "\033[2J" in out, "visible screen must be cleared"
    assert "\033[3J" in out, "scrollback must be cleared (long \\r progress bars)"


def test_clear_screen_safe_without_a_terminal():
    fake = _FakeTerminal(tty=False)
    _run_with_stdout(fake, install._clear_screen)
    out = fake.getvalue()
    assert "\033" not in out, "no escape codes when stdout is not a terminal"
    assert out.strip() == "", "fallback is blank lines only"


# --- flow order -----------------------------------------------------------

def test_owner_prompt_is_the_last_thing_before_launch():
    """Download/resources/firewall finish first; the console starts after."""
    source = INSTALL_PY.read_text(encoding="utf-8")
    main_body = source[source.index("def main("):]
    resources = main_body.index("ensure_resources(")
    firewall = main_body.index("ensure_firewall(")
    prompt = main_body.index("ask_credentials(")
    launch = main_body.index("launch_detached")
    assert resources < prompt, "embedding download must finish before the prompt"
    assert firewall < prompt, "firewall step must finish before the prompt"
    assert prompt < launch, "the console starts only after the owner is set"


def test_prompt_line_survives_a_pipe_and_shows_the_step():
    """ask_credentials clears the screen, prints 6/6, then waits on stdin."""
    source = INSTALL_PY.read_text(encoding="utf-8")
    body = source[source.index("def ask_credentials("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_clear_screen()" in body
    assert '"6/6 console owner"' in body
    assert body.index("_clear_screen()") < body.index('input("  email: ")')
    assert body.index('input("  email: ")') < body.index("_read_password()")


# --- dependency check runs in a fresh interpreter -------------------------

def test_modules_missing_reports_only_broken_names(tmp_path, monkeypatch):
    (tmp_path / "x5_probe_good.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "x5_probe_bad.py").write_text(
        "raise RuntimeError('broken')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert install._modules_missing(("x5_probe_good",)) == []
    assert install._modules_missing(("x5_probe_bad",)) == ["x5_probe_bad"]
    assert install._modules_missing(("x5_probe_good", "x5_probe_bad")) == ["x5_probe_bad"]
    assert install._modules_missing(("x5_probe_missing",)) == ["x5_probe_missing"]


def test_modules_missing_sees_the_file_pip_just_replaced(tmp_path, monkeypatch):
    """The regression: numpy imported fine, pip replaced it, check must see disk."""
    probe = tmp_path / "x5_probe_swap.py"
    probe.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert install._modules_missing(("x5_probe_swap",)) == []
    probe.write_text("raise RuntimeError('replaced by pip')\n", encoding="utf-8")
    assert install._modules_missing(("x5_probe_swap",)) == ["x5_probe_swap"]


def test_required_imports_are_the_dependency_guard():
    assert install.REQUIRED_IMPORTS == ("sentence_transformers", "numpy", "chromadb")
    source = INSTALL_PY.read_text(encoding="utf-8")
    assert source.count("_modules_missing(REQUIRED_IMPORTS)") == 2, (
        "both the before and after checks must use a fresh interpreter")


# --- declared versions are part of the dependency guard -------------------

def test_requirement_parsing_handles_pins_ranges_and_extras():
    assert install._requirement_name("x5browser==1.6.2") == "x5browser"
    assert install._requirement_name("curl-cffi>=0.5") == "curl-cffi"
    assert install._requirement_name("uvicorn[standard]>=0.18") == "uvicorn"
    assert install._pinned_version("x5browser==1.6.2") == "1.6.2"
    assert install._pinned_version("chromadb>=1.0,<2.0") == ""


def test_deps_out_of_sync_sees_a_pin_bump_and_a_missing_dep(tmp_path, monkeypatch):
    """The regression: a bumped pin (or a new dep) used to be ignored by updates."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x5agent\"\ndependencies = [\n"
        '    "pytest==8.0.0",\n'
        '    "x5-not-installed-anywhere==1.0",\n'
        '    "chromadb>=1.0,<2.0",\n'
        "]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    problems = install._deps_out_of_sync()
    assert any(p.startswith("pytest ") and p.endswith("-> 8.0.0") for p in problems)
    assert "x5-not-installed-anywhere (missing)" in problems
    assert not any(p.startswith("chromadb ") and "->" in p for p in problems), (
        "ranges are presence-checked only, never version-compared")


def test_deps_out_of_sync_is_quiet_when_declared_versions_match(tmp_path, monkeypatch):
    import importlib.metadata as metadata

    installed = metadata.version("pytest")
    (tmp_path / "pyproject.toml").write_text(
        f"[project]\nname = \"x5agent\"\ndependencies = [\"pytest=={installed}\"]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    assert install._deps_out_of_sync() == []


def test_real_pyproject_declares_the_pinned_browser_layer():
    requirements = install._declared_requirements()
    assert requirements, "dependencies array must be readable"
    assert any(r.startswith("x5browser==") for r in requirements)
    assert any(r.startswith("chromadb") for r in requirements)


def test_stale_project_metadata_is_detected(tmp_path, monkeypatch):
    """A pull that bumps a pin leaves the editable dist asking for the old one."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x5agent\"\ndependencies = [\n"
        '    "x5_probe_pkg==2.0",\n'
        '    "chromadb>=1.0,<2.0",\n'
        "]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(install.importlib.metadata, "requires",
                        lambda name: ["x5_probe_pkg==1.0", "chromadb>=1.0,<2.0"])
    stale = install._stale_dist_pins()
    assert stale == ["x5-probe-pkg metadata x5_probe_pkg==1.0 -> 2.0"]
    # A matching recorded pin is not stale.
    monkeypatch.setattr(install.importlib.metadata, "requires",
                        lambda name: ["x5_probe_pkg==2.0"])
    assert install._stale_dist_pins() == []


# --- installer must run before dependencies exist -------------------------

def test_installer_imports_stdlib_only():
    """Top-level imports must be stdlib: the installer runs before deps exist.

    Project modules (core/scripts/web) and optional heavy deps (torch,
    x5browser) are imported lazily inside functions on purpose, so only the
    module scope is checked.
    """
    stdlib = getattr(sys, "stdlib_module_names", None)
    if not stdlib:
        pytest.skip("stdlib_module_names needs python 3.10+")
    tree = ast.parse(INSTALL_PY.read_text(encoding="utf-8"))
    roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    external = sorted(roots - stdlib - {"__future__"})
    assert external == [], f"top-level imports must be stdlib: {external}"


# --- the model fetch is isolated from the console -------------------------

class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _capture_fetch_run(monkeypatch, *, returncode=0, log_text=b"", timeout=None):
    """Replace subprocess.run, record the call, optionally fail/timeout."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        fileobj = kwargs.get("stdout")
        if log_text and hasattr(fileobj, "write"):
            fileobj.write(log_text)
            fileobj.flush()
        if timeout is not None:
            raise subprocess.TimeoutExpired(argv, timeout)
        return _FakeProc(returncode)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    return calls


def test_fetch_runs_in_a_child_with_its_output_in_a_log_file(monkeypatch):
    calls = _capture_fetch_run(monkeypatch)
    ok, detail = install._fetch_embedding_model()
    assert ok is True and detail == ""
    assert len(calls) == 1
    call = calls[0]
    assert call["argv"] == [
        sys.executable, "-c", install._WORKER_FETCH, str(install.PROJECT_ROOT)]
    assert call["stdin"] == subprocess.DEVNULL
    assert call["stderr"] == subprocess.STDOUT
    assert call["cwd"] == str(install.PROJECT_ROOT)
    assert call["env"]["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    log_file = call["stdout"]
    assert str(log_file.name).endswith(".log"), (
        "child output must go to a file, never straight to the console")
    assert not Path(str(log_file.name)).exists(), "the log is deleted after the call"


def test_fetch_returns_the_log_tail_when_the_child_fails(monkeypatch):
    _capture_fetch_run(monkeypatch, returncode=3, log_text=b"[WARN] attempt 1/3: 503\n")
    ok, detail = install._fetch_embedding_model()
    assert ok is False
    assert "503" in detail, "the failure reason must reach the installer output"


def test_fetch_reports_a_timeout_clearly(monkeypatch):
    _capture_fetch_run(monkeypatch, timeout=1)
    ok, detail = install._fetch_embedding_model()
    assert ok is False
    assert "timed out" in detail


def test_fetch_worker_is_valid_python_and_never_in_process():
    ast.parse(install._WORKER_FETCH)
    source = INSTALL_PY.read_text(encoding="utf-8")
    body = source[source.index("def ensure_resources("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_fetch_embedding_model(" in body, "resources step must use the isolated fetch"
    assert "ensure_embedding_model(sync=True)" not in body, (
        "in-process download draws \\r bars that overwrite the owner prompt")
    assert "import ensure_embedding_model" not in body


# --- completion screen: the logo, then the two links ----------------------

LOCAL_URL = "http://127.0.0.1:7777/"
PUBLIC_URL = "http://13.140.164.29:7777/"
_CONTROL_RE = re.compile(r"\033\]8;;[^\033]*\033\\|\033\[[0-9;]*m")


def _completion_text(tty: bool) -> str:
    return _CONTROL_RE.sub("", _completion_output(tty))


def _completion_output(tty: bool) -> str:
    fake = _FakeTerminal(tty=tty)
    original = sys.stdout
    sys.stdout = fake  # type: ignore[assignment]
    try:
        install.print_completion(LOCAL_URL, PUBLIC_URL)
    finally:
        sys.stdout = original
    return fake.getvalue()


def test_completion_shows_the_logo_then_both_links(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = _completion_output(tty=True)
    assert "\033[38;2;" in out, "a terminal gets colour"
    plain = _completion_text(tty=True)
    assert install._LOGO_ART[0] in plain, "the X5 logo must be printed"
    assert "A G E N T" in plain, "the wordmark must be printed"
    lines = plain.splitlines()
    public = next(i for i, line in enumerate(lines) if PUBLIC_URL in line)
    local = next(i for i, line in enumerate(lines) if LOCAL_URL in line)
    assert public < local, "public link first, local link second"
    assert lines[public].strip().startswith("Public"), "the public line is labelled"
    assert lines[local].strip().startswith("Local"), "the local line is labelled"
    assert plain.count("http://") == 2, "only the two links belong under the logo"


def test_logo_is_white_and_carries_no_tagline(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    white = "\033[38;2;255;255;255m"
    assert white in _completion_output(tty=True), "the X5 art must be white"
    plain = _completion_text(tty=True)
    assert "terminal-native" not in plain
    assert "autonomous agent" not in plain
    assert "─────" not in plain and "─────" not in plain, "no divider line"


def test_completion_links_are_ctrl_clickable_on_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = _completion_output(tty=True)
    assert f"\033]8;;{PUBLIC_URL}\033\\" in out, "public link must be a terminal hyperlink"
    assert f"\033]8;;{LOCAL_URL}\033\\" in out, "local link must be a terminal hyperlink"
    assert out.count("\033]8;;") == 4, "two links = two open + two close sequences"


def test_completion_is_plain_when_not_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    out = _completion_output(tty=False)
    assert "\033" not in out, "no escape codes when piped (curl|bash logs)"
    assert install._LOGO_ART[0] in out and PUBLIC_URL in out and LOCAL_URL in out


def test_completion_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\033" not in _completion_output(tty=True)


def test_completion_falls_back_to_plain_text_when_art_cannot_be_encoded():
    assert install._can_encode(install._LOGO_ART[0])
    assert install.logo_banner().startswith("  ")


def test_main_always_prints_the_completion_screen():
    source = INSTALL_PY.read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    assert body.count("print_completion(") == 1
    assert body.index("print_completion(") < body.index("if args.run:")
    assert "Web Server (Local)" not in body, "the old boxed report must be gone"
    assert "starting console now" not in body, "the end screen stays logo + links only"
    assert "console running now" not in body, "the end screen stays logo + links only"


def test_port_restart_is_one_short_line():
    source = INSTALL_PY.read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    assert "restarting console on port" in body
    assert "port busy - freeing it" not in body, "one concise line, no progress dump"
    assert "stopper(CONSOLE_PORT, quiet=True)" in body, (
        "the per-pid progress comes from cli - silence it and print one line")


def test_installer_delegates_credential_rules():
    source = INSTALL_PY.read_text(encoding="utf-8")
    assert "email_problem" in source and "password_problem" in source
    assert "EMAIL_RE" not in source, "one owner for the owner-credential rules"
    assert "English" in source, "the prompt states the rules"


def test_stop_server_quiet_flag_contract(monkeypatch):
    import inspect

    from scripts import cli

    params = inspect.signature(cli._stop_server).parameters
    assert params["quiet"].default is False, "x5agent --off keeps its progress"
    assert "quiet" in inspect.signature(cli._stop_server_windows).parameters
    printed: list = []
    monkeypatch.setattr(cli, "_port_answers", lambda port: False)  # port already free
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(a))
    assert cli._stop_server(7777, quiet=True) is True
    assert printed == []
