"""
Context builder - assembles full professional context each loop iteration.
Single responsibility: build system prompt + memories + chat + env + todo + core tools into messages.
"""
from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "system.md"
CORE_IDENTITY_PATH = Path(__file__).resolve().parents[3] / "prompts" / "core_identity.md"
GENERAL_MISSION_PATH = Path(__file__).resolve().parents[3] / "prompts" / "general_mission.md"
POLICY_PATH = Path(__file__).resolve().parents[3] / "prompts" / "policy.md"
STORAGE_DB = Path(__file__).resolve().parents[3] / "storage" / "database"
# X5Agent/Data/database (new location, zip-backup). Read Data first, fallback legacy.
X5AGENT_DATA_DB = Path(__file__).resolve().parents[4] / "X5Agent" / "Data" / "database"


def _read_text_file(path: Path) -> str:
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            return text if text else ""
        return ""
    except Exception:
        return ""


def _read_system_prompt() -> str:
    # Layer order L1+L2+L3: core_identity + general_mission + policy.
    # Fallback to legacy system.md if new layered files are missing.
    l1 = _read_text_file(CORE_IDENTITY_PATH)
    l2 = _read_text_file(GENERAL_MISSION_PATH)
    l3 = _read_text_file(POLICY_PATH)
    if l1 or l2 or l3:
        parts = [p for p in [l1, l2, l3] if p]
        return "\n\n".join(parts).strip()
    try:
        if SYSTEM_PROMPT_PATH.exists():
            text = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
            return text if text else "You are a helpful AI agent."
        return "You are a helpful AI agent."
    except Exception:
        return "You are a helpful AI agent."


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data is not None else default
    except Exception:
        return default


def _load_db_json(filename: str, default: Any) -> Any:
    # Prefer X5Agent/Data/database (backup), fallback legacy storage/database.
    try:
        new_path = X5AGENT_DATA_DB / filename
        if new_path.exists():
            data = _load_json(new_path, None)
            if data is not None and data != {} and data != []:
                return data
    except Exception:
        pass
    return _load_json(STORAGE_DB / filename, default)


def _ensure_x5agent_best_effort() -> None:
    # Ensure sandbox dirs + one-time legacy->Data migration (idempotent, never overwrites Data).
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from core.paths import ensure_x5agent, migrate_legacy_to_data_once
        ensure_x5agent()
        migrate_legacy_to_data_once()
        return
    except Exception:
        pass
    try:
        ws = Path(__file__).resolve().parents[4] / "X5Agent" / "Workspace"
        data = Path(__file__).resolve().parents[4] / "X5Agent" / "Data"
        for d in [ws, data, data / "database", data / "vector",
                  data / "files" / "images", data / "files" / "audio",
                  data / "files" / "videos", data / "skills",
                  data / "configs" / "plugin_configs"]:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                continue
    except Exception:
        pass


def _workspace_path_str() -> str:
    try:
        ws = (Path(__file__).resolve().parents[4] / "X5Agent" / "Workspace").resolve()
        return str(ws)
    except Exception:
        return "X5Agent/Workspace"


def _browser_state_str() -> str:
    # Live browser state for the main context (read-only probe, never starts it).
    # Control/preview ownership comes straight from the library's session links
    # (no URL: stream links stay redacted from the model, the UI renders them).
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from X5Browser.service import live_links as _links
        links = _links()
    except Exception:
        return "unknown"
    if not isinstance(links, dict) or not links.get("running"):
        return "stopped"
    if links.get("owner") == "user":
        return ("running (CONTROL live active — user is driving; page refs may be "
                "stale, re-snapshot after resume)")
    return "running (preview live active — user watches only, agent keeps control)"


def _terminal_scope_str() -> str:
    # Current terminal scope for the context (limited default). Never raises.
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from core.tools.terminal.tool import _access_scope
        scope = _access_scope()
        return scope if scope == "full_access" else "limited_access"
    except Exception:
        return "limited_access"


