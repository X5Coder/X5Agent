"""
Tool registry - single source of truth for all tools.
Provides listing and validation, used by gateway and tests.
"""
from __future__ import annotations

from pathlib import Path
import json

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

CORE_TOOLS = ["tool_search", "question", "vector_chat_search", "todo_list", "scheduler", "skills_search", "skills_view", "skill_create", "terminal", "trigger_search", "wait", "mcp_servers", "open_browser", "navigate", "page_snapshot", "click", "fill", "human_control"]
DISCOVERABLE_PLUGINS = ["read_file", "write_file", "search_files", "vision_analyze", "document_search"]

# Core plugins: always on, hidden from the UI, no switches.
# Lives in plugins/core_plugins/ + every X5Browser tool (both behind tool_search).
_CORE_PLUGINS_DIR = "core_plugins"

# Skills reserved containers (owned by scheduler/trigger engines, never user skills).
_SKILLS_RESERVED = {"scheduled_skills", "trigger_skills"}

# Skill tools that only make sense when at least one user skill exists.
# When the skills home holds no real skill, the context carries skill_create
# only (search/view are dropped) so the model is never offered dead tools.
_SKILL_SEARCH_TOOLS = {"skills_search", "skills_view"}


def has_user_skills() -> bool:
    """True when project/skills/ holds at least one real user skill.

    Empty placeholders (only .gitkeep / no content file) and the reserved
    engine containers never count. Never raises.
    """
    try:
        skills_root = _PROJECT_ROOT / "skills"
        if not skills_root.is_dir():
            return False
        for child in skills_root.iterdir():
            if not child.is_dir() or child.name in _SKILLS_RESERVED:
                continue
            for cand in ("skill.json", "SKILL.md", "README.md", "description.txt", "instructions.md"):
                if (child / cand).exists():
                    return True
        return False
    except Exception:
        return False


def _core_plugin_dir_ids() -> set[str]:
    """Tool ids owned by plugins/core_plugins/. Never raises."""
    try:
        root = _PROJECT_ROOT / "plugins" / _CORE_PLUGINS_DIR
        if not root.is_dir():
            return set()
        return {p.name for p in root.iterdir()
                if p.is_dir() and (p / "spec.json").exists()}
    except Exception:
        return set()


def _browser_ids() -> set[str]:
    try:
        from X5Browser.catalog import ALL_TOOLS
        return set(ALL_TOOLS)
    except Exception:
        return set()


def is_core_plugin(plugin_id: str) -> bool:
    """True for always-on tools: core_plugins/ ids + all X5Browser tools."""
    if not isinstance(plugin_id, str) or not plugin_id:
        return False
    if plugin_id in _browser_ids():
        return True
    return plugin_id in _core_plugin_dir_ids()


def _browser_specs(core_only: bool) -> list[dict]:
    """Specs owned by project/X5Browser/catalog.py. Never raises."""
    try:
        from X5Browser.catalog import SPECS
        out = []
        for spec in SPECS:
            if not isinstance(spec, dict):
                continue
            is_core = bool(spec.get("core"))
            if is_core != core_only:
                continue
            if isinstance(spec.get("name"), str) and isinstance(spec.get("description"), str):
                out.append({"name": spec["name"], "description": spec["description"],
                            "inputs": spec.get("inputs", {})})
        return out
    except Exception:
        return []


def list_core_specs() -> list[dict]:
    specs = []
    _hide_skill_search = not has_user_skills()
    for name in CORE_TOOLS:
        # No real skills -> offer skill_create only, never dead search/view.
        if _hide_skill_search and name in _SKILL_SEARCH_TOOLS:
            continue
        # core/tools first, then plugins fallback (skill_create), then browser catalog
        candidates = [
            _PROJECT_ROOT / "core" / "tools" / name / "spec.json",
            _PROJECT_ROOT / "plugins" / "core_plugins" / name / "spec.json",
        ]
        loaded = False
        for p in candidates:
            if p.exists():
                try:
                    specs.append(json.loads(p.read_text(encoding="utf-8")))
                    loaded = True
                    break
                except Exception:
                    continue
        if loaded:
            continue
        # Browser core tools (open_browser/navigate/page_snapshot/click/fill/human_control)
        try:
            from X5Browser.catalog import get_spec as _browser_spec
            bspec = _browser_spec(name)
            if isinstance(bspec, dict) and bspec.get("core"):
                specs.append({"name": bspec["name"], "description": bspec["description"],
                              "inputs": bspec.get("inputs", {})})
        except Exception:
            continue
    return specs


