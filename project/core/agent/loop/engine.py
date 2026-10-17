"""
Agent Loop Engine - professional loop up to 500 steps, never fails silently.
Each iteration uses full context: system prompt + memories (if non-empty) + last 30 messages + env + todos + core tools.
System prompt separate (OpenAI system role), rest as user content (example, not specialized sending, just engine).
After each tool call, executes tool and loops with updated context + tool output.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.agent.context.builder import build_context, openai_tool_from_spec
from core.agent.loop.dispatcher import execute_tool

STORAGE_DB = Path(__file__).resolve().parents[3] / "storage" / "database"
MAX_STEPS = 500
MAX_DYNAMIC_TOOLS = 20

# Studio-session tools: the 4 base tools exist inside every PDF Studio
# branch session (pre-seeded there directly). The main agent never sees the
# session-only ones (studio_page/studio_read): tool_search never returns
# them and the main loop rejects them below. http_request is a normal
# discoverable tool that is additionally allowed inside studio sessions
# (fonts/links only — photos come from image_search, also discoverable).
# vision_analyze joins them ONLY when a vision model is configured (never
# advertised without a model behind it).
STUDIO_ONLY_TOOLS = frozenset({"studio_page", "studio_read"})
STUDIO_SESSION_TOOLS = frozenset({"studio_page", "studio_read", "http_request", "image_search"})
STUDIO_VISION_TOOLS = frozenset({"vision_analyze"})


def _studio_tool_specs() -> list[tuple[str, str, dict]]:
    """Local studio tool specs (trusted spec.json files). Never raises.

    image_search is pre-seeded with its studio face (studio_spec(): the
    studio_id home, zero Workspace words) so the studio context never
    mentions Workspace files — the studio stays self-contained.
    """
    out: list[tuple[str, str, dict]] = []
    try:
        wanted = set(STUDIO_SESSION_TOOLS)
        try:
            from core.agent.context.builder import _studio_vision_available
            if _studio_vision_available():
                wanted |= set(STUDIO_VISION_TOOLS)
        except Exception:
            pass
        base = Path(__file__).resolve().parents[3] / "plugins" / "core_plugins"
        for tname in sorted(wanted):
            try:
                if tname == "image_search":
                    from plugins.core_plugins.image_search.tool import studio_spec
                    spec = studio_spec()
                else:
                    spec = json.loads((base / tname / "spec.json").read_text(encoding="utf-8"))
                if not isinstance(spec.get("name"), str) or not isinstance(spec.get("description"), str):
                    continue
                out.append((spec["name"], spec["description"], spec.get("inputs") or {}))
            except Exception:
                continue
    except Exception:
        pass
    return out


def is_studio_call_allowed(name: object, studio_session: bool = False) -> bool:
    """True when a tool call may run in this loop kind. Never raises."""
    try:
        clean = str(name or "").strip()
        if studio_session:
            # Studio branch session: the 4 base tools, plus vision_analyze
            # only while a vision model is configured behind it.
            if clean in STUDIO_SESSION_TOOLS:
                return True
            if clean in STUDIO_VISION_TOOLS:
                try:
                    from core.agent.context.builder import _studio_vision_available
                    return bool(_studio_vision_available())
                except Exception:
                    return False
            return False
        if clean in STUDIO_ONLY_TOOLS:
            return False
    except Exception:
        pass
    return True


def _sanitize_tool_args(name: object, args: Any, studio_session: bool = False) -> Any:
    """Drop session-forged fields before execute. Never raises.

    image_search accepts studio_id ONLY inside studio sessions (pictures
    land in that studio's assets there). In the main chat the field is
    stripped, so the main agent can never write studio assets even if the
    model hallucinates a studio_id.
    """
    try:
        if (name == "image_search" and not studio_session
                and isinstance(args, dict) and "studio_id" in args):
            args = {k: v for k, v in args.items() if k != "studio_id"}
    except Exception:
        pass
    return args


def _ask_user(name: str, args: Any, on_question) -> dict:
    """Route a question tool call through the UI callback (pauses the loop until answered).

    Invalid questions are REJECTED here (never pause the UI): the error goes
    back to the model so it regenerates a valid question instead of parking
    the turn on a blank dialog. Reuses the question tool validators.
    """
    if on_question is None:
        return execute_tool(name, args)
    if not isinstance(args, dict):
        return {"tool": name, "arguments": args, "success": False,
                "error": "Invalid question call: arguments must be an object with a "
                         "'question' string. Call the question tool again with a clear "
                         "one-sentence question."}
    try:
        from core.tools.question.tool import _validate_question, _validate_options
        q = _validate_question(args.get("question"))
        opts = _validate_options(args.get("options")) if "options" in args else None
    except (TypeError, ValueError) as e:
        return {"tool": name, "arguments": args, "success": False,
                "error": f"Invalid question: {e}. Call the question tool again with a "
                         "valid one-sentence question (omit options for free-text answers)."}
    try:
        answer = on_question("ask", {
            "question": q,
            "options": list(opts) if isinstance(opts, list) else [],
        })
    except Exception:
        answer = None
    if not isinstance(answer, dict) or not str(answer.get("answer") or "").strip():
        return {"tool": name, "arguments": args, "output": {"event": "Unanswered", "answer": None}, "success": True}
    return {"tool": name, "arguments": args, "output": {"event": "Answered", "answer": str(answer["answer"])[:2000]}, "success": True}


def _approve_terminal(name: str, args: Any, on_question) -> dict:
    """No-question passthrough: BOTH scopes run terminal commands immediately.

    Scope (not approval) controls reach: limited_access = Workspace jail,
    full_access = whole machine. The scope file is set from the Agent
    permission menu; the loop never asks per command.
    """
    return execute_tool(name, args)


def _approve_browser_tool(name: str, args: Any, on_question) -> dict:
    """Restricted mode: ask the user before running a dangerous browser tool."""
    if on_question is None:
        return execute_tool(name, args)
    try:
        from X5Browser.events import format_event as _browser_event
        try:
            label = _browser_event(name, **(args if isinstance(args, dict) else {}))
        except Exception:
            label = name
        decision = on_question("approval", {"command": f"[{name}] {label}"[:500]})
    except Exception:
        decision = None
    if isinstance(decision, dict) and decision.get("decision") == "continue":
        return execute_tool(name, args)
    return {"tool": name, "arguments": args, "output": {"event": "Stopped by user", "stopped": True}, "success": True}


def _is_dangerous_browser_tool(name: str) -> bool:
    try:
        from X5Browser.catalog import is_dangerous
        return bool(is_dangerous(name))
    except Exception:
        return False


def _resolve_missing_model(name: str, args: Any, kind: str, on_question, cancel_flag=None) -> dict:
    """Pause for the user's Connect/Skip choice, then resume the loop.

    - Skip (or expiry / no-UI / STOP): continue WITHOUT the tool — the
      English skip sentence goes to the MODEL, which continues the task.
    - Connect: the UI keeps the strip up while the user connects that
      kind's model dialog; once saved it answers back and the SAME tool
      runs once (a single retry, never a loop). Still missing at retry
      time → a skip like above.
    A missing model pauses for a decision but never stalls the loop.
    Never raises.
    """
    decision = None
    if on_question is not None:
        try:
            answer = on_question("needs_model", {"kind": kind, "tool": name})
            if isinstance(answer, dict):
                decision = str(answer.get("decision") or "").strip().lower()
        except Exception:
            decision = None
    if decision in ("connect", "activate", "retry", "continue"):
        try:
            if _is_cancelled(cancel_flag):
                return _model_skipped_result(name, args, kind)
            from core.ai_models.store import has_model as _has_model
            if _has_model(kind):
                try:
                    retried = execute_tool(name, args, cancel_flag=cancel_flag)
                    if isinstance(retried, dict):
                        return retried
                except Exception:
                    pass
        except Exception:
            pass
    return _model_skipped_result(name, args, kind)


def _model_skipped_result(name: str, args: Any, kind: str) -> dict:
    """A tool gated on an unconnected model kind becomes a skipped success.

    The loop NEVER pauses or stops for this: the sentence goes to the MODEL,
    which decides — continue the task without this tool, or present the
    message to the user. The turn's final reply carries needs_model_kind, so
    the UI renders its /ai-models link as a Connect button opening the right
    model dialog.     Never raises.
    """
    return {"tool": name, "arguments": args,
            "output": {"event": f"Skipped {name} (no model)", "skipped": True,
                       "needs_model_kind": kind,
                       "message": f"Tool {name} was skipped: no suitable model connected ({kind}). "
                                  "Continue the task without this tool when possible; "
                                  "otherwise show this message to the user with the /ai-models link."},
            "success": True}


def _missing_text(kind: str, name: str) -> str:
    try:
        from core.ai_models.store import missing_message as _missing
        return str(_missing(kind))
    except Exception:
        return f"No {kind} model connected (tool {name})."


def _missing_kind_of(result: dict) -> str | None:
    """Extract the model kind from a failed tool result, else None."""
    try:
        if not isinstance(result, dict) or result.get("success"):
            return None
        direct = result.get("needs_model_kind")
        if isinstance(direct, str) and direct in ("text", "vision", "video", "image", "tts", "stt"):
            return direct
        from core.ai_models.store import kind_from_missing_error as _kind_of
        return _kind_of(str(result.get("error") or ""))
    except Exception:
        return None


def _is_cancelled(cancel_flag) -> bool:
    """Whether the run was cancelled (STOP / fresh answer). Never raises."""
    try:
        return bool(cancel_flag is not None and cancel_flag.is_set())
    except Exception:
        return False


# Closing note persisted when the user stops a task mid-run: the stopped
# task stays in context (user request + steps done so far) with this last
# line so the next turn knows it was stopped and never continues it unasked.
_STOPPED_NOTE = ("The user stopped this task before it finished (STOP). "
                "Do not continue it unless the user asks again.")


def _preview_event(name: str, args: Any) -> str:
    """Best-effort event label BEFORE execution (for instant UI). Never raises."""
    try:
        safe = args if isinstance(args, dict) else {}
        if name == "tool_search":
            return "Finding tools"
        # Browser tools (project/X5Browser): plain-English label with real args.
        try:
            from X5Browser.catalog import ALL_TOOLS as _BROWSER_ALL
            from X5Browser.events import format_event as _browser_format
            if name in _BROWSER_ALL:
                try:
                    label = _browser_format(name, **safe) if isinstance(safe, dict) else _browser_format(name)
                except Exception:
                    try:
                        label = _browser_format(name)
                    except Exception:
                        return name
                return str(label)[:80] if str(label).strip() else name
        except Exception:
            pass
        # Try the tool's own events.py formatter with the real args.
        import sys as _sys
        from pathlib import Path as _P
        proj = _P(__file__).resolve().parents[3]
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        import importlib as _il
        from core.agent.loop.dispatcher import _CORE_MAP, _DISCOVERABLE_MAP as _DMAP
        dotted = {**_CORE_MAP, **_DMAP}.get(name)
        if not dotted:
            return name
        parts = dotted.split(".")
        events_mod = ".".join(parts[:-1] + ["events"])
        try:
            mod = _il.import_module(events_mod)
        except Exception:
            return name
        fmt = getattr(mod, "format_event", None)
        if not callable(fmt):
            return name
        try:
            label = fmt(**safe) if isinstance(safe, dict) else fmt()
        except Exception:
            try:
                label = fmt()
            except Exception:
                return name
        return str(label)[:80] if str(label).strip() else name
    except Exception:
        return name


_REASON_KEYS = frozenset({"reason", "_reason", "why", "purpose", "intent"})


def _extract_model_reason(args: Any) -> tuple[Any, str]:
    """Split a model-supplied reason node off the tool arguments.

    The model may attach `reason`/`why`/`purpose` explaining why it calls
    this tool. Tool implementations never accept it, so it is stripped
    before execution and returned separately for the UI. Never raises.
    Returns (clean_args, model_reason).
    """
    try:
        if not isinstance(args, dict):
            return args, ""
        found = ""
        for key in _REASON_KEYS:
            val = args.get(key)
            if isinstance(val, str) and val.strip() and not found:
                found = val.strip()[:500]
        if not found:
            return args, ""
        clean = {k: v for k, v in args.items() if k not in _REASON_KEYS}
        return clean, found
    except Exception:
        return args, ""


def _tool_reason(name: str, args: Any, model_reason: str = "") -> str:
    """Reason node for a tool call — strictly the model's own words.

    The `reason` parameter is optional: when the model supplies it, it is
    returned (bounded) for the UI arrow; when it does not, "" is returned
    and the UI shows the step with no arrow (unless output text or an
    image arrives). Nothing is ever synthesized. Never raises.
    `name`/`args` are kept for signature compatibility only.
    """
    try:
        if isinstance(model_reason, str) and model_reason.strip():
            return model_reason.strip()[:200]
        return ""
    except Exception:
        return ""


def _should_show_arrow(name: str, result: dict | None = None) -> bool:
    """Whether one tool call may show an expand arrow. Images always show."""
    try:
        if isinstance(result, dict):
            out = result.get("output") if isinstance(result.get("output"), dict) else None
            if isinstance(out, dict) and out.get("has_screenshot"):
                return True
            if isinstance(out, dict):
                for _k in ("image_path", "image"):
                    _v = out.get(_k)
                    if isinstance(_v, str) and _v.strip():
                        return True
        try:
            from X5Browser.catalog import ALL_TOOLS as _BROWSER_ALL
            if name in _BROWSER_ALL:
                from X5Browser.events import should_show_arrow as _browser_arrow
                return bool(_browser_arrow(name))
        except Exception:
            pass
        try:
            import sys as _sys
            from pathlib import Path as _P
            import importlib as _il
            from core.agent.loop.dispatcher import _CORE_MAP, _DISCOVERABLE_MAP as _DMAP
            dotted = {**_CORE_MAP, **_DMAP}.get(name)
            if dotted:
                parts = dotted.split(".")
                events_mod = ".".join(parts[:-1] + ["events"])
                mod = _il.import_module(events_mod)
                flag = getattr(mod, "SHOW_ARROW", False)
                return bool(flag)
        except Exception:
            pass
        return False
    except Exception:
        return False


def _basename(path: Any) -> str:
    """Filename only from a Workspace path (never leaks full paths). Never raises."""
    try:
        s = str(path or "").strip().replace("\\", "/")
        if not s:
            return ""
        return s.rsplit("/", 1)[-1][:80]
    except Exception:
        return ""


def _short_error(err: Any) -> str | None:
    """One short human line for a failed tool call (bounded, never a dump).

    First non-empty line only, machine prefix (`error: <code> - `) stripped,
    so the arrow shows the cause instead of a protocol string. Never raises.
    """
    try:
        if not isinstance(err, str) or not err.strip():
            return None
        import re as _re
        line = ""
        for raw in err.splitlines():
            if raw.strip():
                line = raw.strip()
                break
        if not line:
            return None
        line = _re.sub(r"^error\s*:\s*[\w-]+\s*[-–:]\s*", "", line).strip() or line
        return line[:200]
    except Exception:
        return None


def _summarize_output(result: dict, name: str | None = None, args: Any = None) -> str | None:
    """Curated per-tool detail shown UNDER the reason inside a step arrow (bounded).

    Policy (per request): the arrow shows the model's `reason` only; raw tool
    output is never dumped. Only these tools add a short curated line below
    the reason — everything else returns None (reason-only, or no arrow when
    the model wrote no reason and no image arrived):
    - todo_list: task + steps as step lines only
    - write_file: file name + written size + short after-preview (before is
      unrecoverable after overwrite, so only after is shown)
    - read_file: file name(s) read (args-based, never content)
    - search_files: query + match count + up to 3 names (never content)
    - workspace: action + path/count only (never full listing)
    - terminal: full command + exit code + output tail (bounded, never full log)
    - document_search: file name + query (never chunk text)
    - vector_chat_search: query only (never message text)
    - navigate: URL only (args-based)
    - question: Q + the user's answer (the answer is shown here, never as
      a user bubble; unanswered → question only)
    - pdf_studio: studio id + open card; studio_page: slide result line
    - svg_maker: icon file name + size (never SVG markup)
    Failures return one short human line (machine prefix stripped), so every
    failed call gets an expandable arrow with the cause instead of a dump.
    Model-skips return their short sentence too.
    Screenshot tools show their image via media instead of text here.
    Never raises.
    """
    try:
        if not isinstance(result, dict):
            return None
        if not result.get("success"):
            return _short_error(result.get("error"))
        out = result.get("output") if isinstance(result.get("output"), dict) else {}
        if isinstance(out, dict) and out.get("skipped"):
            msg = str(out.get("message") or "").strip()
            return msg[:200] if msg else None
        safe_args = args if isinstance(args, dict) else {}
        tool = str(name or "").strip()

        if tool == "todo_list":
            todo = out.get("todo") if isinstance(out.get("todo"), dict) else {}
            task = str(todo.get("task") or safe_args.get("task") or "").strip()[:120]
            steps = todo.get("steps") if isinstance(todo.get("steps"), list) else safe_args.get("steps")
            lines: list[str] = []
            if task:
                lines.append(task)
            if isinstance(steps, list):
                for s in steps[:8]:
                    if isinstance(s, str) and s.strip():
                        lines.append("\u2022 " + s.strip()[:120])
            return "\n".join(lines)[:500] or None

        if tool == "read_file":
            base = _basename(safe_args.get("path") or out.get("path"))
            return base or None

        if tool == "search_files":
            q = str(safe_args.get("query") or safe_args.get("pattern") or safe_args.get("name") or "").strip()[:80]
            matches = out.get("matches") if isinstance(out.get("matches"), list) else out.get("results") if isinstance(out.get("results"), list) else []
            names: list[str] = []
            if isinstance(matches, list):
                for m in matches[:3]:
                    if isinstance(m, dict):
                        cand = m.get("path") or m.get("name") or m.get("file")
                        base = _basename(cand)
                        if base:
                            names.append(base)
                    elif isinstance(m, str) and m.strip():
                        base = _basename(m)
                        if base:
                            names.append(base)
            count = out.get("count") if isinstance(out.get("count"), int) else (len(matches) if isinstance(matches, list) else 0)
            head = f"\u201c{q}\u201d: {count} files" if q else f"{count} files"
            if names:
                head += " — " + ", ".join(names)
            return head[:300]

        if tool == "write_file":
            base = _basename(safe_args.get("path") or out.get("path"))
            preview = str(safe_args.get("content") or "").strip()
            size = out.get("bytes_written") if isinstance(out.get("bytes_written"), int) else len(preview.encode("utf-8", "ignore"))
            head = (base or "file") + (f" (+{size} bytes)" if isinstance(size, int) and size else "")
            if not preview:
                return head[:120]
            tail = preview[:240].strip()
            return (head + "\n" + tail)[:400]

        if tool == "workspace":
            action = str(safe_args.get("action") or "").strip()[:20]
            target = _basename(safe_args.get("path") or safe_args.get("name") or "")
            count = out.get("count") if isinstance(out.get("count"), int) else None
            items = out.get("items") if isinstance(out.get("items"), list) else out.get("files") if isinstance(out.get("files"), list) else None
            if isinstance(count, int):
                detail = f"{action or 'list'}: {count} items" if action else f"{count} items"
            elif isinstance(items, list):
                detail = f"{action or 'list'}: {len(items)} items"
            elif target:
                detail = f"{action} {target}".strip() if action else target
            else:
                detail = action or None
            return detail[:160] if detail else None

        if tool == "terminal":
            cmd = str(safe_args.get("command") or out.get("command") or "").strip()[:1000]
            code = out.get("returncode")
            tail = str(out.get("stdout") or out.get("stderr") or "").strip()
            tail = tail[-1500:].strip() if len(tail) > 1500 else tail
            lines = []
            if cmd:
                lines.append(cmd)
            lines.append(f"exit {code}" if isinstance(code, int) else "done")
            if tail:
                lines.append(tail)
            return "\n".join(lines)[:2000] or None

        if tool == "document_search":
            base = _basename(safe_args.get("file_id") or safe_args.get("path"))
            q = str(safe_args.get("query") or "").strip()[:100]
            if base and q:
                return f"{base} — \u201c{q}\u201d"[:200]
            return (base or (f"\u201c{q}\u201d" if q else None) or None)

        if tool == "vector_chat_search":
            q = str(safe_args.get("query") or safe_args.get("q") or "").strip()[:120]
            return f"\u201c{q}\u201d"[:140] if q else None

        if tool == "navigate":
            dest = str(safe_args.get("to") or safe_args.get("url") or "").strip()[:160]
            return dest or None

        if tool == "question":
            # The answer lives inside the step arrow (no user bubble is ever
            # posted): question + the user's answer, bounded. Unanswered →
            # question only, so the step still carries context.
            q = str(safe_args.get("question") or out.get("question") or "").strip()
            a = str(out.get("answer") or "").strip()
            lines: list[str] = []
            if q:
                lines.append("Q: " + q[:200])
            if a:
                lines.append("A: " + a[:300])
            return "\n".join(lines)[:520] or None

        # Default: reason-only (no output dump). Images travel via media.
        # pdf_studio: studio id + studio link, so the arrow shows the link
        # the agent knows, and gateway chats can copy it as plain text.
        if tool == "pdf_studio":
            # NOTE: never include the [STUDIO:id] token here — the open card
            # must render at the END of the reply, never inside this arrow.
            try:
                sid = str(out.get("studio_id") or safe_args.get("studio_id") or "").strip()[:40]
                url = str(out.get("open_url") or out.get("studio_url") or "").strip()[:120]
                if not url and sid:
                    url = f"/studio-pdf/{sid}"
                _act = str(out.get("action") or safe_args.get("action") or "").strip().lower()
                if _act == "rename":
                    return f"Studio {sid} renamed"[:200] if sid else "Studio renamed"[:200]
                if _act == "list":
                    try:
                        _n = len(out.get("studios") or [])
                    except Exception:
                        _n = 0
                    return f"Studios listed · {_n}"[:200]
                if _act == "get":
                    return f"Studio {sid} viewed · {url}"[:200] if sid else "Studio viewed"[:200]
                if sid:
                    return f"Studio {sid} ready · {url}"[:200] or None
                return "Studio ready"[:200]
            except Exception:
                return None
        if tool == "studio_page":
            try:
                act = str(safe_args.get("action") or "").strip().lower()
                n = out.get("page_no") if isinstance(out.get("page_no"), int) else safe_args.get("page_no")
                err = str(out.get("error") or "").strip()[:200]
                if act == "list":
                    return "Slides list"[:120]
                if act == "read":
                    has = out.get("has_code")
                    return f"Slide {n}: {'has code' if has else 'empty'}"[:120]
                if err:
                    return f"Page {n}: {err}"[:300]
                if act == "patch":
                    return f"Page {n} section patched and hot-reloaded"[:200] if isinstance(n, int) else "Page section patched and hot-reloaded"[:200]
                if isinstance(n, int):
                    return f"Page {n} approved and hot-reloaded"[:200]
                return "Page approved and hot-reloaded"[:200]
            except Exception:
                return None
        if tool == "svg_maker":
            try:
                base = _basename(safe_args.get("icon_name") or out.get("file") or out.get("icon_name"))
                size = out.get("size") if isinstance(out.get("size"), int) else safe_args.get("size")
                head = (base or "icon") + (f" ({size}x{size})" if isinstance(size, int) else "")
                return head[:120]
            except Exception:
                return None
        if tool == "generate_image":
            try:
                base = _basename(out.get("image_path") or out.get("image"))
                return (base or "image ready")[:120]
            except Exception:
                return None
        return None
    except Exception:
        return None


_LIVE_PAUSE_TOOLS = frozenset({"human_control"})
_LIVE_EVENT_TOOLS = frozenset({"live_preview", "show_preview", "close_browser", "restart_browser"})


def _redact_live_url(result: dict) -> None:
    """Hide live stream URLs from the model: the player renders them directly.
    The pause/notify paths above already used the real URLs — only the stored
    tool message (model-visible) is redacted, in place. Emptied (not labeled)
    so the model has nothing to quote."""
    try:
        out = result.get("output")
        if not isinstance(out, dict):
            return
        redacted = dict(out)
        changed = False
        for key in ("url", "preview_url", "current_url"):
            if isinstance(redacted.get(key), str) and redacted.get(key):
                redacted[key] = ""
                changed = True
        if isinstance(redacted.get("token"), str) and redacted.get("token"):
            redacted["token"] = ""
            changed = True
        pv = redacted.get("preview")
        if isinstance(pv, dict) and isinstance(pv.get("url"), str) and pv.get("url"):
            pv = dict(pv)
            pv["url"] = ""
            redacted["preview"] = pv
            changed = True
        st = redacted.get("state")
        if isinstance(st, dict) and isinstance(st.get("live_url"), str) and st.get("live_url"):
            st = dict(st)
            st["live_url"] = ""
            redacted["state"] = st
            changed = True
        if changed:
            result["output"] = redacted
    except Exception:
        pass


def _event_media(name: str, result: dict) -> dict | None:
    """Structured extras for one completed tool call (screenshot/stream). Never raises."""
    try:
        if not isinstance(result, dict) or not result.get("success"):
            return None
        out = result.get("output")
        if not isinstance(out, dict):
            return None
        media: dict = {}
        if out.get("has_screenshot") and isinstance(out.get("screenshot_path"), str):
            media["shot"] = str(out["screenshot_path"])[:500]
        # Generated images (generate_image + any tool returning image_path/image
        # basename) travel here so steps and the end-of-reply card share them.
        try:
            _img_cand = out.get("image_path") if isinstance(out.get("image_path"), str) else (out.get("image") if isinstance(out.get("image"), str) else "")
            _img_cand = (_img_cand or "").strip()
            if _img_cand and "/" not in _img_cand.replace("\\", "/") and ".." not in _img_cand and len(_img_cand) <= 100:
                _low = _img_cand.lower()
                if _low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    media["image"] = _img_cand
        except Exception:
            pass
        # TTS voice records are rendered once as a WhatsApp-style bubble on the
        # assistant message (see _collect_final_audio), never inside the step.
        _stream_url = None
        _stream_vo = True
        if name in _LIVE_PAUSE_TOOLS or name in _LIVE_EVENT_TOOLS:
            # Watch-only flow: show_preview/live_preview must
            # carry the PREVIEW token link (out["url"] from live_preview is
            # already the preview token; guard anyway). The paused handoff
            # (human_control) carries the interactive control URL.
            src = out
            if name in ("show_preview", "live_preview"):
                try:
                    from X5Browser.service import live_links as _live_links
                    _links = _live_links()
                    _purl = _links.get("preview") if isinstance(_links, dict) else ""
                    if isinstance(_purl, str) and _purl.startswith(("http://", "https://")):
                        src = {**out, "url": _purl, "view_only": True}
                except Exception:
                    src = out
            url = src.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                _stream_url = url[:2000]
                if name in ("show_preview", "live_preview"):
                    _stream_vo = True
                else:
                    _stream_vo = bool(src.get("view_only", False))
        if _stream_url is None:
            pv = out.get("preview")
            if isinstance(pv, dict):
                purl = pv.get("url")
                if isinstance(purl, str) and purl.startswith(("http://", "https://")):
                    _stream_url = purl[:2000]
                    _stream_vo = bool(pv.get("view_only", True))
        if _stream_url is not None:
            media["stream"] = {"url": _stream_url, "view_only": _stream_vo, "tool": name}
        return media or None
    except Exception:
        return None


def _resolve_conversation_path() -> Path:
    # Unified: writes ALWAYS to X5Agent/Data (zip backup). Fallback legacy only if resolver missing.
    try:
        from core.paths import data_db_file as _df
        return _df("conversation.json")
    except Exception:
        return STORAGE_DB / "conversation.json"


def _resolve_legacy_conversation_path() -> Path:
    return STORAGE_DB / "conversation.json"


def _collect_final_images(tool_calls_log: list) -> list[dict]:
    """Images for the end-of-reply card (screenshots + generated images).

    Shown under the answer (and after refresh) as one unified template.
    Bounded (6), deduped by kind+value. Never raises."""
    images: list[dict] = []
    seen: set[str] = set()
    try:
        for entry in (tool_calls_log or [])[-12:]:
            if not isinstance(entry, dict):
                continue
            res = entry.get("result") or {}
            if not isinstance(res, dict) or not res.get("success"):
                continue
            out = res.get("output") or {}
            if not isinstance(out, dict):
                continue
            if entry.get("tool") == "screenshot" and out.get("has_screenshot"):
                shot = out.get("screenshot_path")
                if isinstance(shot, str) and shot and f"shot:{shot}" not in seen:
                    seen.add(f"shot:{shot}")
                    images.append({"shot": shot[:500]})
                    if len(images) >= 6:
                        break
                continue
            if entry.get("tool") == "image_search":
                # Workspace pictures (main agent only — studio turns never
                # persist final images, and studio outputs carry no `file`).
                for meta in (out.get("images") or [])[:6]:
                    if not isinstance(meta, dict):
                        continue
                    rel = str(meta.get("file") or "")
                    if (rel.startswith("images/") and ".." not in rel
                            and len(rel) <= 200
                            and rel.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
                            and f"ws:{rel}" not in seen):
                        seen.add(f"ws:{rel}")
                        images.append({"workspace": rel})
                        if len(images) >= 6:
                            break
                continue
            cand = out.get("image_path") if isinstance(out.get("image_path"), str) else (out.get("image") if isinstance(out.get("image"), str) else "")
            cand = (cand or "").strip()
            if not cand or "/" in cand.replace("\\", "/") or ".." in cand or len(cand) > 100:
                continue
            if not cand.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                continue
            key = f"image:{cand}"
            if key in seen:
                continue
            seen.add(key)
            images.append({"image": cand})
            if len(images) >= 6:
                break
    except Exception:
        pass
    return images


def _persist_steps(tool_calls_log: list) -> list[dict]:
    """Flatten tool calls into bounded UI steps so history reloads keep every
    event, arrow and output exactly as shown live. Never raises."""
    steps: list[dict] = []
    try:
        for entry in (tool_calls_log or [])[-12:]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("tool") or ""
            res = entry.get("result") or {}
            text = None
            if isinstance(res, dict) and res.get("success"):
                out = res.get("output") or {}
                if isinstance(out, dict) and out.get("event"):
                    text = str(out["event"])[:80]
            if not text:
                # Failures keep the friendly tool label too — the error itself
                # travels as the expandable output (arrow), not as the label.
                try:
                    text = _preview_event(name, entry.get("arguments"))
                except Exception:
                    text = name or "tool"
            item: dict = {"event": str(text)[:80]}
            try:
                _rsn = _tool_reason(name, entry.get("arguments"),
                                    entry.get("reason") or "")
                if _rsn:
                    item["reason"] = _rsn
            except Exception:
                pass
            try:
                excerpt = _summarize_output(res, name, entry.get("arguments"))
            except TypeError:
                try:
                    excerpt = _summarize_output(res, name)
                except TypeError:
                    try:
                        excerpt = _summarize_output(res)
                    except Exception:
                        excerpt = None
            except Exception:
                excerpt = None
            if excerpt:
                item["output"] = excerpt
            try:
                if isinstance(res, dict) and not res.get("success"):
                    item["failed"] = True
            except Exception:
                pass
            try:
                media = _event_media(name, res)
                shot = media.get("shot") if isinstance(media, dict) else None
                if isinstance(shot, str) and shot:
                    item["shot"] = shot[:500]
                img = media.get("image") if isinstance(media, dict) else None
                if isinstance(img, str) and img:
                    item["image"] = img[:100]
            except Exception:
                pass
            steps.append(item)
    except Exception:
        pass
    return steps


def _collect_final_audio(tool_calls_log: list) -> list[dict]:
    """Voice records spoken via text_to_speech, for the standalone chat bubble.
    Bounded, deduped by filename. It plays from the assistant message live and
    after refresh (served by /api/audio). Never raises."""
    audios: list[dict] = []
    seen: set[str] = set()
    try:
        for entry in (tool_calls_log or [])[-6:]:
            if not isinstance(entry, dict):
                continue
            res = entry.get("result") or {}
            if not isinstance(res, dict) or not res.get("success"):
                continue
            out = res.get("output") or {}
            if not isinstance(out, dict):
                continue
            name = out.get("audio_path")
            if not isinstance(name, str) or not name.strip():
                continue
            base = Path(name.strip()).name
            if not base or base in seen:
                continue
            seen.add(base)
            try:
                dur = round(float(out.get("duration_sec", 0) or 0), 2)
            except Exception:
                dur = 0.0
            audios.append({
                "audio": base[:100],
                "duration": dur,
                "voice": str(out.get("voice", "") or "")[:16],
                "lang": str(out.get("lang", "") or "")[:8],
            })
            if len(audios) >= 4:
                break
    except Exception:
        pass
    return audios


def _collect_final_files(tool_calls_log: list) -> list[dict]:
    """Workspace files produced via tools (e.g. studio pages), for the file card.

    Scans for successful results carrying a Workspace-relative `file` path
    (plus slide images for pdf decks, so the preview can offer them too).
    Bounded (4 files, 20 images each), deduped by path. Never raises.
    """
    files: list[dict] = []
    seen: set[str] = set()
    try:
        from core.paths import resolve_inside_workspace
    except Exception:
        resolve_inside_workspace = None  # type: ignore[assignment]
    try:
        for entry in (tool_calls_log or [])[-12:]:
            if not isinstance(entry, dict):
                continue
            res = entry.get("result") or {}
            if not isinstance(res, dict) or not res.get("success"):
                continue
            out = res.get("output") or {}
            if not isinstance(out, dict):
                continue
            rel = out.get("file")
            if not isinstance(rel, str) or not rel.strip() or rel.strip() in seen:
                continue
            rel = rel.strip()[:300]
            size = 0
            if resolve_inside_workspace is not None:
                try:
                    target = resolve_inside_workspace(rel)
                    if target.is_file():
                        size = target.stat().st_size
                    else:
                        continue
                except Exception:
                    continue
            seen.add(rel)
            item: dict = {"name": rel.rsplit("/", 1)[-1][:100], "path": rel, "size": size}
            try:
                images: list[dict] = []
                if isinstance(out.get("slides"), list):
                    folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
                    for slide in out["slides"][:50]:
                        if not isinstance(slide, dict):
                            continue
                        for img in (slide.get("images") or [])[:10]:
                            if not isinstance(img, dict):
                                continue
                            nm = str(img.get("file") or "").strip()
                            if not nm or "/" in nm.replace("\\", "/") or ".." in nm:
                                continue
                            irel = f"{folder}/{nm}" if folder else nm
                            if irel in seen:
                                continue
                            if resolve_inside_workspace is not None:
                                try:
                                    if not resolve_inside_workspace(irel).is_file():
                                        continue
                                except Exception:
                                    continue
                            seen.add(irel)
                            images.append({"name": nm[:100], "path": irel})
                            if len(images) >= 20:
                                break
                        if len(images) >= 20:
                            break
                if images:
                    item["images"] = images
            except Exception:
                pass
            try:
                count = out.get("slides_done") or out.get("slides") or []
                if isinstance(count, list) and count:
                    item["pages"] = len(count)
            except Exception:
                pass
            files.append(item)
            if len(files) >= 4:
                break
    except Exception:
        pass
    return files


def _persist_conversation(user_input: str, assistant_output: str, steps: list | None = None,
                           images: list | None = None, elapsed: int | None = None,
                           audios: list | None = None, session_key: str | None = None,
                           reuse_user_id: str | None = None, files: list | None = None,
                           stopped: bool = False, resume_units: list | None = None):
    import uuid
    if isinstance(session_key, str) and session_key.strip():
        # Branch session (one per scheduled job / trigger): NEVER touches the
        # main chat or its vector memory. "trigger:<id>" goes to the trigger's
        # own hidden session, anything else to the scheduler session.
        # On any failure skip persistence (fail clean).
        try:
            import sys as _sys
            from pathlib import Path as _P
            _proj = _P(__file__).resolve().parents[3]
            if str(_proj) not in _sys.path:
                _sys.path.insert(0, str(_proj))
            _key = session_key.strip()
            if _key.startswith("trigger:") and len(_key) > len("trigger:"):
                from core.trigger.session import append_session as _sess_append
                _branch = _key[len("trigger:"):]
            else:
                from core.scheduler.session import append_session as _sess_append
                _branch = _key
            extra = {}
            if isinstance(steps, list) and steps:
                extra["steps"] = steps[:12]
            if isinstance(elapsed, int) and elapsed >= 0:
                extra["elapsed"] = elapsed
            if isinstance(images, list) and images:
                extra["images"] = images[:6]
            if isinstance(audios, list) and audios:
                extra["audio"] = audios[:4]
            if isinstance(files, list) and files:
                extra["files"] = files[:4]
            if stopped:
                # STOP marker for the UI (frozen Working + Stopped divider,
                # never the note text). The note itself stays in content so
                # the model keeps the full context.
                extra["stopped"] = True
                if isinstance(resume_units, list) and resume_units:
                    extra["resume_units"] = resume_units
            # Edit/regenerate resend: the edited user bubble already persists
            # (server updated it in place) — append ONLY the assistant reply,
            # same contract as the main chat (fixes the duplicated-message bug).
            if isinstance(reuse_user_id, str) and reuse_user_id.strip():
                a = _sess_append(_branch, "assistant", assistant_output[:5000], extra or None)
                if a:
                    return reuse_user_id.strip()[:16], a[0]
                print(f"[loop] session persist failed for key={session_key.strip()[:16]} — skipped (main chat untouched)")
                return None
            u = _sess_append(_branch, "user", user_input[:2000])
            a = _sess_append(_branch, "assistant", assistant_output[:5000], extra or None)
            if u and a:
                return u[0], a[0]
            print(f"[loop] session persist failed for key={session_key.strip()[:16]} — skipped (main chat untouched)")
            return None
        except Exception as e:
            print(f"[loop] session persist error: {e} — skipped (main chat untouched)")
            return None
    path = _resolve_conversation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for cand in (path, _resolve_legacy_conversation_path()):
        if cand.exists():
            try:
                loaded = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(loaded, list) and loaded:
                    data = loaded
                    break
            except Exception:
                continue
    # ensure each message has id for vector store
    # reuse_user_id (edit resend): the edited user bubble already persists —
    # append ONLY the assistant reply (no duplicate user message, the fix for
    # the double-reply bug). Otherwise append a fresh user + assistant pair.
    if isinstance(reuse_user_id, str) and reuse_user_id.strip():
        uid = reuse_user_id.strip()[:16]
        if not stopped:
            # Continue-after-STOP resumes in place: the trailing stopped
            # marker now continues, so its Stopped divider goes away (the UI
            # renders its steps only). The note text stays for model context.
            # A re-stop (stopped=True) keeps its own fresh marker. Never
            # fails persistence.
            try:
                if data and isinstance(data[-1], dict) and data[-1].get("role") == "assistant" and data[-1].get("stopped") is True:
                    data[-1].pop("stopped", None)
                    data[-1].pop("resume_units", None)
                    data[-1]["resumed"] = True
            except Exception:
                pass
        else:
            # Re-stop of the same resumed task (stop → continue → stop):
            # refresh the trailing stopped marker in place (latest steps)
            # instead of stacking another Stopped divider below it.
            try:
                _last = data[-1] if data else None
                if (isinstance(_last, dict) and _last.get("role") == "assistant"
                        and _last.get("stopped") is True and _last.get("id")):
                    _last["content"] = assistant_output[:5000]
                    if isinstance(steps, list) and steps:
                        _last["steps"] = steps[:12]
                    if isinstance(elapsed, int) and elapsed >= 0:
                        _last["elapsed"] = elapsed
                    if isinstance(resume_units, list) and resume_units:
                        _last["resume_units"] = resume_units
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    return uid, str(_last["id"])[:16]
            except Exception:
                pass
    else:
        uid = str(uuid.uuid4())[:8]
        data.append({"id": uid, "role": "user", "content": user_input[:2000], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    aid = str(uuid.uuid4())[:8]
    assistant_msg: dict = {"id": aid, "role": "assistant", "content": assistant_output[:5000], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if isinstance(elapsed, int) and elapsed >= 0:
        assistant_msg["elapsed"] = elapsed
    if isinstance(steps, list) and steps:
        assistant_msg["steps"] = steps[:12]
    if isinstance(images, list) and images:
        assistant_msg["images"] = images[:6]
    if isinstance(audios, list) and audios:
        assistant_msg["audio"] = audios[:4]
    if isinstance(files, list) and files:
        assistant_msg["files"] = files[:4]
    if stopped:
        assistant_msg["stopped"] = True
        if isinstance(resume_units, list) and resume_units:
            assistant_msg["resume_units"] = resume_units
    data.append(assistant_msg)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Auto-create embeddings for new messages (per-message, no fallback).
    # Reused user id: embed the assistant reply only (user already embedded).
    try:
        from core.agent.memory.chat_vector_store import add_message
        if not (isinstance(reuse_user_id, str) and reuse_user_id.strip()):
            add_message(uid, user_input[:2000], role="user")
        add_message(aid, assistant_output[:5000], role="assistant")
    except Exception:
        pass
    return uid, aid


_UNIT_LIMIT = 30
_RESUME_CONTENT_MAX = 4000


def _clip_resume_units(units: list | None) -> list:
    """Bound loop units stored on a STOP marker for Continue. Never raises."""
    out: list = []
    try:
        for unit in (units or [])[-_UNIT_LIMIT:]:
            if not isinstance(unit, list):
                continue
            clipped: list = []
            for m in unit:
                if not isinstance(m, dict):
                    continue
                role = m.get("role")
                if role not in ("assistant", "tool"):
                    continue
                msg: dict = {"role": role}
                if role == "assistant":
                    if m.get("content"):
                        msg["content"] = str(m.get("content"))[:_RESUME_CONTENT_MAX]
                    if isinstance(m.get("tool_calls"), list) and m["tool_calls"]:
                        msg["tool_calls"] = m["tool_calls"][:8]
                else:
                    msg["tool_call_id"] = str(m.get("tool_call_id") or "")[:64]
                    c = str(m.get("content") or "")
                    msg["content"] = c[:_RESUME_CONTENT_MAX] + ("\n... truncated" if len(c) > _RESUME_CONTENT_MAX else "")
                clipped.append(msg)
            if clipped:
                out.append(clipped)
    except Exception:
        return []
    return out


def _units_from_steps(steps: list | None) -> list:
    """Fallback Continue context from UI steps when resume_units is absent."""
    lines: list[str] = []
    try:
        for s in (steps or [])[:40]:
            if isinstance(s, dict):
                ev = str(s.get("event") or "").strip()[:80]
                rsn = str(s.get("reason") or "").strip()[:120]
                if ev:
                    lines.append(f"- {ev}" + (f" ({rsn})" if rsn else ""))
            elif isinstance(s, str) and s.strip():
                lines.append(f"- {s.strip()[:80]}")
    except Exception:
        lines = []
    if not lines:
        return []
    text = ("Progress already completed this turn. Continue from here; "
            "do not repeat these steps:\n" + "\n".join(lines))
    return [[{"role": "assistant", "content": text[:_RESUME_CONTENT_MAX]}]]


def _load_resume_units(reuse_user_id: str) -> list:
    """Units saved on the stopped marker after this user id, or steps fallback."""
    uid = (reuse_user_id or "").strip()[:16]
    if not uid:
        return []
    try:
        path = _resolve_conversation_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        for i, m in enumerate(data):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            if str(m.get("id") or "")[:16] != uid:
                continue
            nxt = data[i + 1] if i + 1 < len(data) else None
            if not isinstance(nxt, dict) or nxt.get("role") != "assistant":
                return []
            stored = nxt.get("resume_units")
            clipped = _clip_resume_units(stored) if isinstance(stored, list) else []
            if clipped:
                return clipped
            return _units_from_steps(nxt.get("steps") or [])
    except Exception:
        return []
    return []


def _restore_dynamic_from_units(units: list, known_tool_names: set, dynamic_tools: list) -> None:
    """Re-register tools discovered in a previous (stopped) turn. Never raises."""
    try:
        for unit in units or []:
            if not isinstance(unit, list):
                continue
            for m in unit:
                if not isinstance(m, dict) or m.get("role") != "tool":
                    continue
                try:
                    payload = json.loads(m.get("content") or "")
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                found = (payload.get("output") or {}).get("results") if isinstance(payload.get("output"), dict) else None
                if not isinstance(found, list):
                    continue
                for item in found:
                    if not isinstance(item, dict):
                        continue
                    spec = item.get("spec") or {}
                    tname = spec.get("name") or item.get("name")
                    if not tname or tname in known_tool_names or len(dynamic_tools) >= MAX_DYNAMIC_TOOLS:
                        continue
                    dynamic_tools.append(openai_tool_from_spec(
                        tname,
                        spec.get("description") or item.get("description", ""),
                        spec.get("inputs") or {},
                    ))
                    known_tool_names.add(tname)
    except Exception:
        pass


def _outgoing_messages(seed: list, units: list, limit: int = _UNIT_LIMIT) -> list:
    """Per-step provider input: seed + the last N conversation units.

    One unit = one model reply, or one tool call + its output together, so
    the model always sees what it did in the last 30 units with call/output
    linkage intact. The seed (system + full L4-L8 context, task included) is
    sent every step; full history stays in `messages`, only the send is
    windowed. Never raises.
    """
    try:
        count = int(limit) if isinstance(limit, int) else _UNIT_LIMIT
        if count < 1:
            count = _UNIT_LIMIT
        tail = units[-count:] if len(units) > count else units
        out = list(seed)
        for unit in tail:
            if isinstance(unit, list):
                out.extend(unit)
        return out
    except Exception:
        try:
            return list(seed)
        except Exception:
            return []


def _clear_temp_images_if_any():
    # Images are no longer tracked by a store: nothing to clear here.
    # Video is saved locally so its path can be sent as metadata, then deleted after analysis or loop end
    try:
        # Clean any remaining temp videos in Data/files/videos + legacy (including pending jobs)
        from pathlib import Path as _P
        proj = _P(__file__).resolve().parents[3]
        vdirs = []
        try:
            import sys as _sysv
            if str(proj) not in _sysv.path:
                _sysv.path.insert(0, str(proj))
            from core.paths import data_subdir as _dsv
            vdirs.append(_dsv("files", "videos"))
        except Exception:
            pass
        vdirs.append(proj / "storage" / "files" / "videos")
        for vdir in vdirs:
            if not vdir.exists():
                continue
            # Remove files older than current loop (all temp videos should be removed after loop)
            # For safety, we only delete files that are not part of a pending job? But spec says delete from storage after analysis/loop
            # We will delete all files in videos folder that are older than 0 (i.e., all temp) - but keep .gitkeep
            for f in vdir.iterdir():
                if f.is_file() and f.name != ".gitkeep":
                    try:
                        # Check if its job is already completed (if so, delete)
                        # For simplicity, delete all temp videos at loop end (they are not meant to be persisted)
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        pass


def run_loop(
    user_input: str,
    max_steps: int = 500,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.3,
    verbose: bool = True,
    isolated: bool = False,
    todo_snapshot: dict | None = None,
    on_event=None,
    on_tool_start=None,
    on_token=None,
    restricted: bool = False,
    on_question=None,
    on_live_event=None,
    cancel_flag=None,
    session_key: str | None = None,
    session_history: str | None = None,
    reuse_user_id: str | None = None,
    chat_limit: int = 30,
    resume_stopped: bool = False,
    system_override: str | None = None,
    studio_session: bool = False,
) -> dict[str, Any]:
    """
    Run agent loop for user_input.
    Returns dict: {steps, final_answer, history, tool_calls_log, success}
    Never fails silently - even on provider errors, continues up to max_steps.
    on_event(text, summary, key, media) is called live after every tool execution
    for UI streaming (best-effort); media carries {shot, stream} extras or None.
    on_token(step, delta_text) fires live per model content chunk (best-effort).
    restricted=True asks the user (via on_question) before every terminal call.
    on_question(kind, payload) pauses for ask/approval/live and returns the user's answer dict.
    on_live_event({"tool", "output", "success"}) fires for live_preview/
    close_browser completions (best-effort).
    isolated=True for scheduled large tasks (skips chat history, uses todo_snapshot).
    session_key: persist this run into the job's/trigger's own branch session
      instead of the main conversation ("trigger:<id>" → the trigger's hidden
      session, anything else → the scheduler session).
    session_history: that branch's recent history (holds the skill among its
      last 30) — replaces L6 when isolated, extends it otherwise.
    reuse_user_id: id of the ALREADY-persisted edited user bubble — the loop
      appends only the assistant reply (fixes the double-reply bug on edit).
    chat_limit: L6 history depth for this run (default 30; edit-resend uses 20:
      the edited message + the 20 before it).
    resume_stopped: seamless Continue — no new user message exists, so the
      internal instruction must reach NEITHER the UI NOR the model: user_input
      must be empty (server enforces), reuse_user_id is required (the reply
      attaches to the stopped task), and the context is built with
      resume_stopped so L8 is omitted and trailing stopped markers are
      excluded from L6. A LATER non-resume turn still sees the persisted
      marker and knows the task was stopped.
    system_override: replaces the L1-L3 system prompt (studio branch
      sessions run under their own system prompt, e.g. pdf_studio_system.md).
    studio_session: True only inside a PDF Studio branch session. Pre-seeds
      the session-only studio tool (studio_page) so the studio agent calls
      it directly, and rejects it in every other loop so the main agent can
      never run it.
    """
    if not isinstance(user_input, str):
        raise ValueError("user_input must be string")
    if resume_stopped:
        if not isinstance(reuse_user_id, str) or not reuse_user_id.strip():
            raise ValueError("resume_stopped requires reuse_user_id")
        user_input = ""
    elif not user_input.strip():
        raise ValueError("user_input must be non-empty string")
    if not isinstance(max_steps, int) or max_steps < 1 or max_steps > 500:
        raise ValueError("max_steps must be 1..500")
    if session_key is not None and (not isinstance(session_key, str) or not session_key.strip()):
        raise ValueError("session_key must be non-empty string")
    # Gate FIRST: no request runs without the built-in resources, even when a
    # template model is connected (embeddings back tool discovery + memory).
    try:
        from core.embedding.bootstrap import require_ready as _emb_ready
        _emb_ready()
    except Exception as e:
        refusal = str(e)
        # A refused resume persists nothing: the stopped marker and the
        # Continue pill stay intact so the user can retry after fixing the
        # cause (the refusal itself still reaches the UI as an error frame).
        if not resume_stopped:
            try:
                _persist_conversation(user_input, refusal, [], 0.0)
            except Exception:
                pass
        return {"steps": 0, "final_answer": refusal, "history": [],
                "tool_calls_log": [], "success": False, "no_embedding": True}
    mdl = model.strip() if model and isinstance(model, str) and model.strip() else "deepseek/deepseek-v4.1-flash"
    # Build initial context (isolated for large scheduled tasks).
    # Studio branch turns are fully isolated AND minimal: sentence + state
    # only, never the main session, never old page codes.
    system_prompt, user_context, core_tools = build_context(user_input, isolated=isolated, todo_snapshot=todo_snapshot, session_history=session_history, chat_limit=chat_limit, resume_stopped=resume_stopped, studio_mode=studio_session)
    if isinstance(system_override, str) and system_override.strip():
        # Branch session (studio): its own system prompt replaces L1-L3.
        system_prompt = system_override.strip()[:16000]
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_context},
    ]
    # Windowing: `seed` (system + full L4-L8 context) goes out every step;
    # every turn unit (one reply, or one call + its output) is appended to
    # `units`, and the provider only ever receives seed + last 30 units.
    seed = list(messages)
    units: list[list[dict]] = []
    if resume_stopped:
        try:
            units = _load_resume_units(reuse_user_id or "")
        except Exception:
            units = []
    # Universal text model (AI Models section). No provider-specific code here:
    # the record carries everything the engine needs (see core/ai_models).
    try:
        from core.ai_models.store import get_text_model as _get_text_model
        from core.ai_models.engine import chat as _universal_chat
    except Exception as e:
        raise RuntimeError(f"AI text model engine not available: {e}") from e
    _model_record = _get_text_model()
    if _model_record is None:
        try:
            from core.ai_models.store import missing_message as _missing
            refusal = _missing("text")
        except Exception:
            refusal = ("No AI Text Model connected. Open AI Models → AI Text Model (/ai-models), "
                       "press Test, then Save.")
        # A refused resume persists nothing (stopped marker + pill stay for retry).
        # Studio turns never touch the main chat either (branch session owns them).
        if not resume_stopped and not studio_session:
            try:
                _persist_conversation(user_input, refusal, [], 0.0)
            except Exception:
                pass
        return {"steps": 0, "final_answer": refusal, "history": list(messages),
                "tool_calls_log": [], "success": False, "no_model": True}
    # AIP/1 path: the record carries a declarative parser — the interpreter
    # sends messages + tools in their provider places (nothing is dropped).
    # Legacy connection JSON keeps the old engine. Pre-compile once so a
    # bad parser (e.g. a missing required variable) fails fast with its
    # name instead of once per step.
    _use_aip = False
    _compiled = None
    try:
        _pdef = _model_record.get("parser_definition") or _model_record.get("config_json") or {}
        _use_aip = isinstance(_pdef, dict) and _pdef.get("language") == "AIP/1"
    except Exception:
        _use_aip = False
    if _use_aip:
        try:
            from core.ai_models import aip as _aip_pre
            _compiled = _aip_pre.get_compiled(_model_record, "text")
        except Exception as e:
            refusal = f"AI Text Model configuration error: {e}"
            # A refused resume persists nothing (stopped marker + pill stay for retry).
            # Studio turns never touch the main chat either (branch session owns them).
            if not resume_stopped and not studio_session:
                try:
                    _persist_conversation(user_input, refusal, [], 0.0)
                except Exception:
                    pass
            return {"steps": 0, "final_answer": refusal, "history": list(messages),
                    "tool_calls_log": [], "success": False, "bad_parser": True}

    tool_calls_log: list[dict] = []
    steps = 0
    turn_t0 = time.time()
    final_answer: str | None = None
    cancelled = False
    persist_ids: tuple | None = None
    # Tools skipped this turn for a missing model connection, in order
    # (deduplicated by tool). Flags the final reply so the UI offers a
    # per-tool Connect/Skip choice. The loop itself never stops for this.
    skipped_models: list[dict] = []
    # Discovered tools become directly callable once tool_search returns their specs
    known_tool_names = {t.get("function", {}).get("name") for t in core_tools}
    dynamic_tools: list[dict] = []
    if studio_session:
        # Studio branch session: the studio tools are pre-seeded here
        # (never via tool_search), so each parallel per-slide turn in the
        # same session approves its own tasked page through studio_page,
        # views other slides through studio_read, fetches links/files
        # through http_request, and inspects images through vision_analyze
        # (only when a vision model is configured). All turns share this
        # one branch session and the same sentence+state context; one slide
        # runs at most one turn (the designing flag + the per-page cancel
        # registry enforce it), while different slides run in parallel.
        try:
            for _tname, _tdesc, _tinputs in _studio_tool_specs():
                if not _tname or _tname in known_tool_names or len(dynamic_tools) >= MAX_DYNAMIC_TOOLS:
                    continue
                dynamic_tools.append(openai_tool_from_spec(_tname, _tdesc, _tinputs))
                known_tool_names.add(_tname)
        except Exception:
            pass
    if resume_stopped and units:
        _restore_dynamic_from_units(units, known_tool_names, dynamic_tools)
    empty_strikes = 0

    for step in range(1, max_steps + 1):
        steps = step
        if _is_cancelled(cancel_flag):
            cancelled = True
            break
        if verbose:
            print(f"\n--- Loop step {step}/{max_steps} ---")
        # Call provider with bounded retries (provider does 3 retries internally).
        # Live token callback for this step (feeds SSE so the UI types the
        # final answer live). No preamble/narration is emitted for tool calls.
        def _emit_token(delta: str) -> None:
            try:
                if on_token is not None and isinstance(delta, str) and delta:
                    on_token(step, delta)
            except Exception:
                pass

        try:
            # First injection happened once above (seed); every later step
            # resends the seed plus only what is new (last 30 units).
            outgoing = _outgoing_messages(seed, units)
            # NOTE (protocol v1): the provider returns text only, so the
            # loop answers directly. Tool calling via the model returns in a
            # future protocol version; local tools stay wired and gated below.
            # What changed: the AIP/1 path sends messages + tools in their
            # provider places (the legacy engine never sent tools at all).
            def _emit_chunk(delta: str) -> None:
                _emit_token(delta)
            if _use_aip:
                from core.ai_models import aip as _aip_run
                _canonical = _aip_run.canonical_from_loop(
                    outgoing, list(core_tools) + list(dynamic_tools))
                _res = _aip_run.execute_compiled(_compiled, _canonical, cancel_event=cancel_flag)
                content_text = _res.get("text") or ""
                _emit_token(content_text)
                response = {"content": content_text,
                            "tool_calls": _res.get("tool_calls") or []}
            else:
                content_text = _universal_chat(
                    _model_record, outgoing,
                    {"on_chunk": _emit_chunk if on_token is not None else None,
                     "cancel_event": cancel_flag},
                )
                response = {"content": content_text, "tool_calls": []}
            if not response.get("tool_calls") and not (response.get("content") or "").strip():
                raise RuntimeError("model returned an empty reply with no usable tool calls")
        except Exception as e:
            # A STOP racing the provider call must win at once: break now
            # with no error note and no backoff sleep (the run is over).
            if _is_cancelled(cancel_flag):
                cancelled = True
                break
            # Provider failed even after retries - treat as tool error and continue
            err_msg = f"Provider error at step {step}: {e}"
            tech = str(getattr(e, "technical", "") or "")[:300]
            print(f"[loop] {err_msg}" + (f" | {tech}" if tech else ""))
            # Empty replies: retry briefly, then say so plainly instead of
            # persisting silence (500 silent steps help nobody).
            if "empty reply with no usable tool calls" in str(e):
                empty_strikes += 1
                if empty_strikes >= 3:
                    final_answer = ("The model returned empty replies several times. "
                                    "Please try again or check the AI model connection.")
                    break
            # Add an assistant error note to messages so next iteration can recover
            _note = {"role": "assistant", "content": f"[System note: provider temporarily unavailable: {str(e)[:200]}. Will retry.]"}
            messages.append(_note)
            units.append([_note])
            # Small backoff
            time.sleep(1)
            if step == max_steps:
                final_answer = f"[Loop reached max steps with provider errors] Last error: {e}"
                break
            continue

        content = response.get("content")
        # normalize() contract: flat calls [{id, name, arguments, ...}].
        # Drop malformed entries (nameless) - never execute nor persist them.
        tool_calls = [tc for tc in (response.get("tool_calls") or [])
                      if isinstance(tc, dict) and isinstance(tc.get("name"), str)
                      and tc.get("name").strip()]
        # STOP wins over a reply that arrived after cancel: persist the
        # stopped marker, never execute the just-returned tools.
        if _is_cancelled(cancel_flag):
            cancelled = True
            break
        if verbose:
            if content:
                print(f"[LLM content] {str(content)[:300]}")
            if tool_calls:
                print(f"[LLM tool_calls] {[tc.get('name') for tc in tool_calls]}")

        if not tool_calls:
            # No tools requested - final answer is agent content only.
            # (Empty replies never reach here: they raise into recovery above.)
            final_answer = content if isinstance(content, str) and content.strip() else ""
            messages.append({"role": "assistant", "content": final_answer})
            if verbose:
                print(f"[Loop finished at step {step} with final answer]")
            break

        # Execute each tool call
        # First, add assistant message with tool_calls (OpenAI wire format
        # needs nested calls for history resubmission - rebuilt from the
        # flat normalize() contract here, in this one place only)
        raw_tool_calls = []
        for tc in tool_calls:
            targs = tc.get("arguments", {})
            raw_tool_calls.append({
                "id": str(tc.get("id") or f"{step}-{tc.get('name')}"),
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(targs, ensure_ascii=False) if isinstance(targs, dict) else str(targs)
                }
            })
        assistant_msg = {"role": "assistant", "content": content or "", "tool_calls": raw_tool_calls}
        messages.append(assistant_msg)
        unit = [assistant_msg]

        # No narration for tool calls: tools run silently, only their event
        # line is shown. An optional `reason` arg is stripped before execute.
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("arguments", {})
            call_key = str(tc.get("id") or f"{step}-{name}")
            # Reason node: optional model-supplied `reason` (stripped, tools
            # never accept it). Empty when the model wrote none.
            try:
                clean_args, model_reason = _extract_model_reason(args)
            except Exception:
                clean_args, model_reason = args, ""
            try:
                call_reason = _tool_reason(name, clean_args, model_reason)
            except Exception:
                call_reason = ""
            args = _sanitize_tool_args(name, clean_args, studio_session)
            if verbose:
                print(f"  -> Executing {name} with {str(args)[:200]}")
            # Instant UI: show the call immediately (no output, no arrow yet).
            # Every tool call shows its own event. Permissions are re-read
            # from disk inside execute_tool on every call, so toggling a
            # plugin mid-loop applies to the very next tool call.
            if on_tool_start is not None:
                try:
                    try:
                        on_tool_start(_preview_event(name, args), call_key, call_reason, step)
                    except TypeError:
                        try:
                            on_tool_start(_preview_event(name, args), call_key, call_reason)
                        except TypeError:
                            try:
                                on_tool_start(_preview_event(name, args), call_key)
                            except TypeError:
                                on_tool_start(_preview_event(name, args))
                except Exception:
                    pass
            if not is_studio_call_allowed(name, studio_session=studio_session):
                # Wrong loop kind: session-only studio tools never run in
                # the main agent, and studio turns run only their 4 tools.
                if studio_session:
                    result = {"tool": name, "arguments": args, "success": False,
                              "error": (f"Tool {name} is not available in studio sessions — "
                                        "use only studio_page (approve the tasked slide), "
                                        "studio_read (view another slide), image_search "
                                        "(find photos), http_request "
                                        "(fonts/links)"
                                        " or vision_analyze (when a vision model is connected).")}
                else:
                    result = {"tool": name, "arguments": args, "success": False,
                              "error": f"Tool {name} runs inside PDF Studio sessions only, never in the main chat."}
            elif name == "question" and on_question is not None:
                result = _ask_user(name, args, on_question)
            elif restricted and name == "terminal" and on_question is not None:
                result = _approve_terminal(name, args, on_question)
            elif restricted and _is_dangerous_browser_tool(name) and on_question is not None:
                result = _approve_browser_tool(name, args, on_question)
            elif name == "tool_search":
                # No cap: search as many times as needed, the loop itself is bounded by max_steps.
                result = execute_tool(name, args)
            else:
                # Long tools (e.g. studio_page) receive the cancel event so
                # STOP aborts them mid-execution instead of after they finish.
                result = execute_tool(name, args, cancel_flag=cancel_flag)
            # Missing-model gate: pauses for the user's Connect/Skip choice.
            # Skip continues without the tool (the English skip sentence goes
            # to the model); Connect retries the SAME tool once the model is
            # connected. Only real skips flag the final reply, whose UI
            # renders the /ai-models link as a Connect button.
            try:
                _mkind = _missing_kind_of(result) if isinstance(result, dict) else None
            except Exception:
                _mkind = None
            if _mkind is not None:
                try:
                    result = _resolve_missing_model(name, args, _mkind, on_question, cancel_flag=cancel_flag)
                except Exception:
                    try:
                        result = _model_skipped_result(name, args, _mkind)
                    except Exception:
                        pass
                try:
                    _rout = result.get("output") if isinstance(result, dict) else None
                    _was_skipped = isinstance(_rout, dict) and bool(_rout.get("skipped"))
                    if _was_skipped and not any(s.get("tool") == name for s in skipped_models):
                        skipped_models.append({"tool": str(name)[:40], "kind": str(_mkind)[:16]})
                except Exception:
                    pass
            if _is_cancelled(cancel_flag):
                cancelled = True
                break
            tool_calls_log.append({"step": step, "tool": name, "arguments": args,
                                   "result": result, "reason": call_reason})
            # Live handoff: human_control pauses the loop so the user
            # can drive the browser; the UI resumes it via on_question("live").
            # live_preview never pauses — it only shows the floating player.
            if name in _LIVE_PAUSE_TOOLS and result.get("success") and on_question is not None:
                try:
                    _out = result.get("output", {})
                    _url = _out.get("url") if isinstance(_out, dict) else None
                    if isinstance(_url, str) and _url.startswith(("http://", "https://")):
                        on_question("live", {
                            "tool": name,
                            "url": _url[:2000],
                            "view_only": False,
                            "reason": str(call_reason or "")[:500],
                        })
                        # Resumed: hand the agent a fresh page tree so it
                        # continues from live refs (old ones expired).
                        try:
                            from core.agent.loop.dispatcher import execute_tool as _exec
                            _snap = _exec("page_snapshot", {"max_elements": 150, "mode": "full"})
                            _sout = _snap.get("output") if isinstance(_snap.get("output"), dict) else {}
                            _stext = _sout.get("text") if isinstance(_sout, dict) else None
                            if isinstance(result.get("output"), dict):
                                result["output"]["resumed"] = True
                                if isinstance(_stext, str) and _stext.strip():
                                    result["output"]["fresh_snapshot"] = _stext.strip()[:4000]
                                result["output"]["resume_note"] = (
                                    "The page was shown to the user, who pressed Continue. "
                                    "Resume the task from this fresh map; old refs expired.")
                        except Exception:
                            pass
                except Exception:
                    pass
            # Live notifications: preview start / handoff end / browser close.
            if name in _LIVE_EVENT_TOOLS and on_live_event is not None:
                try:
                    _out2 = result.get("output") if isinstance(result.get("output"), dict) else None
                    on_live_event({"tool": name, "output": _out2, "success": bool(result.get("success"))})
                except Exception:
                    pass
            # Register discovered tools so the model can call them directly next step.
            # Specs come from local spec.json files (trusted); names/args stay validated by dispatcher.
            if name == "tool_search" and result.get("success"):
                try:
                    found = (result.get("output") or {}).get("results") or []
                    for item in found:
                        if not isinstance(item, dict):
                            continue
                        spec = item.get("spec") or {}
                        tname = spec.get("name") or item.get("name")
                        if not tname or tname in known_tool_names or len(dynamic_tools) >= MAX_DYNAMIC_TOOLS:
                            continue
                        dynamic_tools.append(openai_tool_from_spec(
                            tname,
                            spec.get("description") or item.get("description", ""),
                            spec.get("inputs") or {},
                        ))
                        known_tool_names.add(tname)
                        if verbose:
                            print(f"     Registered discovered tool: {tname}")
                except Exception:
                    pass
            # event logging: print event if available
            try:
                out = result.get("output", {}) if result.get("success") else result
                ev = out.get("event") if isinstance(out, dict) else None
                if ev and verbose:
                    print(f"     Event: {ev}")
                if not result.get("success") and verbose:
                    print(f"     Error: {result.get('error')}")
            except Exception:
                pass
            # Live UI streaming: every tool call notifies with its own event
            # (best-effort, never breaks the loop). No default text anywhere.
            if on_event is not None:
                try:
                    out = result.get("output", {}) if result.get("success") else result
                    text = None
                    if isinstance(out, dict):
                        text = out.get("event")
                    # Titles are always the friendly preview label — raw error
                    # text never becomes a title (it lives, shortened, under
                    # the reason instead). Same rule as _persist_steps.
                    # The 7th arg carries the failure flag so the UI dot turns
                    # red on any failed call; old callbacks keep working via
                    # the TypeError fallback chain below.
                    if not text:
                        text = _preview_event(name, args)
                    if text:
                        try:
                            call_failed = not (isinstance(result, dict) and result.get("success"))
                            try:
                                on_event(str(text)[:80], _summarize_output(result, name, args), call_key,
                                         _event_media(name, result), call_reason, step, call_failed)
                            except TypeError:
                                try:
                                    on_event(str(text)[:80], _summarize_output(result, name, args), call_key,
                                             _event_media(name, result), call_reason, step)
                                except TypeError:
                                    try:
                                        on_event(str(text)[:80], _summarize_output(result, name, args), call_key,
                                                 _event_media(name, result), call_reason)
                                    except TypeError:
                                        try:
                                            on_event(str(text)[:80], _summarize_output(result, name, args), call_key,
                                                     _event_media(name, result))
                                        except TypeError:
                                            on_event(str(text)[:80], _summarize_output(result, name, args))
                        except Exception:
                            pass
                except Exception:
                    pass
            # Append tool result as tool message (live URLs redacted: the player
            # renders them directly, the model must never see or quote them).
            _redact_live_url(result)
            output_str = json.dumps(result, ensure_ascii=False, indent=2)
            if len(output_str) > 12000:
                output_str = output_str[:12000] + "\n... truncated"
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": output_str,
            }
            messages.append(tool_msg)
            unit.append(tool_msg)

        # The step's unit (call + all its outputs) is complete: visible to the
        # model from the next step on, inside the last-30 window.
        units.append(unit)
        # A mid-step cancel (fresh answer / STOP during a pause) stops now.
        if cancelled:
            break

        # Loop continues - next iteration will see tool outputs
        # Optional: update todo context? Already in messages, but we also could rebuild todo block as system note?
        # Keep loop bounded - check if we need to inject fresh context about new memories? Provider sees history, but we could also inject a short system note with updated memories if needed.
        # For simplicity, continue with history; context will be seen.
        # Small delay to avoid rate limits
        time.sleep(0.2)

        if step == max_steps and final_answer is None:
            final_answer = ""

    # Ensure final_answer exists — agent content only, never a default sentence.
    if final_answer is None:
        # Extract last assistant content if any
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("content"):
                final_answer = str(m["content"])
                break
        if final_answer is None:
            final_answer = ""

    # Persist conversation. Cancelled runs keep the stopped task in context
    # (user request + steps done so far + closing stopped note) so the next
    # turn sees it was stopped by the user and never continues it unasked.
    # No media for unfinished work — nothing final was produced.
    final_images: list[dict] = []
    final_files: list[dict] = []
    final_audio: list[dict] = []
    try:
        elapsed_sec = max(0, int(round(time.time() - turn_t0)))
    except Exception:
        elapsed_sec = 0
    # Studio branch turns persist NOTHING to the main chat — not even a
    # stopped marker. The studio branch session (core/studio/session.py)
    # owns its own history; the main session stays untouched.
    if cancelled and not studio_session:
        try:
            persist_ids = _persist_conversation(
                user_input, _STOPPED_NOTE, _persist_steps(tool_calls_log),
                [], elapsed_sec, [],
                session_key=session_key, reuse_user_id=reuse_user_id,
                stopped=True, resume_units=_clip_resume_units(units))
        except Exception:
            pass
    if not cancelled and not studio_session:
        try:
            final_images = _collect_final_images(tool_calls_log)
        except Exception:
            final_images = []
        try:
            final_files = _collect_final_files(tool_calls_log)
        except Exception:
            final_files = []
        try:
            final_audio = _collect_final_audio(tool_calls_log)
        except Exception:
            final_audio = []
        try:
            persist_ids = _persist_conversation(user_input, final_answer, _persist_steps(tool_calls_log), final_images, elapsed_sec, final_audio, session_key=session_key, reuse_user_id=reuse_user_id, files=final_files)
        except Exception:
            pass

        # Auto memory every 30 messages — main chat only, never branch sessions.
        if not session_key:
            try:
                from core.memory.auto_memory import maybe_trigger_auto_memory
                maybe_trigger_auto_memory()
            except Exception:
                pass

    # Clear temp images per spec (temp request images auto-cleared after vision or loop end)
    _clear_temp_images_if_any()

    return {
        "steps": steps,
        "final_answer": final_answer,
        "history": messages,
        "tool_calls_log": tool_calls_log,
        "model_used": mdl,
        "success": not cancelled,
        "cancelled": cancelled,
        "elapsed_sec": elapsed_sec,
        "final_images": final_images,
        "final_files": final_files,
        "final_audio": final_audio,
        "message_ids": {"user": persist_ids[0], "assistant": persist_ids[1]} if persist_ids else None,
        "needs_model_kind": skipped_models[0]["kind"] if skipped_models else None,
        "needs_model_tools": skipped_models[:10],
    }