def _env_block() -> str:
    # L4 system_info: time + OS + exact Workspace path + jail notice + terminal scope.
    _ensure_x5agent_best_effort()
    now = datetime.now(timezone.utc).astimezone()
    os_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    ws = _workspace_path_str()
    proj = str(STORAGE_DB.parent.parent.resolve())
    scope = _terminal_scope_str()
    if scope == "full_access":
        scope_line = ("- Terminal access: FULL access.\n"
                      "- Full access means you may run terminal commands freely outside the Workspace folder.\n"
                      "- File tools (workspace, read_file, write_file) stay jailed inside Workspace regardless.\n")
    else:
        scope_line = ("- Terminal access: LIMITED access (Workspace only).\n"
                      "- Full access would mean you may run terminal commands freely outside the Workspace folder; "
                      "it is currently OFF, so stay inside Workspace.\n"
                      "- FORBIDDEN to exit Workspace by any method (.., absolute paths, symlinks, shell tricks). "
                      "Any path outside Workspace is BLOCKED automatically.\n")
    return (
        f"[System Info - L4]\n"
        f"- DateTime: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} (ISO: {now.isoformat()})\n"
        f"- OS: {os_info}\n"
        f"- Python: {platform.python_version()}\n"
        f"- You are inside: {ws}\n"
        f"{scope_line}"
        f"- This Workspace folder is your device. Do EVERYTHING here (experiments, files, folders, terminal).\n"
        f"- System Core (read-only): {proj} - never write it. Data (X5Agent/Data) via memory/config tools only.\n"
        f"- Browser: {_browser_state_str()} (when running, a live preview bar is shown to the user automatically).\n"
        f"- If the user asks whether the browser runs, answer DIRECTLY from that state line only — "
        f"do not call any browser tool just to check (get_page_info is read-only and never boots it). "
        f"If they ask to open/start it, call open_browser (it boots on demand). "
        f"Never call open_browser on your own: when a browser tool fails with 'browser stopped', "
        f"tell the user it is stopped instead of opening it.\n"
    )