def list_discoverable_specs() -> list[dict]:
    specs = []
    plugins_root = _PROJECT_ROOT / "plugins"
    if plugins_root.exists():
        for cat in plugins_root.iterdir():
            if not cat.is_dir() or cat.name == "tool-discovery":
                continue
            for tool_dir in cat.iterdir():
                # skill_create is CORE now (in-context), never discoverable
                if tool_dir.name == "skill_create":
                    continue
                spec = tool_dir / "spec.json"
                if spec.exists():
                    try:
                        specs.append(json.loads(spec.read_text(encoding="utf-8")))
                    except Exception:
                        continue
    # document_search lives in core/tools on disk but is classified
    # discoverable (behind tool_search); the folder stays for import compat.
    for _disc_name in ("document_search",):
        try:
            _spec_path = _PROJECT_ROOT / "core" / "tools" / _disc_name / "spec.json"
            if _spec_path.exists():
                specs.append(json.loads(_spec_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    specs.sort(key=lambda x: x.get("name", ""))
    # Browser discoverable tools live in project/X5Browser (single versioned folder).
    try:
        seen = {s.get("name") for s in specs}
        for spec in _browser_specs(core_only=False):
            if spec.get("name") not in seen:
                specs.append(spec)
                seen.add(spec.get("name"))
        specs.sort(key=lambda x: x.get("name", ""))
    except Exception:
        pass
    return specs


# ---------- plugin / model enablement state ----------
# Single owner of runtime on/off state. Stored in storage/database/plugin_state.json:
# {"plugins": {id: {"enabled": bool, "permissions": {name: bool}}}, "models": {id: {"enabled": bool}}}
# Effective rules: plugin off if stored-off OR its model is effectively off.
# A model counts as off if stored-off OR all its linked plugins are stored-off.

def _state_path() -> Path:
    # Unified: writes ALWAYS to X5Agent/Data (zip backup). Fallback legacy only if resolver missing.
    try:
        from core.paths import data_db_file
        return data_db_file("plugin_state.json")
    except Exception:
        return _PROJECT_ROOT / "storage" / "database" / "plugin_state.json"


_STATE_PATH = _state_path()
_STATE_LEGACY = _PROJECT_ROOT / "storage" / "database" / "plugin_state.json"


def _load_state() -> dict:
    for cand in (_STATE_PATH, _STATE_LEGACY):
        try:
            if cand.exists():
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if not isinstance(data.get("plugins"), dict):
                        data["plugins"] = {}
                    if not isinstance(data.get("models"), dict):
                        data["models"] = {}
                    return data
        except Exception:
            continue
    return {"plugins": {}, "models": {}}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STATE_PATH)


def get_plugin_state() -> dict:
    return _load_state()


def _model_index() -> list[dict]:
    # No local models anymore: the only built-in model is the hidden embedding
    # model (core/embedding), everything else connects via AI Models templates.
    return []


def _plugins_for_model(model_id: str) -> list[str]:
    out: list[str] = []
    for m in _model_index():
        if m.get("id") == model_id:
            plugs = m.get("plugins") or []
            return [p for p in plugs if isinstance(p, str)]
    return out


def _model_for_plugin(plugin_id: str) -> str | None:
    for m in _model_index():
        plugs = m.get("plugins") or []
        if plugin_id in plugs:
            return str(m.get("id"))
    return None


def model_for_plugin(plugin_id: str) -> str | None:
    """Public: which local model serves this plugin (None if standalone)."""
    try:
        return _model_for_plugin(plugin_id)
    except Exception:
        return None


def plugin_actions(plugin_id: str) -> list[str]:
    """Action names from the tool's spec action-enum (empty = single-permission tool)."""
    try:
        for spec in list_discoverable_specs():
            if spec.get("name") == plugin_id:
                inputs = spec.get("inputs", {})
                act = inputs.get("action", {}) if isinstance(inputs, dict) else {}
                enum = act.get("enum") if isinstance(act, dict) else None
                if isinstance(enum, list):
                    return [str(x) for x in enum if isinstance(x, str)]
    except Exception:
        pass
    return []


def known_plugin_ids() -> list[str]:
    # Core plugins are hidden from the UI and untoggleable — excluded here so
    # set_plugin_enabled/set_permission_enabled reject them as unknown.
    return [s.get("name", "") for s in list_discoverable_specs()
            if s.get("name") and not is_core_plugin(s.get("name", ""))]


def known_model_ids() -> list[str]:
    return [str(m.get("id")) for m in _model_index() if m.get("id")]


def set_plugin_enabled(plugin_id: str, enabled: bool) -> dict:
    if plugin_id not in known_plugin_ids():
        raise ValueError(f"Unknown plugin: {plugin_id}")
    state = _load_state()
    entry = state["plugins"].get(plugin_id)
    if not isinstance(entry, dict):
        entry = {}
        state["plugins"][plugin_id] = entry
    entry["enabled"] = bool(enabled)
    _save_state(state)
    return state


def set_permission_enabled(plugin_id: str, perm_name: str, enabled: bool) -> dict:
    if plugin_id not in known_plugin_ids():
        raise ValueError(f"Unknown plugin: {plugin_id}")
    state = _load_state()
    entry = state["plugins"].get(plugin_id)
    if not isinstance(entry, dict):
        entry = {}
        state["plugins"][plugin_id] = entry
    perms = entry.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        entry["permissions"] = perms
    perms[str(perm_name)] = bool(enabled)
    _save_state(state)
    return state


def set_model_enabled(model_id: str, enabled: bool) -> dict:
    if model_id not in known_model_ids():
        raise ValueError(f"Unknown model: {model_id}")
    state = _load_state()
    state["models"][model_id] = {"enabled": bool(enabled)}
    _save_state(state)
    return state


def is_model_effective(model_id: str) -> bool:
    state = _load_state()
    models = state.get("models", {})
    if isinstance(models.get(model_id), dict) and models[model_id].get("enabled") is False:
        return False
    plugs = _plugins_for_model(model_id)
    if plugs:
        stored_off = [p for p in plugs if state.get("plugins", {}).get(p, {}).get("enabled") is False]
        if len(stored_off) == len(plugs):
            return False
    return True


def is_plugin_effective(plugin_id: str) -> bool:
    # Core plugins are always effective, ignoring any stale stored/model state.
    try:
        if is_core_plugin(plugin_id):
            return True
    except Exception:
        pass
    state = _load_state()
    entry = state.get("plugins", {}).get(plugin_id, {})
    if isinstance(entry, dict) and entry.get("enabled") is False:
        return False
    model_id = _model_for_plugin(plugin_id)
    if model_id:
        if not is_model_effective(model_id):
            return False
        # Unconfigured model (files missing) hides its tools as if absent
        try:
            for m in _model_index():
                if m.get("id") == model_id and m.get("status") not in ("ready", "found"):
                    return False
        except Exception:
            pass
    return True


def is_permission_effective(plugin_id: str, perm_name: str, required: bool) -> bool:
    if required:
        return is_plugin_effective(plugin_id)
    if not is_plugin_effective(plugin_id):
        return False
    state = _load_state()
    entry = state.get("plugins", {}).get(plugin_id, {})
    perms = entry.get("permissions", {}) if isinstance(entry, dict) else {}
    return perms.get(perm_name, True) is not False
