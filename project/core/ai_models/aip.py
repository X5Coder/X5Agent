"""
AIP/1 - AI Provider Interpreter (project-defined spec, NOT an industry standard).

Single interpreter between the user/Black Box and any provider.
The Black Box speaks canonical ONLY. The parser JSON speaks provider.
Adding a provider = connection settings + one declarative parser JSON.
No Black Box code changes, no arbitrary code in parser JSON.

Stdlib only. Secrets never logged. Retries bounded (max 3, retryable only).
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from typing import Any

LANGUAGE = "AIP/1"
AIP_VERSION = 1
MAX_RETRIES = 3
MAX_STRING = 200_000
MAX_ARRAY = 500
MAX_DEPTH = 20
MAX_NODES = 500
MAX_BODY_BYTES = 5 * 1024 * 1024

ALLOWED_OPS = (
    "literal", "ref", "object", "array", "map", "if", "coalesce",
    "omit_if_null", "rename", "select", "parse_json",
    "to_string", "to_number", "to_boolean", "merge", "encode",
    "transform_content", "collect_media",
)

# Canonical typed content parts (spec: text/image/audio/video + future kinds).
PART_TYPES = ("text", "image", "audio", "video")
MODALITIES = ("text", "image", "audio", "video")
MAX_PART_DATA = 25 * 1024 * 1024

ROLES = ("system", "developer", "user", "assistant", "tool")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
BLOCKED_HOSTS = ("169.254.169.254", "metadata.google.internal", "instance-data")


# --------------------------------------------------------------------------
# Mandatory canonical refs (transport-level: nothing the engine sends may be
# dropped by a parser). The loop assembles L1-L8 (system prompt, env,
# memories, chat, todo, skills/MCP tool specs) into `messages` + `tools`,
# so a parser that forwards these two refs forwards EVERYTHING.
# A parser that omits one fails loudly with the variable name (red error
# in the UI) instead of silently losing data.
# --------------------------------------------------------------------------

TOOLS_REF = "input.tools"
MESSAGES_REF = "input.messages"
MODEL_REF = "model.model_name"

# Per-kind request refs that MUST appear somewhere inside request.*.
# (vision carries its image inside messages parts, video carries its video
# inside messages parts; image/tts build their
# prompt from messages via transform_content; stt collects audio from
# messages via collect_media — all still need the messages ref.)
# NOTE: model.model_name is deliberately NOT enforced: many local/private
# services take no model field, and the store allows an empty Field 1.
# Parsers SHOULD reference it whenever the docs show a model field.
REQUIRED_REQUEST_REFS: dict = {
    "text": (MESSAGES_REF,),
    "vision": (MESSAGES_REF,),
    "video": (MESSAGES_REF,),
    "image": (MESSAGES_REF,),
    "tts": (MESSAGES_REF,),
    "stt": (MESSAGES_REF,),
}

# Per-kind response readers: at least one must be present (or auto mode).
REQUIRED_RESPONSE_ANY: dict = {
    "text": ("text", "tool_calls", "auto"),
    "vision": ("text", "auto"),
    "video": ("text", "auto"),
    "image": ("media", "auto"),
    "tts": ("media", "auto"),
    "stt": ("text", "auto"),
}

_REF_HINTS = {
    MESSAGES_REF: "the full conversation (system prompt L1-L3 + env/memories/chat/todo L4-L6 + current task L8)",
    MODEL_REF: "Field 1 (Model name)",
    TOOLS_REF: "the tool definitions (core tools + discovered skills/MCP tools)",
}


def _request_blob(parser: dict) -> str:
    try:
        req = parser.get("request", {}) if isinstance(parser.get("request"), dict) else {}
        return json.dumps(req, ensure_ascii=False)
    except Exception:
        return ""


def missing_required_refs(parser: Any, kind: str = "text") -> list[str]:
    """Names of mandatory refs absent from request/response. Empty = OK."""
    if not isinstance(parser, dict):
        return ["Parser Definition must be a JSON object."]
    kind = kind if kind in REQUIRED_REQUEST_REFS else "text"
    missing: list[str] = []
    blob = _request_blob(parser)
    for ref in REQUIRED_REQUEST_REFS[kind]:
        if ref not in blob:
            missing.append(ref)
    caps = parser.get("capabilities") if isinstance(parser.get("capabilities"), dict) else {}
    if caps.get("tools") is True and TOOLS_REF not in blob:
        missing.append(TOOLS_REF)
    resp = parser.get("response") if isinstance(parser.get("response"), dict) else {}
    if isinstance(resp, dict):
        want = REQUIRED_RESPONSE_ANY[kind]
        if not any(k in resp and isinstance(resp[k], dict) for k in want):
            missing.append("response." + "/".join(want))
    return missing


def describe_missing(ref: str) -> str:
    if ref.startswith("response."):
        return (f"Response reader '{ref}' is missing: the parser never reads the reply. "
                f"Map one of these response fields to the provider's reply shape.")
    hint = _REF_HINTS.get(ref, ref)
    if ref == TOOLS_REF:
        return (f"Required variable '{ref}' ({hint}) is missing from request.*. "
                f"The engine always sends tools — map them to the provider's tools field "
                f"(see the text-model tools example), or set capabilities.tools=false "
                f"only if the service truly cannot call tools.")
    return (f"Required variable '{ref}' ({hint}) is missing from request.*. "
            f"Reference it in the request body so nothing the engine sends is dropped.")


class AipError(Exception):
    def __init__(self, code: str, message: str, category: str = "invalid_request",
                 retryable: bool = False, http_status: int | None = None,
                 provider_code: str = "", details: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.retryable = retryable
        self.http_status = http_status
        self.provider_code = provider_code
        self.details = details[:500]

    def normalized(self) -> dict:
        return {"code": self.code, "message": self.message, "category": self.category,
                "retryable": self.retryable, "http_status": self.http_status,
                "provider_code": self.provider_code, "details": self.details}


class _Omit:
    pass


OMIT = _Omit()


# --------------------------------------------------------------------------
# URL policy (SSRF guard)
# --------------------------------------------------------------------------

def join_url(base: str, path: str) -> str:
    base = (base or "").strip()
    path = (path or "").strip()
    if not path:
        return base
    return base.rstrip("/") + "/" + path.lstrip("/")


def check_url(url: str) -> None:
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        raise AipError("bad_url", "Malformed request URL.", "configuration_error")
    if p.scheme not in ("http", "https") or not p.netloc:
        raise AipError("bad_url", "URL must start with http:// or https://.",
                       "configuration_error")
    host = (p.hostname or "").lower()
    if host in BLOCKED_HOSTS or host.endswith(".internal"):
        raise AipError("blocked_host", f"Requests to '{host}' are not allowed.",
                       "configuration_error")


# --------------------------------------------------------------------------
# Canonical validation (Black Box contract, versioned)
# --------------------------------------------------------------------------

CANONICAL_VERSION = 1


def validate_canonical(req: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(req, dict):
        return ["Canonical request must be an object."]
    if req.get("version", 1) != CANONICAL_VERSION:
        problems.append("Unsupported canonical version (only 1).")
    msgs = req.get("messages", [])
    if not isinstance(msgs, list) or not msgs:
        problems.append("messages must be a non-empty list.")
        msgs = []
    if len(msgs) > 200:
        problems.append("messages too long (max 200).")
    for i, m in enumerate(msgs[:200]):
        if not isinstance(m, dict):
            problems.append(f"messages[{i}] must be an object.")
            continue
        if m.get("role") not in ROLES:
            problems.append(f"messages[{i}].role must be one of {', '.join(ROLES)}.")
        c = m.get("content", "")
        if not isinstance(c, (str, list)):
            problems.append(f"messages[{i}].content must be text or typed parts.")
        elif isinstance(c, str) and len(c) > MAX_STRING:
            problems.append(f"messages[{i}].content too large (max 200k chars).")
        elif isinstance(c, list):
            if len(c) > 64:
                problems.append(f"messages[{i}].content too many parts (max 64).")
            for j, p in enumerate(c[:64]):
                if not isinstance(p, dict):
                    problems.append(f"messages[{i}].content[{j}] must be an object.")
                    continue
                t = p.get("type")
                if t not in PART_TYPES:
                    problems.append(
                        f"messages[{i}].content[{j}].type must be one of "
                        f"{', '.join(PART_TYPES)}.")
                elif t == "text":
                    if not isinstance(p.get("text"), str):
                        problems.append(f"messages[{i}].content[{j}] text part needs 'text'.")
                    elif len(p["text"]) > MAX_STRING:
                        problems.append(f"messages[{i}].content[{j}] text too large.")
                else:
                    data = p.get("url", p.get("b64", p.get("data")))
                    if not isinstance(data, str) or not data.strip():
                        problems.append(
                            f"messages[{i}].content[{j}] {t} part needs url, b64, or data.")
                    elif len(data) > MAX_PART_DATA:
                        problems.append(f"messages[{i}].content[{j}] {t} too large (max 25 MB).")
    gen = req.get("generation") or {}
    if gen and not isinstance(gen, dict):
        problems.append("generation must be an object.")
    elif isinstance(gen, dict):
        t = gen.get("temperature")
        if t is not None and (not isinstance(t, (int, float)) or isinstance(t, bool)
                              or not (0 <= float(t) <= 2)):
            problems.append("generation.temperature must be 0..2.")
        mt = gen.get("max_tokens")
        if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool)
                               or not (1 <= mt <= 200000)):
            problems.append("generation.max_tokens must be 1..200000.")
    tools = req.get("tools") or []
    if tools and not isinstance(tools, list):
        problems.append("tools must be a list.")
    elif isinstance(tools, list):
        if len(tools) > 64:
            problems.append("tools too many (max 64).")
        for i, t in enumerate(tools[:64]):
            if not isinstance(t, dict):
                problems.append(f"tools[{i}] must be an object.")
                continue
            if not isinstance(t.get("name"), str) or not TOOL_NAME_RE.match(t["name"]):
                problems.append(f"tools[{i}].name invalid (A-Za-z0-9_.-, max 64).")
    if req.get("stream") not in (None, True, False):
        problems.append("stream must be true/false.")
    return problems


# --------------------------------------------------------------------------
# Parser validation (no code execution, no secret access)
# --------------------------------------------------------------------------

DANGEROUS_TOKENS = ("eval", "exec(", "import ", "__", "os.", "sys.",
                    "subprocess", "open(", "api_key", "secret", "env")


def validate_parser(p: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(p, dict):
        return ["Parser Definition must be a JSON object."]
    if p.get("language") != LANGUAGE:
        problems.append(f"language must be '{LANGUAGE}'.")
    if p.get("version") != AIP_VERSION:
        problems.append("Unsupported parser version (only 1).")
    for section in ("request", "response"):
        if not isinstance(p.get(section), dict):
            problems.append(f"{section} must be an object.")
    req = p.get("request") if isinstance(p.get("request"), dict) else {}
    if req.get("method", "POST") not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        problems.append("request.method must be GET/POST/PUT/PATCH/DELETE.")
    for field in ("headers", "body"):
        if field in req and not isinstance(req[field], dict):
            problems.append(f"request.{field} must be an op object.")
    resp = p.get("response") if isinstance(p.get("response"), dict) else {}
    for field in ("text", "tool_calls", "finish_reason", "usage", "response_id", "media",
                  "provider_metadata", "auto"):
        if field in resp and not isinstance(resp[field], dict):
            problems.append(f"response.{field} must be an op object.")
    auth = p.get("auth", {"type": "none"})
    if not isinstance(auth, dict) or auth.get("type") not in (
            "none", "bearer", "header", "query", "body_token"):
        problems.append("auth.type must be none/bearer/header/query/body_token.")
    caps = p.get("capabilities", {})
    if caps and not isinstance(caps, dict):
        problems.append("capabilities must be an object.")
    # Op-name + danger scan (bounded).
    nodes = [p]
    seen = 0
    while nodes:
        node = nodes.pop()
        seen += 1
        if seen > MAX_NODES + 50:
            problems.append("Parser Definition too large (max ~500 ops).")
            break
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ALLOWED_OPS or k in (
                        "language", "version", "capabilities", "request", "response",
                        "errors", "streaming", "operations", "auth", "method",
                        "headers", "body", "text", "tool_calls", "finish_reason",
                        "usage", "response_id", "type", "header", "param", "prefix",
                        "field"):
                    pass
                elif k in ("cond", "then", "else", "over", "item", "value",
                           "format", "path", "op", "equals", "exists",
                           "non_empty", "parts", "text_join", "strict"):
                    pass
                else:
                    # Unknown executable op -> reject (spec: never guess).
                    if isinstance(v, dict) or isinstance(v, list):
                        pass
                    else:
                        continue
                if isinstance(v, str):
                    low = v.lower()
                    for tok in ("{{secrets", "{{env", "eval(", "__import__"):
                        if tok in low:
                            problems.append(f"Forbidden reference '{tok}' in parser.")
                if isinstance(v, (dict, list)):
                    nodes.append(v)
        elif isinstance(node, list):
            nodes.extend(node)
    # Hard reject arbitrary-code shapes.
    raw = json.dumps(p, ensure_ascii=False)[:20000].lower()
    for tok in ("eval(", "__import__", "{{secrets", "{{env"):
        if tok in raw:
            if not any(tok in x for x in problems):
                problems.append(f"Forbidden construct '{tok}'.")
    return problems


# --------------------------------------------------------------------------
# Op evaluator (deterministic, bounded)
# --------------------------------------------------------------------------

def _raw_get(ctx: dict, path: str) -> Any:
    if path == "$item":
        if "$item" not in ctx:
            raise AipError("bad_ref", "No $item in this context.", "parser_validation_error")
        return ctx["$item"]
    if path.startswith("$item."):
        node: Any = ctx.get("$item")
        path = path[len("$item."):]
    elif "." in path:
        root, rest = path.split(".", 1)
        if root not in ctx:
            raise AipError("bad_ref", f"Unknown context '{root}'.", "parser_validation_error")
        node = ctx[root]
        path = rest
    else:
        if path in ctx:
            return ctx[path]
        raise AipError("bad_ref", f"Unresolvable ref '{path}'.", "parser_validation_error")
    cur = node
    for seg in re.findall(r"[A-Za-z0-9_-]+|\[\d+\]", path):
        if seg.startswith("["):
            if not isinstance(cur, list):
                return None
            i = int(seg[1:-1])
            cur = cur[i] if 0 <= i < len(cur) else None
        else:
            cur = cur.get(seg) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def _flatten_parts(parts: list) -> list:
    """Flatten one nesting level (e.g. per-message contents mapped into one list)."""
    flat: list = []
    for p in parts[:MAX_ARRAY]:
        if isinstance(p, list):
            flat.extend(p[:MAX_ARRAY])
        else:
            flat.append(p)
    return flat[:MAX_ARRAY]


def _collect_media(node: Any, kind: str) -> list:
    """Walk messages/parts (one level) and return data strings of one kind.

    Canonical part shapes: text {type,text}, media {type, url|b64|data, mime}.
    Message objects ({role, content}) are descended into, never emitted.
    """
    out: list = []
    if isinstance(node, dict):
        if "content" in node and isinstance(node.get("role"), str):
            return _collect_media(node.get("content"), kind)
        if node.get("type") == kind:
            if kind == "text":
                t = node.get("text", "")
                return [str(t)] if isinstance(t, str) and t else []
            data = node.get("url", node.get("b64", node.get("data")))
            if isinstance(data, str) and data.strip():
                return [data.strip()]
        return []
    if isinstance(node, list):
        for item in node[:MAX_ARRAY]:
            if len(out) >= MAX_ARRAY:
                break
            if isinstance(item, str):
                if kind == "text" and item:
                    out.append(item)
                continue
            out.extend(_collect_media(item, kind))
        return out
    if isinstance(node, str):
        return [node] if kind == "text" and node else []
    return []


class _Runner:
    def __init__(self, ctx: dict):
        self.ctx = ctx
        self.count = 0
        self.depth = 0

    def run(self, node: Any) -> Any:
        self.count += 1
        if self.count > MAX_NODES:
            raise AipError("too_big", "Parser mapping too large.", "parser_validation_error")
        if self.depth > MAX_DEPTH:
            raise AipError("too_deep", "Parser mapping too deep.", "parser_validation_error")
        if isinstance(node, dict) and len(node) == 1 and next(iter(node)) in ALLOWED_OPS:
            op, arg = next(iter(node.items()))
            self.depth += 1
            try:
                return self._op(op, arg)
            finally:
                self.depth -= 1
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                r = self.run(v)
                if r is OMIT:
                    continue
                out[k] = r
            return out
        if isinstance(node, list):
            if len(node) > MAX_ARRAY:
                raise AipError("too_big", "Array too large (max 500).", "parser_validation_error")
            return [self.run(v) for v in node]
        if isinstance(node, str) and len(node) > MAX_STRING:
            raise AipError("too_big", "String too large.", "parser_validation_error")
        return node

    def _op(self, op: str, arg: Any) -> Any:
        if op == "literal":
            return arg
        if op == "ref":
            if not isinstance(arg, str):
                raise AipError("bad_op", "ref needs a string path.", "parser_validation_error")
            return _raw_get(self.ctx, arg)
        if op == "object":
            if not isinstance(arg, dict):
                raise AipError("bad_op", "object needs a dict.", "parser_validation_error")
            return self.run(arg)
        if op == "array":
            if not isinstance(arg, list):
                raise AipError("bad_op", "array needs a list.", "parser_validation_error")
            return self.run(arg)
        if op == "map":
            over = self.run(arg.get("over"))
            if over is None:
                return []
            if not isinstance(over, list):
                raise AipError("bad_op", "map.over must be a list.", "parser_validation_error")
            compact = bool(arg.get("compact", False))
            out = []
            for item in over[:MAX_ARRAY]:
                sub = _Runner(dict(self.ctx, **{"$item": item}))
                sub.count = self.count
                r = sub.run(arg.get("item"))
                self.count = sub.count
                if compact and r is None:
                    continue
                out.append(r)
            return out
        if op == "if":
            cond = arg.get("cond", {})
            take = self._cond(cond)
            return self.run(arg.get("then") if take else arg.get("else"))
        if op == "coalesce":
            for branch in arg or []:
                v = self.run(branch)
                if v is not None:
                    return v
            return None
        if op == "omit_if_null":
            v = self.run(arg)
            return OMIT if v is None else v
        if op == "rename":
            v = self.run(arg.get("value"))
            return {str(arg.get("to", "value")): v}
        if op == "select":
            if not isinstance(arg, str):
                raise AipError("bad_op", "select needs a path string.", "parser_validation_error")
            return _raw_get(self.ctx, arg)
        if op == "parse_json":
            v = self.run(arg)
            if not isinstance(v, str):
                raise AipError("bad_args", "parse_json needs a string.", "response_decoding_error")
            try:
                return json.loads(v)
            except Exception:
                raise AipError("bad_args", "Tool arguments are not valid JSON; not executed.",
                               "response_decoding_error")
        if op == "to_string":
            v = self.run(arg)
            return "" if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
        if op == "to_number":
            v = self.run(arg)
            try:
                return float(v)
            except Exception:
                raise AipError("bad_op", "to_number failed.", "parser_validation_error")
        if op == "to_boolean":
            v = self.run(arg)
            return bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes")
        if op == "merge":
            out = {}
            for branch in arg or []:
                v = self.run(branch)
                if isinstance(v, dict):
                    out.update(v)
            return out
        if op == "encode":
            if not isinstance(arg, dict) or arg.get("format") != "base64":
                raise AipError("bad_op", "Only base64 encode is supported.", "parser_validation_error")
            v = self.run(arg.get("value"))
            raw = v.encode("utf-8") if isinstance(v, str) else bytes(v or b"")
            return base64.b64encode(raw).decode("ascii")
        if op == "transform_content":
            parts = self.run(arg.get("parts")) if isinstance(arg, dict) else None
            if not isinstance(parts, list):
                # Plain string content passes through.
                return parts if isinstance(parts, str) else ""
            texts = []
            for p in _flatten_parts(parts):
                if isinstance(p, str):
                    texts.append(p)
                elif isinstance(p, dict) and p.get("type") == "text":
                    texts.append(str(p.get("text", "")))
            joiner = arg.get("text_join", "\n") if isinstance(arg, dict) else "\n"
            return joiner.join(t for t in texts if t)
        if op == "collect_media":
            if not isinstance(arg, dict):
                raise AipError("bad_op", "collect_media needs an object.", "parser_validation_error")
            kind = arg.get("kind")
            if kind not in PART_TYPES:
                raise AipError("bad_op", "collect_media.kind must be text/image/audio/video.",
                               "parser_validation_error")
            parts = self.run(arg.get("parts"))
            return _collect_media(parts, kind)
        raise AipError("bad_op", f"Unsupported op '{op}'.", "parser_validation_error")

    def _cond(self, cond: Any) -> bool:
        if not isinstance(cond, dict):
            return bool(self.run(cond))
        if "is_list" in cond:
            return isinstance(self.run(cond["is_list"]), list)
        if "equals" in cond:
            a, b = cond["equals"][0], cond["equals"][1]
            return self.run(a) == self.run(b)
        if "exists" in cond:
            return self.run(cond["exists"]) is not None
        if "non_empty" in cond:
            v = self.run(cond["non_empty"])
            return bool(v) and v not in ("", [], {})
        raise AipError("bad_op", "Unsupported condition.", "parser_validation_error")


def evaluate(node: Any, ctx: dict) -> Any:
    return _Runner(dict(ctx)).run(node)


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

def check_capabilities(parser: dict, req: dict) -> list[str]:
    caps = parser.get("capabilities") or {}
    problems: list[str] = []
    if req.get("stream") and not caps.get("streaming", True):
        problems.append("This model does not support streaming.")
    if req.get("tools") and not caps.get("tools", True):
        problems.append("This model does not support tool calling.")
    if req.get("response_format") and not caps.get("structured_output", True):
        problems.append("This model does not support structured output.")
    mods = req.get("modalities") or {}
    if isinstance(mods, dict):
        declared_in = caps.get("input_modalities")
        if declared_in is not None:
            for m in mods.get("input") or []:
                if m not in declared_in:
                    problems.append(f"This model does not take '{m}' input.")
        declared_out = caps.get("output_modalities")
        if declared_out is not None:
            for m in mods.get("output") or []:
                if m not in declared_out:
                    problems.append(f"This model does not produce '{m}' output.")
    return problems


# --------------------------------------------------------------------------
# Request building + auth (reviewed mechanism, parser never touches secrets)
# --------------------------------------------------------------------------

def _apply_auth(headers: dict, query: dict, body: Any, auth: dict, api_key: str) -> tuple:
    headers = dict(headers or {})
    query = dict(query or {})
    t = (auth or {}).get("type", "none")
    if t == "none":
        return headers, query, body
    if not api_key:
        raise AipError("auth_missing", "This model needs an API key (connection settings).",
                       "authentication_failure")
    if t == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif t == "header":
        name = str(auth.get("header") or "X-API-Key")
        if not re.match(r"^[A-Za-z0-9-]+$", name):
            raise AipError("bad_auth", "Invalid auth header name.", "configuration_error")
        headers[name] = f"{auth.get('prefix', '')}{api_key}"
    elif t == "query":
        query[str(auth.get("param") or "key")] = api_key
    elif t == "body_token":
        if isinstance(body, dict):
            body = dict(body)
            body[str(auth.get("field") or "api_key")] = api_key
    return headers, query, body


# --------------------------------------------------------------------------
# Compile-once (interpreter runs ONE time, fast path after that)
#
# compile_model() is the first-time interpreter run: it validates the parser,
# resolves the URL, and freezes the static plan. Later requests reuse the
# compiled plan via get_compiled()/execute_compiled() and never re-validate
# the parser. Two things ALWAYS still run per request and can never be
# skipped: canonical input validation (external input is untrusted) and
# response normalization (provider output is untrusted).
# --------------------------------------------------------------------------

_COMPILED: dict = {}
_COMPILED_MAX = 16


def fingerprint(model_record: dict) -> str:
    import hashlib as _hl
    parser = model_record.get("parser_definition") or model_record.get("config_json") or {}
    raw = json.dumps({
        "m": model_record.get("model_name", ""),
        "s": model_record.get("server_url", ""),
        "a": model_record.get("api_url", ""),
        "p": parser,
    }, sort_keys=True, ensure_ascii=False)
    return _hl.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compile_model(model_record: dict) -> dict:
    """First (and only) full interpreter run for this configuration."""
    parser = model_record.get("parser_definition") or model_record.get("config_json") or {}
    probs = validate_parser(parser)
    if probs:
        raise AipError("bad_parser", probs[0], "parser_validation_error",
                       details="; ".join(probs)[:500])
    kind = str(model_record.get("kind") or "text")
    if kind not in REQUIRED_REQUEST_REFS:
        kind = "text"
    miss = missing_required_refs(parser, kind)
    if miss:
        first = miss[0]
        raise AipError("missing_variable", describe_missing(first), "parser_validation_error",
                       details="; ".join(describe_missing(m) for m in miss)[:500])
    url = join_url(str(model_record.get("server_url") or ""),
                   str(model_record.get("api_url") or ""))
    check_url(url)
    req_def = parser.get("request", {}) if isinstance(parser.get("request"), dict) else {}
    return {
        "fingerprint": fingerprint(model_record),
        "parser": parser,
        "url": url,
        "method": str(req_def.get("method", "POST")).upper(),
        "auth": parser.get("auth", {"type": "none"}),
        "capabilities": parser.get("capabilities") or {},
        "model_name": str(model_record.get("model_name") or ""),
        "api_key": str(model_record.get("api_key") or ""),  # server memory only
    }


def get_compiled(model_record: dict, model_id: str = "default") -> dict:
    """Cached compile: recompiles only when the configuration changed."""
    fp = fingerprint(model_record)
    hit = _COMPILED.get(model_id)
    if isinstance(hit, dict) and hit.get("fingerprint") == fp:
        return hit
    compiled = compile_model(model_record)
    if len(_COMPILED) >= _COMPILED_MAX:
        _COMPILED.pop(next(iter(_COMPILED)))
    _COMPILED[model_id] = compiled
    return compiled


def _build_from_compiled(compiled: dict, canonical: dict) -> tuple:
    """Per-request build from a compiled plan (values differ every call)."""
    cprobs = validate_canonical(canonical)
    if cprobs:
        raise AipError("bad_request", cprobs[0], "invalid_request",
                       details="; ".join(cprobs)[:500])
    cap = check_capabilities({"capabilities": compiled.get("capabilities")}, canonical)
    if cap:
        raise AipError("no_capability", cap[0], "unsupported_capability")
    if compiled.get("capabilities", {}).get("async_workflow"):
        raise AipError("no_capability",
                       "This provider needs an async job workflow (submit, wait, fetch). "
                       "AIP/1 covers single-shot JSON only; async polling is not implemented.",
                       "unsupported_capability")

    parser = compiled["parser"]
    ctx = {"input": canonical, "model": {"model_name": compiled.get("model_name", "")}}
    req_def = parser.get("request", {})
    headers = evaluate(req_def.get("headers", {"literal": {}}), ctx) or {}
    body = evaluate(req_def.get("body", {"literal": {}}), ctx)
    if not isinstance(headers, dict):
        raise AipError("bad_parser", "request.headers must build an object.", "parser_validation_error")
    headers, query, body = _apply_auth(
        headers, {}, body, compiled.get("auth", {"type": "none"}),
        str(compiled.get("api_key") or ""))

    url = str(compiled.get("url") or "")
    if query:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    method = str(compiled.get("method", "POST")).upper()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if not isinstance(body, str) else body.encode("utf-8")
    if len(raw) > MAX_BODY_BYTES:
        raise AipError("too_big", "Request body too large (max 5 MB).", "invalid_request")
    headers = {str(k): str(v) for k, v in headers.items()}
    if not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"
    return url, method, headers, raw


def build_request(model_record: dict, canonical: dict) -> tuple:
    """Returns (url, method, headers, body_bytes). Raises AipError."""
    return _build_from_compiled(compile_model(model_record), canonical)


# --------------------------------------------------------------------------
# Auto translation (unknown JSON shapes — READING only, best-effort)
#
# Writing a request can never be automatic: the provider's contract is the
# only truth about what a server accepts, and sending guessed fields would
# be fabrication with real side effects. Responses are safe to probe because
# extraction only reads values that are actually present.
# Anything ambiguous fails clearly instead of guessing.
# --------------------------------------------------------------------------

_AUTO_TEXT_KEYS = ("content", "text", "reply", "output_text", "answer",
                   "completion", "output", "result", "generated_text")
_AUTO_CALL_KEYS = ("tool_calls", "actions", "function_calls", "function_call")
_AUTO_NAME_KEYS = ("name", "tool")
_AUTO_ARGS_KEYS = ("arguments", "args", "parameters", "input")
_AUTO_MEDIA_KEYS = ("b64_json", "image_url", "image", "audio", "video",
                    "file_url", "media")
_AUTO_ERROR_KEYS = ("error", "errors")
_AUTO_SKIP_SUBTREE = ("tool_calls", "actions", "function_calls", "function_call",
                      "arguments", "args", "parameters", "usage")
_AUTO_MAX_NODES = 2000
_AUTO_MAX_DEPTH = 8


def _auto_find(node: Any, keys: tuple, depth: int, budget: list,
               skip: tuple = (), containers: bool = True) -> tuple:
    """Depth-first search for first non-empty value under a key name.

    Returns (path, value) or (None, None). Never raises on shape mismatch.
    Strings win over containers so {"reply": {"text": "..."}} finds the text,
    not the wrapper (text search passes containers=False).
    """
    budget[0] -= 1
    if budget[0] < 0 or depth > _AUTO_MAX_DEPTH:
        return None, None
    if isinstance(node, dict):
        for k, v in node.items():
            kl = str(k).lower()
            if kl in keys and k not in skip:
                if isinstance(v, str) and v.strip():
                    return str(k), v
                if containers and isinstance(v, (dict, list)) and v:
                    return str(k), v
        for k, v in node.items():
            if str(k).lower() in skip and keys != _AUTO_ERROR_KEYS:
                continue
            p, val = _auto_find(v, keys, depth + 1, budget, skip, containers)
            if val is not None:
                return f"{k}.{p}" if p else str(k), val
    elif isinstance(node, list):
        for i, item in enumerate(node[:50]):
            p, val = _auto_find(item, keys, depth + 1, budget, skip, containers)
            if val is not None:
                return f"[{i}]{('.' + p) if p else ''}", val
    return None, None


def _auto_name(item: dict) -> str:
    for k in _AUTO_NAME_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    fn = item.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"].strip()
    return ""


def _auto_args(item: dict) -> tuple:
    """Returns (present, args). Absent args key means 'probably not a tool call'
    (skip silently); a present-but-bad value means 'skip with a warning'."""
    for k in _AUTO_ARGS_KEYS:
        if k in item:
            v = item[k]
            if isinstance(v, dict):
                return True, v
            if isinstance(v, str):
                if not v.strip():
                    return True, {}
                try:
                    parsed = json.loads(v)
                    return True, parsed if isinstance(parsed, dict) else None
                except Exception:
                    return True, None
            return True, None
    fn = item.get("function")
    if isinstance(fn, dict):
        return _auto_args(fn)
    return False, None


_B64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


def _looks_url(s: str) -> bool:
    low = s.strip().lower()
    return low.startswith(("http://", "https://", "data:"))


def _looks_blob(s: str) -> bool:
    s = s.strip()
    if len(s) < 64:
        return False
    sample = s[:512]
    return sum(1 for ch in sample if ch in _B64_CHARS) / len(sample) > 0.9


def _auto_scan_strings(node: Any, depth: int, budget: list, path: str,
                       acc: list) -> None:
    """Collect every string with its path (shape-based fallback, no key names)."""
    budget[0] -= 1
    if budget[0] < 0 or depth > _AUTO_MAX_DEPTH or len(acc) >= 500:
        return
    if isinstance(node, str):
        if node.strip():
            acc.append((path or "$", node.strip()))
    elif isinstance(node, dict):
        for k, v in node.items():
            _auto_scan_strings(v, depth + 1, budget,
                               f"{path}.{k}" if path else str(k), acc)
    elif isinstance(node, list):
        for i, item in enumerate(node[:50]):
            _auto_scan_strings(item, depth + 1, budget, f"{path}[{i}]", acc)


def normalize_auto(provider_text: str) -> dict:
    """Translate any unknown JSON response shape. Best-effort, never fabricates."""
    try:
        provider: Any = json.loads(provider_text) if provider_text.strip() else None
    except Exception:
        raise AipError("bad_response", "Reply is not JSON; auto mode needs JSON.",
                       "response_decoding_error")
    if not isinstance(provider, (dict, list)):
        raise AipError("bad_response", "Reply is not a JSON object.",
                       "response_decoding_error")
    budget = [_AUTO_MAX_NODES]
    out: dict = {"status": "ok", "warnings": [
        "Auto translation is best-effort; register an explicit parser for reliability."]}
    # Error first: any real error value fails the request.
    _, err = _auto_find(provider, _AUTO_ERROR_KEYS, 0, [_AUTO_MAX_NODES])
    if err is not None and err is not False and err not in ("", [], {}):
        msg = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)[:300]
        raise AipError("provider", str(msg)[:300], "provider_server_error")
    # Tool calls / actions.
    _, calls = _auto_find(provider, _AUTO_CALL_KEYS, 0, [_AUTO_MAX_NODES])
    if isinstance(calls, dict):
        calls = [calls]
    found_calls: list[dict] = []
    if isinstance(calls, list):
        for c in calls[:64]:
            if not isinstance(c, dict):
                continue
            name = _auto_name(c)
            if not name or not TOOL_NAME_RE.match(name):
                continue
            present, args = _auto_args(c)
            if not present:
                continue  # a name without arguments is data, not a tool call
            if args is None:
                out["warnings"].append(f"Tool '{name}' arguments unreadable; skipped.")
                continue
            found_calls.append({"id": str(c.get("id", ""))[:128], "name": name,
                                "arguments": args, "status": "pending"})
    if found_calls:
        names = sorted({c["name"] for c in found_calls})
        out["tool_calls"] = found_calls
        out["warnings"].append(f"Auto-detected tool calls: {', '.join(names)}.")
    # Text (skips tool-call subtrees so argument strings are never mistaken).
    tpath, text = _auto_find(provider, _AUTO_TEXT_KEYS, 0, [_AUTO_MAX_NODES],
                             skip=_AUTO_SKIP_SUBTREE, containers=False)
    if isinstance(text, str) and text.strip():
        out["text"] = text.strip()
        out["message"] = {"role": "assistant", "content": text.strip()}
        out["warnings"].append(f"Auto-detected text at '{tpath}'.")
    else:
        # Nameless fallback: longest meaningful string (not a URL, not a blob).
        acc: list = []
        _auto_scan_strings(provider, 0, [_AUTO_MAX_NODES], "", acc)
        best = ("", "")
        for p, s in acc:
            if _looks_url(s) or _looks_blob(s):
                continue
            if len(s) > len(best[1]):
                best = (p, s)
        if best[1]:
            out["text"] = best[1]
            out["message"] = {"role": "assistant", "content": best[1]}
            out["warnings"].append(
                f"Low-confidence text guess at '{best[0]}' (no known field name).")
    # Media (URL or long blob only; short strings are labels, not files).
    mpath, media = _auto_find(provider, _AUTO_MEDIA_KEYS, 0, [_AUTO_MAX_NODES],
                              skip=_AUTO_SKIP_SUBTREE)
    if isinstance(media, dict):
        for k in ("url", "b64_json", "data", "b64"):
            if isinstance(media.get(k), str) and media[k].strip():
                media = media[k].strip()
                mpath = f"{mpath}.{k}"
                break
    if isinstance(media, str) and media.strip():
        v = media.strip()
        last = (mpath or "").split(".")[-1].strip("[]").lower()
        named = last in _AUTO_MEDIA_KEYS + ("url", "b64", "data")
        if not (named or _looks_url(v) or _looks_blob(v)):
            media = None
        else:
            if len(v) > MAX_BODY_BYTES:
                raise AipError("too_big", "Media value too large (over 5 MB).",
                               "response_decoding_error")
            out["media"] = {"data": v, "source": _media_source(v)}
            out["warnings"].append(f"Auto-detected media at '{mpath}'.")
    if "media" not in out:
        # Nameless fallback: first URL/data-URL/blob-shaped string anywhere.
        acc2: list = []
        _auto_scan_strings(provider, 0, [_AUTO_MAX_NODES], "", acc2)
        hits = [(p, s) for p, s in acc2 if _looks_url(s) or _looks_blob(s)]
        if hits:
            p, s = hits[0]
            if len(s) > MAX_BODY_BYTES:
                raise AipError("too_big", "Media value too large (over 5 MB).",
                               "response_decoding_error")
            out["media"] = {"data": s, "source": _media_source(s)}
            extra = f" (+{len(hits) - 1} more ignored)" if len(hits) > 1 else ""
            out["warnings"].append(f"Low-confidence media guess at '{p}'{extra}.")
    # Usage.
    _, usage = _auto_find(provider, ("usage",), 0, [_AUTO_MAX_NODES])
    if isinstance(usage, dict) and usage:
        out["usage"] = {str(k)[:64]: usage[k] for k in list(usage)[:16]}
    if "text" not in out and "tool_calls" not in out and "media" not in out:
        keys = (sorted(str(k) for k in provider.keys())[:12]
                if isinstance(provider, dict) else ["list"])
        raise AipError(
            "bad_response",
            f"Unrecognized reply shape (top keys: {', '.join(keys) or '(none)'}). "
            f"Point an explicit parser at the right fields.",
            "response_decoding_error")
    return out


# --------------------------------------------------------------------------
# Response normalization (never fabricate)
# --------------------------------------------------------------------------

def normalize(provider_text: str, parser: dict) -> dict:
    try:
        provider: Any = json.loads(provider_text) if provider_text.strip() else {}
    except Exception:
        provider = {"_raw": provider_text[:2000]}
    ctx = {"response": provider}
    resp_def = parser.get("response", {}) if isinstance(parser.get("response"), dict) else {}
    # Auto mode: unknown JSON shape -> best-effort extraction (responses only).
    if isinstance(resp_def.get("auto"), dict):
        try:
            flag = _Runner(dict(ctx)).run(resp_def["auto"])
        except AipError:
            flag = False
        if flag:
            return normalize_auto(provider_text)
    # Provider-level error (errors.path): null/absent means OK, anything else fails.
    errs = parser.get("errors") if isinstance(parser.get("errors"), dict) else {}
    epath = errs.get("path") if isinstance(errs, dict) else None
    if isinstance(epath, str) and epath.strip():
        ev = _raw_get(ctx, epath.strip())
        if ev is not None and ev is not False and ev not in ("", [], {}):
            msg = ev if isinstance(ev, str) else json.dumps(ev, ensure_ascii=False)[:300]
            raise AipError("provider", str(msg)[:300], "provider_server_error")

    def _opt(name: str) -> Any:
        return evaluate(resp_def[name], ctx) if isinstance(resp_def.get(name), dict) else None

    out: dict = {"status": "ok", "warnings": []}
    text = _opt("text")
    if isinstance(text, str) and text.strip():
        out["text"] = text
        out["message"] = {"role": "assistant", "content": text}
    raw_calls = _opt("tool_calls")
    calls: list[dict] = []
    if isinstance(raw_calls, list):
        for c in raw_calls[:64]:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                continue
            args = c.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    out["warnings"].append(f"Tool '{name}' arguments invalid JSON; skipped.")
                    continue
            if not isinstance(args, dict):
                continue
            call = {"id": str(c.get("id", ""))[:128], "name": name,
                    "arguments": args, "status": str(c.get("status", "pending"))[:32]}
            md = c.get("metadata")
            if isinstance(md, dict) and len(md) <= 16:
                try:
                    if len(json.dumps(md, ensure_ascii=False)) <= 5000:
                        call["metadata"] = md
                except Exception:
                    pass
            calls.append(call)
    if calls:
        out["tool_calls"] = calls
    for name in ("finish_reason", "usage", "response_id"):
        v = _opt(name)
        if v is not None:
            out[name] = v
    # Media output (image/audio): b64, data-URL, or file URL. Never fabricated;
    # missing/empty stays absent. Transport already caps raw bodies at 5 MB.
    media = _opt("media") if isinstance(resp_def.get("media"), dict) else None
    if isinstance(media, str) and media.strip():
        v = media.strip()
        if len(v) > MAX_BODY_BYTES:
            raise AipError("too_big", "Media value too large (over 5 MB).",
                           "response_decoding_error")
        out["media"] = {"data": v, "source": _media_source(v)}
    if isinstance(resp_def.get("provider_metadata"), dict):
        pm = _opt("provider_metadata")
        if isinstance(pm, dict) and pm:
            try:
                if len(json.dumps(pm, ensure_ascii=False)) <= 20000:
                    out["provider_metadata"] = {str(k)[:64]: pm[k] for k in list(pm)[:32]}
            except Exception:
                out["warnings"].append("provider_metadata dropped (not serializable).")
    return out


def _media_source(value: str) -> str:
    v = (value or "").strip()
    if v.startswith("data:"):
        return "data_url"
    low = v.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return "url"
    return "base64"


def normalize_error(e: Exception) -> dict:
    if isinstance(e, AipError):
        return e.normalized()
    return AipError("internal", f"Unexpected: {e}"[:300], "provider_server_error").normalized()


# --------------------------------------------------------------------------
# Execution (Black Box entry points — canonical in, canonical out)
# --------------------------------------------------------------------------

def _send_once(url: str, method: str, headers: dict, body: bytes, cancel_event=None) -> tuple[int, str]:
    from core.ai_models import engine as _eng
    status, raw = _eng._send(url, method, headers, body, 60.0, cancel_event)
    return status, raw.decode("utf-8", errors="replace")


def _retryable(status: int, category: str) -> bool:
    return category in ("network_error", "timeout", "rate_limit") or status in (429,) or 500 <= status < 600


def execute_compiled(compiled: dict, canonical: dict, cancel_event=None) -> dict:
    """Fast path: compiled plan in, canonical response out. The parser is NOT
    re-validated here (done once by compile_model); input validation, the
    async-workflow refusal, and response normalization still run every time.
    cancel_event (optional threading.Event): STOP aborts the request between
    read chunks and skips retries/backoff instead of blocking up to 3 min."""
    url, method, headers, body = _build_from_compiled(compiled, canonical)
    parser = compiled["parser"]
    last: AipError | None = None
    for attempt in range(MAX_RETRIES):
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            from core.ai_models import engine as _eng0
            raise _eng0.EngineError("timeout", "Cancelled.",
                                    technical="cancelled before send",
                                    copyable="Cancelled.")
        try:
            status, text = _send_once(url, method, headers, body, cancel_event)
            if status in (401, 403):
                raise AipError("auth", "Service refused the key (401/403).", "authentication_failure",
                               http_status=status)
            if status == 429:
                raise AipError("rate_limit", "Rate limited (429).", "rate_limiting",
                               retryable=True, http_status=status)
            if status == 404:
                raise AipError("not_found", "Address or model not found (404).", "invalid_request",
                               http_status=status)
            if status >= 400:
                raise AipError("provider", f"Service returned HTTP {status}.", "provider_server_error",
                               http_status=status)
            res = normalize(text, parser)
            try:
                from core.lm.usage import log_usage as _log_usage

                _usage = res.get("usage") or {}
                _log_usage(str(compiled.get("model_name") or "unknown"),
                           _usage.get("prompt_tokens") or 0,
                           _usage.get("completion_tokens") or 0)
            except Exception:
                pass  # accounting must never break chat
            return res
        except AipError as e:
            last = e
            if not e.retryable and not _retryable(e.http_status or 0, e.category):
                raise
            if attempt < MAX_RETRIES - 1:
                _backoff = 1.0 * (attempt + 1)
                if cancel_event is not None:
                    # Interruptible backoff so STOP lands during retries too.
                    _end = time.time() + _backoff
                    while time.time() < _end:
                        if getattr(cancel_event, "is_set", lambda: False)():
                            from core.ai_models import engine as _eng2
                            raise _eng2.EngineError("timeout", "Cancelled.",
                                                    technical="cancelled during retry backoff",
                                                    copyable="Cancelled.")
                        time.sleep(0.25)
                else:
                    time.sleep(_backoff)
    raise last or AipError("network", "Request failed.", "network_error", retryable=True)


def execute(model_record: dict, canonical: dict, model_id: str | None = None, cancel_event=None) -> dict:
    """model_id set -> compiled plan reused (interpreter ran once);
    None -> fresh compile (old behavior, e.g. one-off tests).
    cancel_event is forwarded to the request layer (STOP aborts it)."""
    compiled = get_compiled(model_record, model_id) if model_id else compile_model(model_record)
    return execute_compiled(compiled, canonical, cancel_event)


def test_connection(model_record: dict, model_id: str | None = None) -> dict:
    t0 = time.time()
    try:
        res = execute(model_record, {
            "version": 1,
            "messages": [{"role": "user", "content": "Reply with the single word: OK"}],
        }, model_id)
        ms = int((time.time() - t0) * 1000)
        preview = (res.get("text") or "OK")[:200]
        return {"ok": True, "latency_ms": ms, "reply_preview": preview, "error": None,
                "canonical": res}
    except AipError as e:
        ms = int((time.time() - t0) * 1000)
        n = e.normalized()
        return {"ok": False, "latency_ms": ms, "reply_preview": None,
                "error": {"category": n["category"], "message": n["message"],
                          "technical": n["code"], "copyable": f"{n['message']} [{n['code']}]"}}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return {"ok": False, "latency_ms": ms, "reply_preview": None,
                "error": {"category": "server", "message": str(e)[:300],
                          "technical": "unexpected", "copyable": str(e)[:500]}}


# --------------------------------------------------------------------------
# Bundled templates (official shapes; adjust paths/literals per docs only)
# --------------------------------------------------------------------------

# Canonical image part -> OpenAI content block (text passes, image url maps,
# anything else is dropped by compact:true; modalities gate rejects audio/video
# upfront when the parser declares input_modalities).
_VISION_PART = {
    "if": {
        "cond": {"equals": [{"select": "$item.type"}, {"literal": "text"}]},
        "then": {"object": {
            "type": {"literal": "text"},
            "text": {"select": "$item.text"}}},
        "else": {
            "if": {
                "cond": {"equals": [{"select": "$item.type"}, {"literal": "image"}]},
                "then": {"object": {
                    "type": {"literal": "image_url"},
                    "image_url": {"object": {
                        "url": {"coalesce": [
                            {"select": "$item.url"},
                            {"select": "$item.data"}]}}}}},
                "else": {"literal": None}}}}}


def openai_vision_parser() -> dict:
    """Chat with images (official chat-completions content-block shape)."""
    return {
        "language": "AIP/1",
        "version": 1,
        "capabilities": {"tools": True, "streaming": False, "structured_output": True,
                          "input_modalities": ["text", "image"],
                          "output_modalities": ["text"]},
        "auth": {"type": "bearer"},
        "request": {
            "method": "POST",
            "headers": {"literal": {"Content-Type": "application/json"}},
            "body": {"object": {
                "model": {"ref": "model.model_name"},
                "messages": {"map": {
                    "over": {"ref": "input.messages"},
                    "item": {"object": {
                        "role": {"select": "$item.role"},
                        "content": {
                            "if": {
                                "cond": {"is_list": {"select": "$item.content"}},
                                "then": {"map": {
                                    "over": {"select": "$item.content"},
                                    "compact": True,
                                    "item": _VISION_PART}},
                                "else": {"select": "$item.content"}}}}}}},
                "tools": {"map": {
                    "over": {"coalesce": [{"ref": "input.tools"}, {"literal": []}]},
                    "item": {"object": {
                        "type": {"literal": "function"},
                        "function": {"object": {
                            "name": {"select": "$item.name"},
                            "description": {"select": "$item.description"},
                            "parameters": {"select": "$item.parameters_schema"}}}}}}},
                "tool_choice": {"omit_if_null": {"ref": "input.tool_choice"}},
            }},
        },
        "response": {
            "text": {"select": "response.choices[0].message.content"},
            "finish_reason": {"select": "response.choices[0].finish_reason"},
            "usage": {"select": "response.usage"},
            "response_id": {"select": "response.id"},
            "tool_calls": {"map": {
                "over": {"coalesce": [
                    {"select": "response.choices[0].message.tool_calls"},
                    {"literal": []}]},
                "item": {"object": {
                    "id": {"select": "$item.id"},
                    "name": {"select": "$item.function.name"},
                    "arguments": {"select": "$item.function.arguments"}}}}},
        },
        "errors": {},
    }


def image_gen_parser(response_path: str = "response.data[0].b64_json",
                      size: str = "1024x1024") -> dict:
    """Single-shot JSON image generation (prompt = joined message texts).

    Audio/video single-shot JSON providers use the same shape: point
    response_path at the file URL/b64 field; source is auto-detected
    (url, base64, data_url). Async job providers are refused explicitly.
    """
    return {
        "language": "AIP/1",
        "version": 1,
        "capabilities": {"tools": False, "streaming": False,
                          "structured_output": False,
                          "input_modalities": ["text"],
                          "output_modalities": ["image", "audio", "video"]},
        "auth": {"type": "bearer"},
        "request": {
            "method": "POST",
            "headers": {"literal": {"Content-Type": "application/json"}},
            "body": {"object": {
                "model": {"ref": "model.model_name"},
                "prompt": {"transform_content": {
                    "parts": {"map": {
                        "over": {"ref": "input.messages"},
                        "item": {"select": "$item.content"}}},
                    "text_join": "\n"}},
                "size": {"literal": size}}},
        },
        "response": {
            "media": {"select": response_path},
            "usage": {"select": "response.usage"},
        },
        "errors": {},
    }

def openai_text_parser() -> dict:
    return {
        "language": "AIP/1",
        "version": 1,
        "capabilities": {"tools": True, "streaming": False, "structured_output": True},
        "auth": {"type": "bearer"},
        "request": {
            "method": "POST",
            "headers": {"literal": {"Content-Type": "application/json"}},
            "body": {"object": {
                "model": {"ref": "model.model_name"},
                "messages": {"ref": "input.messages"},
                "tools": {"map": {
                    "over": {"coalesce": [{"ref": "input.tools"}, {"literal": []}]},
                    "item": {"object": {
                        "type": {"literal": "function"},
                        "function": {"object": {
                            "name": {"select": "$item.name"},
                            "description": {"select": "$item.description"},
                            "parameters": {"select": "$item.parameters_schema"}}}}}}},
                "tool_choice": {"omit_if_null": {"ref": "input.tool_choice"}},
            }},
        },
        "response": {
            "text": {"select": "response.choices[0].message.content"},
            "finish_reason": {"select": "response.choices[0].finish_reason"},
            "usage": {"select": "response.usage"},
            "response_id": {"select": "response.id"},
            "tool_calls": {"map": {
                "over": {"coalesce": [
                    {"select": "response.choices[0].message.tool_calls"},
                    {"literal": []},
                ]},
                "item": {"object": {
                    "id": {"select": "$item.id"},
                    "name": {"select": "$item.function.name"},
                    # Raw string on purpose: normalize() decodes per call and
                    # skips malformed args with a warning (never executes them).
                    "arguments": {"select": "$item.function.arguments"},
                }},
            }},
        },
        "errors": {},
    }


# --------------------------------------------------------------------------
# Loop bridge: OpenAI loop messages/tools -> canonical (single place where
# the engine's L1-L8 context becomes the interpreter's input).
# --------------------------------------------------------------------------

def canonical_from_loop(messages: Any, tools_openai: Any) -> dict:
    """Build the canonical request the interpreter sends.

    messages: loop history [{role, content, ...}] (system L1-L3 + user
      context L4-L8 + assistant/tool turns). content may be text or typed
      parts; passed through untouched.
    tools_openai: OpenAI function specs [{type, function:{name, description,
      parameters}}] (core + discovered skills/MCP tools).
    Returns {"version":1, "messages":..., "tools":[{name, description,
    parameters_schema}]}. Never raises on odd shapes (skips bad entries).
    """
    canon_msgs: list = []
    try:
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role not in ROLES:
                continue
            # Pass the OpenAI conversation fields through untouched. In
            # particular tool_call_id (tool results) and tool_calls
            # (assistant turns) MUST survive: providers reject tool results
            # without a matching id (HTTP 400). Only known wire-safe keys.
            item = {k: m[k] for k in ("role", "content", "name") if k in m}
            if isinstance(m.get("tool_call_id"), str) and m["tool_call_id"].strip():
                item["tool_call_id"] = m["tool_call_id"]
            tcs = m.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                item["tool_calls"] = [tc for tc in tcs if isinstance(tc, dict)][:64]
            canon_msgs.append(item)
    except Exception:
        canon_msgs = []
    canon_tools: list = []
    try:
        for t in tools_openai or []:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            name = fn.get("name") if isinstance(fn, dict) else None
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                continue
            canon_tools.append({
                "name": name,
                "description": str(fn.get("description") or "")[:2000],
                "parameters_schema": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {},
            })
    except Exception:
        canon_tools = []
    out: dict = {"version": 1, "messages": canon_msgs}
    if canon_tools:
        out["tools"] = canon_tools
    return out