def _workspace_block() -> str:
    # Workspace OUTER level only: every file carries its full metadata shape
    # (id + full path + type + size); folders show item counts. Never walks
    # inside: enter any folder with the workspace tool (via tool_search) and
    # every file there shows the SAME shape with its own path.
    # Bounded: first 100 entries, counts capped.
    try:
        ws = (Path(__file__).resolve().parents[4] / "X5Agent" / "Workspace").resolve()
    except Exception:
        return ""
    try:
        if not ws.is_dir():
            return "[Workspace - your work device: empty]"
        children = sorted(
            (p for p in ws.iterdir() if p.is_dir() or p.is_file()),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except Exception:
        return ""
    if not children:
        return "[Workspace - your work device: empty]"
    lines = []
    for p in children[:100]:
        try:
            if p.is_dir():
                n = 0
                trunc = False
                try:
                    for _ in p.iterdir():
                        n += 1
                        if n >= 1001:
                            trunc = True
                            break
                except Exception:
                    n = 0
                lines.append(f"  folder: {p.name}/ ({n}{'+' if trunc else ''} items)")
            else:
                try:
                    size = p.stat().st_size
                except Exception:
                    size = 0
                ext = p.suffix.lower().lstrip(".") if p.suffix else "no-ext"
                try:
                    full = str(p.resolve()).replace("\\", "/")
                except Exception:
                    full = p.name
                lines.append(f"  file: {p.name} (id: \"{p.name}\", path: \"{full}\", type: {ext}, size: {size} bytes)")
        except Exception:
            continue
    # Virtual browser-downloads folder (read-only view, same rule as the
    # workspace tool: hidden when a real folder with the same name exists).
    try:
        if not (ws / "downloads").exists():
            from x5browser.config import LIBRARY_DOWNLOADS_DIR as _lib_dl
            _lib = Path(str(_lib_dl))
            if _lib.is_dir():
                _n = 0
                try:
                    for _ in _lib.iterdir():
                        _n += 1
                        if _n >= 1001:
                            break
                except Exception:
                    _n = 0
                _dl_line = (f"  folder: downloads/ ({_n} items, type: folder, browser downloads, read-only)")
                _fi = next((i for i, _l in enumerate(lines)
                            if _l.startswith("  file:")), len(lines))
                lines.insert(_fi, _dl_line)
    except Exception:
        pass
    more = "" if len(children) <= 100 else f"\n  ... +{len(children) - 100} more"
    return ("[Workspace map - outer level only: id + path + type + size - L4]\n"
            + "\n".join(lines) + more
            + "\n(Use these exact file path links as-is in any code or HTML: images, fonts, any file. "
              "An @<file-id> mention in the user message refers to the map entry with that id. "
              "You may copy any Workspace file into the folder you work in so everything lives in one "
              "folder, then reference it by its file name. "
              "In your reply text, mention a Workspace file ONLY as [label](workspace:folder/file.ext) "
              "with the exact Workspace-relative path — this renders as a blue underlined link. "
              "Never write bare folder/file paths or absolute paths as reply-text links; plain text stays plain.)")


def _embedding_memory_block() -> str:
    # Conditional L5 line: hidden when no chunks stored, otherwise one fixed
    # English sentence with the stored-chunk count only (no content dumped).
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from plugins.core_plugins.embedding_memory.tool import count_stored
        n = count_stored()
    except Exception:
        n = 0
    if not isinstance(n, int) or n <= 0:
        return ""
    return ("[Embedding Memory - L5]\n"
            f"Embedding memory (embedding_memory tool) holds {n} stored "
            "chunks you may search or store via that tool.")


def _memory_text(kind: str) -> str:
    # Read one internal memory as bullet text via the single owner
    # (core.memory.service). Never raises.
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from core.memory import service
        return service.get_text(kind)
    except Exception:
        return ""


def _agent_personality_block() -> str:
    # The agent personality memory travels in the SYSTEM prompt (its identity),
    # right after L1-L3, because the user wants the agent to become an exact
    # copy of him. Hidden (empty) until the memory has content.
    txt = _memory_text("agent")
    if not txt.strip():
        return ""
    return ("[Agent personality memory - your identity: think, speak and decide exactly "
            "the way the user does.]\n" + txt)


def _memories_block() -> str:
    # L5: user profile (first) + facts (second) + the embedding count line.
    # The agent personality memory is NOT here: it lives in the system prompt.
    # The agent-owned notes memory is never sent automatically (tool only).
    parts: list[str] = []
    user_txt = _memory_text("user")
    if user_txt.strip():
        parts.append("[User memory - who the user is - L5]\n" + user_txt)
    facts_txt = _memory_text("facts")
    if facts_txt.strip():
        parts.append("[Facts memory - confirmed facts about the user - L5]\n" + facts_txt)
    emb_block = _embedding_memory_block()
    if emb_block:
        parts.append(emb_block)
    if not parts:
        return ""
    return "\n\n".join(parts)


def _chat_block(limit: int = 30, exclude_trailing_stopped: bool = False) -> str:
    # L6a: last N messages - explicit layer, never skipped except isolated mode.
    # Default 30; edit-resend passes 20 (the edited message + the 20 before it).
    # exclude_trailing_stopped (seamless resume only): the model continues the
    # task as if it never stopped, so trailing stopped markers are dropped
    # from the CONTEXT ONLY — persisted history keeps them (UI + future turns
    # that are NOT resumes still see that the task was stopped). Never touches
    # the file (works on the loaded list).
    # NOTE: STOPPED_NOTE_TEXT must stay identical to _STOPPED_NOTE in
    # core/agent/loop/engine.py (legacy markers have no stopped flag).
    STOPPED_NOTE_TEXT = ("The user stopped this task before it finished (STOP). "
                         "Do not continue it unless the user asks again.")
    for cand in [X5AGENT_DATA_DB / "conversation.json", STORAGE_DB / "conversation.json", STORAGE_DB / "chat.json", STORAGE_DB / "conversation_history.json"]:
        data = _load_json(cand, None)
        if isinstance(data, list) and data:
            if exclude_trailing_stopped:
                try:
                    while data and isinstance(data[-1], dict) and (
                            data[-1].get("stopped") is True
                            or str(data[-1].get("content") or "").strip() == STOPPED_NOTE_TEXT):
                        data.pop()
                except Exception:
                    pass
                if not data:
                    return ""
            last = data[-limit:]
            lines = []
            for m in last:
                role = m.get("role", "user")
                content = str(m.get("content",""))[:400]
                lines.append(f"  {role}: {content}")
            return f"[Last {len(last)} messages - L6]\n" + "\n".join(lines)
    return ""


def _documents_block() -> str:
    # Documents metadata: Workspace files MarkItDown can convert + index state.
    # Bounded (first 20), read-only, never embeds — indexing happens lazily
    # inside document_search. Also purges orphan embeddings best-effort so a
    # file deleted from Workspace never leaves stale vectors behind.
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from core.documents.store import list_documents
        docs = list_documents()
    except Exception:
        return ""
    if not docs:
        return ""
    try:
        import sys as _sys2
        _proj2 = Path(__file__).resolve().parents[3]
        if str(_proj2) not in _sys2.path:
            _sys2.path.insert(0, str(_proj2))
        from core.paths import workspace_root as _ws2
        _wsroot = _ws2().resolve()
    except Exception:
        _wsroot = None
    lines = []
    for d in docs[:20]:
        try:
            state = f"{d.get('chunk_count', 0)} chunks" if d.get("indexed") else "not indexed yet"
            _fid = str(d.get("file_id") or "")
            _ext = Path(_fid).suffix.lower().lstrip(".") or "no-ext"
            try:
                _full = str((_wsroot / _fid).resolve()).replace("\\", "/") if _wsroot is not None else _fid
            except Exception:
                _full = _fid
            lines.append(f"  - {_fid} [id: \"{_fid}\", path: \"{_full}\", type: {_ext}, size: {d.get('size', 0)} bytes] — {state}")
        except Exception:
            continue
    if not lines:
        return ""
    more = "" if len(docs) <= 20 else f"\n  ... +{len(docs) - 20} more"
    return ("[Documents metadata - find document_search via tool_search, then search - L4]\n"
            "  Supported: PDF, Word, Excel, PowerPoint, csv/txt/md/html/json/xml.\n"
            "  Search one file with document_search(file_id, query, top_k).\n"
            + "\n".join(lines) + more)


def _studios_hint() -> str:
    # PDF Studios are NEVER auto-listed: one fixed pointer line only. The
    # model calls pdf_studio action=list (via tool_search) to see every
    # studio with its state, or action=get for one studio's full details.
    # No studio data travels here — no IDs, no titles, no counts.
    return ("[PDF Studios - L4: not auto-listed — call pdf_studio action=list "
            "(via tool_search) to see all studios with state, action=get "
            "for one studio's details; reuse an empty (0 designed) studio "
            "via action=open before creating]")


def _todo_block() -> str:
    todos = _load_db_json("todos.json", [])
    if not isinstance(todos, list) or not todos:
        return ""
    # bounded: show last 10
    lines = []
    for t in todos[-10:]:
        task = str(t.get("task",""))[:120]
        steps = t.get("steps")
        status = t.get("status","")
        tid = t.get("id","")
        lines.append(f"  - [{status}] {tid}: {task}")
        if isinstance(steps, list) and steps:
            for i, s in enumerate(steps, 1):
                lines.append(f"      {i}. {str(s)[:80]}")
    return "[Todo List - current plan - L6]\n" + "\n".join(lines)


def _triggers_block() -> str:
    triggers = _load_db_json("triggers.json", [])
    if not isinstance(triggers, list) or not triggers:
        return ""
    # Only active triggers
    active = [t for t in triggers if t.get("status") in (None, "active")]
    if not active:
        return ""
    # Only active triggers, bounded: last 10 shown with total count so the
    # agent knows each trigger + its awaited action and can manage them all
    # via trigger_search (get/update/delete by trigger_id).
    lines = []
    shown = active[-10:]
    for t in shown:
        tid = t.get("trigger_id","")
        tname = t.get("trigger_name","")
        task = str(t.get("task",""))[:80]
        skill = t.get("skill_ref","")
        cfg = t.get("config") or {}
        cfg_str = json.dumps(cfg, ensure_ascii=False)[:100] if isinstance(cfg, dict) and cfg else ""
        lines.append(f"  - {tid} [{tname}] task: {task} skill: {skill} config: {cfg_str}")
    head = f"[Active Triggers ({len(active)} armed, background, one-time, die after firing)]"
    if len(active) > len(shown):
        head += f"\n  (showing last {len(shown)}; use trigger_search list for all)"
    return head + "\n" + "\n".join(lines) + "\n  (Use trigger_search to manage: search, create, list, get, update, delete)"


def openai_tool_from_spec(name: str, description: str, inputs: dict) -> dict:
    """Convert a spec.json-style (name, description, inputs) into an OpenAI function tool."""
    props: dict = {}
    required: list[str] = []
    for k, v in (inputs or {}).items():
        if not isinstance(v, dict):
            continue
        typ = v.get("type", "string")        # map spec types to JSON schema types
        json_type = "string"
        if typ == "integer":
            json_type = "integer"
        elif typ == "boolean":
            json_type = "boolean"
        elif typ == "array":
            json_type = "array"
        elif typ == "number":
            json_type = "number"
        prop: dict[str, Any] = {"type": json_type, "description": v.get("description", "")}
        if typ == "array" and "items" in v:
            prop["items"] = v["items"]
        if "enum" in v:
            prop["enum"] = v["enum"]
        if "default" in v:
            prop["default"] = v["default"]
        props[k] = prop
        if v.get("required"):
            required.append(k)
    # Optional Reason node on EVERY tool: the model may explain why it calls
    # this tool (shown when the user expands the step). Never required; the
    # engine strips it before execution so tools never see it.
    if "reason" not in props:
        props["reason"] = {
            "type": "string",
            "description": "Why you call this tool (max 15 words, English). Shown in the UI when the user expands this step.",
        }
    parameters: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description or "", "parameters": parameters},
    }


