"""
Tool dispatcher - maps tool names to execute functions for agent loop.
Handles both CORE and DISCOVERABLE tools.
Validates model-generated arguments (untrusted) before execution.
"""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Registry: name -> (module_path, spec)
# CORE = always in context. workspace + document_search live in core/tools on
# disk but are classified discoverable (behind tool_search) per product
# decision. There is no "trigger" tool: trigger_search alone searches AND
# manages all library + project triggers (core.tools.trigger.tool is only its
# internal implementation module, never exposed to the model).
# studio_page + studio_read stay registered here ONLY so the studio branch
# loop can execute them (pre-seeded there directly): tool_search never
# returns them and the main loop rejects them, so the main agent never
# sees or runs the session-only studio tools. http_request is a normal
# discoverable tool that is additionally allowed inside studio sessions.
_CORE_MAP = {
    "tool_search": "core.tools.tool_search.tool",
    "trigger_search": "core.tools.trigger_search.tool",
    "question": "core.tools.question.tool",
    "vector_chat_search": "core.tools.vector_chat_search.tool",
    "todo_list": "core.tools.todo_list.tool",
    "scheduler": "core.tools.scheduler.tool",
    "skills_search": "core.tools.skills_search.tool",
    "skills_view": "core.tools.skills_view.tool",
    "skill_create": "plugins.core_plugins.skill_create.tool",
    "terminal": "core.tools.terminal.tool",
    "wait": "core.tools.wait.tool",
}

_DISCOVERABLE_MAP = {
    "workspace": "core.tools.workspace.tool",
    "document_search": "core.tools.document_search.tool",
    "read_file": "plugins.core_plugins.read_file.tool",
    "write_file": "plugins.core_plugins.write_file.tool",
    "search_files": "plugins.core_plugins.search_files.tool",
    "vision_analyze": "plugins.core_plugins.vision_analyze.tool",
    "file_to_base64": "plugins.core_plugins.file_to_base64.tool",
    "file_metadata": "plugins.core_plugins.file_metadata.tool",
    "video_analyze": "plugins.core_plugins.video_analyze.tool",
    "video_transcribe": "plugins.core_plugins.video_transcribe.tool",
    "video_download": "plugins.core_plugins.video_download.tool",
    "audio_extract": "plugins.core_plugins.audio_extract.tool",
    "embedding_memory": "plugins.core_plugins.embedding_memory.tool",
    "text_to_speech": "plugins.core_plugins.text_to_speech.tool",
    "generate_image": "plugins.core_plugins.generate_image.tool",
    "memory": "plugins.core_plugins.memory.tool",
    "http_request": "plugins.core_plugins.http_request.tool",
    "pdf_studio": "plugins.core_plugins.pdf_studio.tool",
    "studio_page": "plugins.core_plugins.studio_page.tool",
    "studio_read": "plugins.core_plugins.studio_read.tool",
    "svg_maker": "plugins.core_plugins.svg_maker.tool",
    "image_search": "plugins.core_plugins.image_search.tool",
    "reply_assembly": "plugins.core_plugins.reply_assembly.tool",
}

_ALL = {**_CORE_MAP, **_DISCOVERABLE_MAP}


def _browser_tool_names() -> set[str]:
    """Browser tools owned by project/X5Browser/catalog.py. Empty when unavailable."""
    try:
        from X5Browser.catalog import ALL_TOOLS
        return set(ALL_TOOLS)
    except Exception:
        return set()


def _load_module(dotted: str):
    # Use importlib.import_module for proper package-relative imports (handles `from .events import`)
    # Ensure project root is in sys.path
    import sys
    import importlib
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    # Handle dash paths via file location (only tool-discovery). A compiled
    # release ships store.pyd instead of store.py, so the loader resolves both.
    if "tool-discovery" in dotted:
        from core.module_loader import load_from_path

        return load_from_path(
            PROJECT_ROOT / "plugins" / "tool-discovery" / "search" / "store.py", "store"
        )
    # Normal case: dotted is importable package now that we added __init__.py
    try:
        return importlib.import_module(dotted)
    except ImportError:
        # Fallback to file location if import fails
        parts = dotted.split(".")
        if parts[0] == "core" and len(parts) >= 4 and parts[1] == "tools":
            f = PROJECT_ROOT / "core" / "tools" / parts[2] / "tool.py"
        elif parts[0] == "plugins" and len(parts) == 4:
            f = PROJECT_ROOT / "plugins" / parts[1] / parts[2] / "tool.py"
        else:
            f = PROJECT_ROOT / "/".join(parts[:-1]) / "tool.py"
        from core.module_loader import load_from_path

        return load_from_path(f, dotted.replace(".", "_").replace("-", "_"))