def _core_tools_block() -> list[dict]:
    """Return core tools OpenAI-format specs.

    Core = everything in core/tools EXCEPT workspace + document_search
    (discoverable behind tool_search) + skill_create (lives in plugins but
    classified core) + the 6 browser core tools
    (open_browser/navigate/page_snapshot/click/fill/human_control).
    When no real user skill exists, skills_search/skills_view are dropped and
    skill_create alone is offered.
    """
    tools: list[dict] = []
    core_root = Path(__file__).resolve().parents[2] / "tools"
    # workspace + document_search are discoverable (behind tool_search);
    # the agent already sees the Workspace map, so no in-context doc tool.
    _EXCLUDED_FROM_CORE = {"workspace", "document_search"}
    try:
        import sys as _sys2
        _proj2 = Path(__file__).resolve().parents[3]
        if str(_proj2) not in _sys2.path:
            _sys2.path.insert(0, str(_proj2))
        from core.tools.registry import has_user_skills as _has_skills
        _hide_skill_search = not _has_skills()
    except Exception:
        _hide_skill_search = False
    if _hide_skill_search:
        _EXCLUDED_FROM_CORE = _EXCLUDED_FROM_CORE | {"skills_search", "skills_view"}
    for d in sorted(core_root.iterdir()):
        if not d.is_dir() or d.name in _EXCLUDED_FROM_CORE:
            continue
        spec_path = d / "spec.json"
        if not spec_path.exists():
            continue
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            tools.append(openai_tool_from_spec(spec.get("name"), spec.get("description", ""), spec.get("inputs", {})))
        except Exception:
            continue
    # skill_create is CORE (folder stays in plugins, no move per minimal-change)
    try:
        _skill_spec_path = core_root.parent.parent / "plugins" / "core_plugins" / "skill_create" / "spec.json"
        if _skill_spec_path.exists():
            spec = json.loads(_skill_spec_path.read_text(encoding="utf-8"))
            if not any(t.get("function", {}).get("name") == spec.get("name") for t in tools):
                tools.append(openai_tool_from_spec(spec.get("name"), spec.get("description", ""), spec.get("inputs", {})))
    except Exception:
        pass
    # Browser core tools (core=True in X5Browser/catalog.py)
    try:
        import sys as _sys
        _proj = Path(__file__).resolve().parents[3]
        if str(_proj) not in _sys.path:
            _sys.path.insert(0, str(_proj))
        from X5Browser.catalog import SPECS as _BROWSER_SPECS
        for _spec in _BROWSER_SPECS:
            try:
                if not isinstance(_spec, dict) or not _spec.get("core"):
                    continue
                if not isinstance(_spec.get("name"), str) or not isinstance(_spec.get("description"), str):
                    continue
                if any(t.get("function", {}).get("name") == _spec.get("name") for t in tools):
                    continue
                tools.append(openai_tool_from_spec(_spec.get("name"), _spec.get("description", ""), _spec.get("inputs", {})))
            except Exception:
                continue
    except Exception:
        pass
    return tools