def execute_tool(name: str, arguments: dict | str | None, cancel_flag: Any = None,
                 part_index: Any = None) -> dict:
    """
    Execute tool by name with validated arguments.
    Returns dict with tool output (or error) - never raises unhandled, loop continues.
    cancel_flag (optional threading.Event): forwarded ONLY to tools whose
    execute() accepts it (long-running tools abort on STOP); all other
    tools are called exactly as before.
    part_index (optional int): the loop's running part number, forwarded ONLY
    to tools whose execute() accepts it (reply_assembly labels its answer).
    """
    if not isinstance(name, str) or not name.strip():
        return {"error": "tool name missing", "tool": name}
    n = name.strip()
    _browser_names = _browser_tool_names()
    if n not in _ALL and n not in _browser_names:
        # Try discover additional plugins dynamically by scanning storage/vector? But just error
        return {"error": f"Unknown tool: {n}. Available: {sorted(list(_ALL.keys()) + sorted(_browser_names))}", "tool": n}
    dotted = _ALL.get(n, "")
    # Arguments validation: model-generated data is untrusted
    args: dict[str, Any] = {}
    if arguments is None:
        args = {}
    elif isinstance(arguments, dict):
        args = arguments
    elif isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(args, dict):
                return {"error": f"Tool {n} arguments must be JSON object, got {type(args)}", "tool": n}
        except Exception as e:
            return {"error": f"Tool {n} arguments invalid JSON: {e}", "tool": n}
    else:
        return {"error": f"Tool {n} arguments must be dict or JSON string", "tool": n}
    # Size limits: args JSON stringify truncated 10000 chars.
    # studio_page is exempt: a full page design is legitimately large (its
    # own trial-build + the 200KB store cap are the real guards). Without
    # this, every full-code delivery would be rejected and no design could
    # ever land.
    # reply_assembly is exempt too: one part of a very long reply is a legit
    # payload (the tool's own 30000-char cap + the loop's total cap guard it).
    try:
        if n not in ("studio_page", "reply_assembly") and len(json.dumps(args, ensure_ascii=False)) > 10000:
            return {"error": f"Tool {n} arguments too large (max 10000 chars)", "tool": n}
    except Exception:
        pass
    if n in _DISCOVERABLE_MAP:
        # Core plugins are always on (no switches, hidden from UI) — never gated.
        from core.tools.registry import get_plugin_state, is_core_plugin, is_plugin_effective

        if not is_core_plugin(n):
            # Disabled plugins (or disabled actions) are blocked even if called directly
            if not is_plugin_effective(n):
                return {"tool": n, "arguments": args, "error": f"Plugin disabled: {n}", "success": False}
            act = args.get("action")
            if isinstance(act, str):
                perms = get_plugin_state().get("plugins", {}).get(n, {}).get("permissions", {})
                if perms.get(act) is False:
                    return {"tool": n, "arguments": args, "error": f"Permission disabled: {n}.{act}", "success": False}
    if n in _browser_names:
        # All browser tools are core plugins: always on, hidden from UI, never gated.
        # Dangerous ones still ask in restricted mode (handled in engine).
        try:
            from X5Browser.tools import execute_browser_tool
            output = execute_browser_tool(n, args)
        except (TypeError, ValueError) as e:
            return {"tool": n, "arguments": args, "error": f"Invalid arguments for {n}: {e}", "success": False}
        except Exception as e:
            return {"tool": n, "arguments": args, "error": f"Tool {n} execution failed: {e}", "success": False}
        # Propagate the inner ok so callers (engine pause, server control)
        # see real failures instead of a masked success.
        inner_ok = True
        try:
            if isinstance(output, dict) and "ok" in output:
                inner_ok = bool(output.get("ok"))
        except Exception:
            inner_ok = True
        try:
            s = json.dumps(output, ensure_ascii=False)
            if len(s) > 15000:
                if isinstance(output, dict) and "observation" in output:
                    output["observation"] = str(output["observation"])[:10000] + "... truncated"
                elif isinstance(output, dict) and "text" in output:
                    output["text"] = str(output["text"])[:10000] + "... truncated"
        except Exception:
            pass
        if not inner_ok:
            err = ""
            try:
                if isinstance(output, dict):
                    err = str(output.get("error") or output.get("message") or "")[:500]
            except Exception:
                err = ""
            return {"tool": n, "arguments": args, "output": output,
                    "error": err or f"Tool {n} failed", "success": False}
        return {"tool": n, "arguments": args, "output": output, "success": True}
    if not dotted:
        return {"error": f"Unknown tool: {n}", "tool": n}
    try:
        mod = _load_module(dotted)
        if not hasattr(mod, "execute"):
            return {"error": f"Tool {n} has no execute function", "tool": n}
        # Optional reason is loop-level (shown before the call); only forward it
        # when the tool accepts it, otherwise drop it so old tools keep working.
        call_args = args
        if isinstance(args, dict) and "reason" in args:
            try:
                import inspect as _inspect
                _params = _inspect.signature(mod.execute).parameters
                _ok = ("reason" in _params) or any(
                    p.kind == _inspect.Parameter.VAR_KEYWORD for p in _params.values())
            except Exception:
                _ok = False
            if not _ok:
                call_args = {k: v for k, v in args.items() if k != "reason"}
        # STOP must reach long-running tools mid-execution: forward the
        # loop's cancel event only when the tool accepts it, otherwise
        # call exactly as before so old tools keep working.
        if cancel_flag is not None:
            try:
                import inspect as _inspect2
                _params2 = _inspect2.signature(mod.execute).parameters
                _ok2 = ("cancel_flag" in _params2) or any(
                    p.kind == _inspect2.Parameter.VAR_KEYWORD for p in _params2.values())
            except Exception:
                _ok2 = False
            if _ok2 and not (isinstance(call_args, dict) and "cancel_flag" in call_args):
                try:
                    call_args = {**(call_args if isinstance(call_args, dict) else {}),
                                 "cancel_flag": cancel_flag}
                except Exception:
                    pass
        # Reply parts: the loop owns the running part number (the model never
        # supplies it). Forwarded only when the tool accepts it.
        if part_index is not None:
            try:
                import inspect as _inspect3
                _params3 = _inspect3.signature(mod.execute).parameters
                _ok3 = ("part_index" in _params3) or any(
                    p.kind == _inspect3.Parameter.VAR_KEYWORD for p in _params3.values())
            except Exception:
                _ok3 = False
            if _ok3 and not (isinstance(call_args, dict) and "part_index" in call_args):
                try:
                    call_args = {**(call_args if isinstance(call_args, dict) else {}),
                                 "part_index": part_index}
                except Exception:
                    pass
        # execute with unpacked args - validates inside tool
        result = mod.execute(**call_args)  # type: ignore
        # Truncate large outputs
        try:
            s = json.dumps(result, ensure_ascii=False)
            if len(s) > 15000:
                # truncate result content field if present
                if isinstance(result, dict) and "content" in result:
                    result["content"] = str(result["content"])[:10000] + "... truncated"
                elif isinstance(result, dict) and "answer" in result:
                    result["answer"] = str(result["answer"])[:10000] + "... truncated"
        except Exception:
            pass
        return {"tool": n, "arguments": args, "output": result, "success": True}
    except TypeError as e:
        return {"tool": n, "arguments": args, "error": f"Invalid arguments for {n}: {e}", "success": False}
    except FileNotFoundError as e:
        return {"tool": n, "arguments": args, "error": str(e), "success": False}
    except PermissionError as e:
        return {"tool": n, "arguments": args, "error": str(e), "success": False}
    except ValueError as e:
        return {"tool": n, "arguments": args, "error": str(e), "success": False}
    except Exception as e:
        return {"tool": n, "arguments": args, "error": f"Tool {n} execution failed: {e}", "success": False}