def _studio_env_block() -> str:
    """Tiny env for studio-isolated turns (no maps, no memories, no Workspace).

    The studio is self-contained: every file the turn touches lives in the
    studio's own assets. Never raises.
    """
    try:
        from datetime import datetime as _dt
        now = _dt.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        now = "unknown time"
    return ("[Studio Env]\n"
            f"- Time: {now}\n"
            "- Every file you use lives in THIS studio's assets (exact src "
            "paths are listed under Studio assets — use them verbatim).\n"
            "- Photos: image_search with this studio id (pictures land in the "
            "studio assets with src paths). Fonts/links: http_request.")


def _studio_vision_available() -> bool:
    """True when a vision model is configured (vision_analyze then shows).

    The tool stays hidden otherwise — never advertised without a model
    behind it. Never raises (file read only, no network).
    """
    try:
        import sys as _sys2
        _proj2 = Path(__file__).resolve().parents[3]
        if str(_proj2) not in _sys2.path:
            _sys2.path.insert(0, str(_proj2))
        from core.ai_models.store import has_model as _has_model
        return bool(_has_model("vision"))
    except Exception:
        return False


def _studio_tool_names() -> list[str]:
    """Studio tools in-context: the 4 base + vision_analyze when configured."""
    names = ["studio_page", "studio_read", "image_search", "http_request"]
    try:
        if _studio_vision_available():
            names.append("vision_analyze")
    except Exception:
        pass
    return names


def _studio_core_tools() -> list[dict]:
    """Studio tools in-context (no tool_search in studio).

    image_search rides with its studio face (no Workspace words), same as
    the engine pre-seed — one override, two readers.
    """
    out: list[dict] = []
    try:
        base = Path(__file__).resolve().parents[3] / "plugins" / "core_plugins"
        for tname in _studio_tool_names():
            try:
                if tname == "image_search":
                    import sys as _sys9
                    _proj9 = Path(__file__).resolve().parents[3]
                    if str(_proj9) not in _sys9.path:
                        _sys9.path.insert(0, str(_proj9))
                    from plugins.core_plugins.image_search.tool import studio_spec
                    spec = studio_spec()
                else:
                    spec = json.loads((base / tname / "spec.json").read_text(encoding="utf-8"))
                if not isinstance(spec.get("name"), str) or not isinstance(spec.get("description"), str):
                    continue
                out.append(openai_tool_from_spec(spec.get("name"), spec.get("description", ""),
                                                spec.get("inputs", {})))
            except Exception:
                continue
    except Exception:
        pass
    return out


def build_context(user_input: str, include_todo: bool = True, isolated: bool = False, todo_snapshot: dict | None = None, session_history: str | None = None, chat_limit: int = 30, resume_stopped: bool = False, studio_mode: bool = False) -> tuple[str, str, list[dict]]:
    """
    8-layer context order (fixed):
      L1 core_identity.md + L2 general_mission.md + L3 policy.md -> system role
      L4 system_info (time + Workspace path + jail) -> user content
      L5 memories (user profile + facts) -> user content (agent personality rides in the system prompt)
      L6 chat last-N + todo + triggers (explicit, never merged into L5) -> user content
      L7 available core tools list -> user content
      L8 current user input (ALWAYS last) -> user content
    Returns (system_prompt, user_context_content, core_tools_openai)
    isolated=True for scheduled large tasks: skips last-30 chat history and uses snapshot.
    session_history: this task's own branch history (holds the skill among
      its last 30) — replaces L6 when isolated, extends it otherwise, so a
      task follow-up chat answers like a separate normal chat with the same
      full context (each scheduled job keeps its session, main chat untouched).
    chat_limit: L6 history depth (default 30; edit-resend uses 20).
    resume_stopped: seamless Continue — the model resumes the stopped task as
      if it never stopped: the L8 current-input block is omitted (there is no
      new message) and trailing stopped markers are excluded from L6 (context
      only; persisted history keeps them, so a LATER non-resume turn still
      sees the task was stopped).
    """
    if not isinstance(user_input, str):
        raise TypeError("user_input must be string")
    # L1+L2+L3
    system_prompt = _read_system_prompt()
    if studio_mode:
        # Studio-isolated turn: sentence + state only. No main-session
        # baggage (workspace map, docs, memories, todos, triggers) and
        # exactly 4 tools in-context (no tool_search in studio).
        # Old page codes are NEVER sent — the model reads via the tools.
        # — f-string replacement fields must stay on ONE line (multi-line
        # expressions inside an f-string need Python 3.12; the floor is 3.10).
        _vision_core = (" | vision_analyze (inspect image content: pass studio assets as "
                        "image_path=studio:<id>/images/<name>)"
                        if _studio_vision_available() else "")
        _s_ctx = "\n\n".join([
            _studio_env_block(),
            "[Memories L5: skipped — studio turns are stateless]",
            "[Chat L6: studio-isolated — the main session never enters studio turns. "
            "All parallel per-slide turns share this one branch session.]",
            "[Available CORE tools L7 (only these exist here): "
            "studio_page (approve the tasked slide: read / deliver / patch / test its file, "
            "fetch to save a web font into assets) "
            "| studio_read (read-only view of any other slide) "
            "| image_search (find photos: this studio id + query words -> pictures land in the studio assets with src paths) "
            "| http_request (fonts/links only, never photos)"
            f"{_vision_core}]",
            "[Bridge] Based on the studio task below, execute it.\n\n"
            f"[Current User Input - L8]\n{user_input.strip()}"])
        return system_prompt, _s_ctx, _studio_core_tools()
    # Agent personality memory -> SYSTEM prompt (identity), right after L1-L3.
    personality = _agent_personality_block()
    if personality:
        system_prompt = system_prompt + "\n\n" + personality
    # L4
    env = _env_block()
    ws_block = _workspace_block()
    docs_block = _documents_block()
    studios_hint = _studios_hint()
    # L5
    mem = _memories_block()
    # L6: chat + todo + triggers (explicit separate layer)
    if isolated:
        chat = ""
        if todo_snapshot and isinstance(todo_snapshot, dict):
            steps = todo_snapshot.get("steps") or []
            task = str(todo_snapshot.get("task",""))[:200]
            tid = todo_snapshot.get("id","")
            snap = f"[Scheduled Todo Snapshot - isolated, no chat history - L6]\n- [{todo_snapshot.get('status','planned')}] {tid}: {task}\n" + "\n".join([f"  {i+1}. {s}" for i, s in enumerate(steps)])
            todo = snap
        else:
            todo = _todo_block() if include_todo else ""
    else:
        chat = _chat_block(limit=chat_limit if isinstance(chat_limit, int) and 1 <= chat_limit <= 100 else 30,
                           exclude_trailing_stopped=resume_stopped)
        todo = _todo_block() if include_todo else ""
    parts = []
    parts.append(env)
    if ws_block:
        parts.append(ws_block)
    if docs_block:
        parts.append(docs_block)
    if studios_hint:
        parts.append(studios_hint)
    if mem:
        parts.append(mem)
    else:
        parts.append("[Memories L5: empty]")
    if isolated:
        if isinstance(session_history, str) and session_history.strip():
            parts.append(session_history.strip()[:16000])
        else:
            parts.append("[Chat L6: excluded for isolated scheduled execution - large task reliability]")
    else:
        if chat:
            parts.append(chat)
        else:
            parts.append("[Chat L6: no prior messages]")
        # A branch session (scheduled task follow-up chat) keeps its own
        # history — including the skill — as an extra layer on top of the
        # same full context, so the run answers like a separate normal chat
        # instead of re-executing the task.
        if isinstance(session_history, str) and session_history.strip():
            parts.append(session_history.strip()[:16000])
    if todo:
        parts.append(todo)
    # Active triggers metadata + skill name (so AI can manage via trigger_search)
    triggers_block = _triggers_block()
    if triggers_block:
        parts.append(triggers_block)
    # L7 (dynamic: mirrors the real core_tools list, so conditional skills stay honest)
    core_tools = _core_tools_block()
    try:
        _core_names = sorted({str(t.get("function", {}).get("name") or "") for t in core_tools} - {""})
    except Exception:
        _core_names = []
    if _core_names:
        parts.append("[Available CORE tools L7: " + ", ".join(_core_names)
                     + " | Discoverable behind tool_search: workspace + all other browser/plugin tools]")
    else:
        parts.append("[Available CORE tools L7: tool_search, terminal]")
    # L8 ALWAYS last (with bridge: re-anchor attention after the long L4-L7 context).
    # Omitted for a seamless resume: there is no new message, and the agent
    # must never see an internal continue instruction.
    if not resume_stopped:
        parts.append(f"[Bridge] Based on all layers above, execute this task.\n\n[Current User Input - L8]\n{user_input.strip()}")
    if isolated:
        parts.append("[Mode: ISOLATED SCHEDULED EXECUTION - execute exactly as in snapshot, do not ask for clarification, complete the workflow]")
    user_content = "\n\n".join(parts)
    return system_prompt, user_content, core_tools


def get_core_tools_openai() -> list[dict]:
    return _core_tools_block()