"""
X5Browser tool catalog — single source of truth for all 42 browser tools.

41 mirror the library's TOOL_SCHEMAS 1:1; `show_preview` is a small project
wrapper over the library's live_preview (floating watch-only window for the UI).
All are CORE PLUGINS: always on, hidden from the UI, found via tool_search.
4 DANGEROUS tools: fill_form, upload_files, export_page, console_command.
In restricted mode the loop asks the user (via question) before running them;
otherwise they run directly.

Library 1.6.0+ ship 42 tools (1.6.0 added `image_search`). That one tool is
deliberately NOT mirrored here: the project's own `image_search` core plugin
(plugins/core_plugins/image_search) already owns that exact name and behavior,
and two tools under one name would collide in the tool registry/dispatcher.
So the catalog stays 41 library tools + show_preview = 42.

Update path: bump lib pin in VERSION + adjust entries here to match the new
TOOL_SCHEMAS (add/remove/rename only what actually changed). Everything else
(dispatcher/registry/builder/store) reads this file.

Each entry mirrors our spec.json shape: {name, description, inputs}.
Inputs shape mirrors our convention: {param: {type, required, description, ...}}.
Common optional params (pre_wait/post_wait) are accepted by tools.py,
so they are NOT repeated here. Tools take no reason parameter at all.
"""
from __future__ import annotations

CORE_TOOLS: tuple[str, ...] = ("open_browser", "navigate", "page_snapshot", "click", "fill", "human_control")

DANGEROUS_TOOLS = frozenset({"fill_form", "upload_files", "export_page", "console_command"})

SPECS: list[dict] = [
    # ---------------- CORE (6: always in context, rest behind tool_search) ----------------
    {
        "name": "page_snapshot",
        "core": True,
        "description": (
            "Numbered map of the page's interactive elements, including modals and iframes. "
            "The agent's eyes: call this first on any page and re-run when the page may have changed. "
            "Every click/fill ref must come from here or find_elements. "
            "mode=changes_only diffs against the last map."
        ),
        "inputs": {
            "scope": {"type": "string", "required": False, "description": "viewport, full, or a ref number to scope around one element. Default viewport."},
            "max_elements": {"type": "integer", "required": False, "description": "Max elements in the map, 1..1000. Default 150."},
            "mode": {"type": "string", "required": False, "description": "full or changes_only. Default full."},
            "include_hidden": {"type": "boolean", "required": False, "description": "Include offscreen elements. Default false."},
        },
    },
    {
        "name": "navigate",
        "core": True,
        "description": (
            "Move in the current tab: open an http(s) URL, or go back, forward, or reload. "
            "Use whenever the user gives a link or the task needs a new page. Waits for load by itself."
        ),
        "inputs": {
            "to": {"type": "string", "required": True, "description": "http(s) URL, or back, forward, reload."},
            "wait_until": {"type": "string", "required": False, "description": "load, stable or none. Default stable."},
            "timeout": {"type": "number", "required": False, "description": "Seconds to wait for load, 1..120. Default 30."},
        },
    },
    {
        "name": "click",
        "core": True,
        "description": (
            "Real mouse press on an element (scrolls to it, checks it is uncovered, clicks the center) "
            "or on viewport x,y coordinates. The agent's hand: ~80% of browsing acts are clicks. "
            "For checkbox/radio/switch pass checked=true/false to set its exact state."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "Target element ref from page_snapshot or find_elements."},
            "x": {"type": "number", "required": False, "description": "Viewport x in px (alternative to ref)."},
            "y": {"type": "number", "required": False, "description": "Viewport y in px (alternative to ref)."},
            "button": {"type": "string", "required": False, "description": "left, right or middle. Default left."},
            "double": {"type": "boolean", "required": False, "description": "Double-click. Default false."},
            "hold_ms": {"type": "number", "required": False, "description": "Sustained press in ms. Default 0."},
            "modifiers": {"type": "list", "required": False, "description": "Keys held with the click: ctrl, shift, alt, meta."},
            "checked": {"type": "boolean", "required": False, "description": "Exact state for checkbox/radio/switch."},
        },
    },
    {
        "name": "fill",
        "core": True,
        "description": (
            "Type into one field: focuses it, clears the old content, writes genuine text, "
            "optionally presses Enter. Use for search boxes, logins and any single input. "
            "For several fields at once use fill_form."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": True, "description": "The field's ref from page_snapshot or find_elements."},
            "text": {"type": "string", "required": True, "description": "Text to type."},
            "clear": {"type": "boolean", "required": False, "description": "Clear old content first. Default true."},
            "submit": {"type": "boolean", "required": False, "description": "Press Enter after typing. Default false."},
            "delay_ms": {"type": "number", "required": False, "description": "Per-character delay in ms. Default 0."},
        },
    },
    # ---------------- DISCOVERABLE (36: behind tool_search) ----------------
    {
        "name": "read_text",
        "core": False,
        "description": (
            "Visible text of the whole page or of one element. Use to read articles, "
            "search results or error messages. Long text is cut with a truncated marker."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "One element only; omit for the whole page."},
            "max_chars": {"type": "integer", "required": False, "description": "Max characters, 1..20000. Default 2000."},
            "include_frames": {"type": "boolean", "required": False, "description": "Include iframe text. Default true."},
        },
    },
    {
        "name": "extract_data",
        "core": False,
        "description": (
            "Table, list, links, forms, images or meta of the page/element as structured JSON. "
            "Use to extract a table or link list instead of reading long text and parsing it yourself."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "One element only; omit for the whole page."},
            "kind": {"type": "string", "required": True, "description": "table, list, links, forms, images or meta."},
            "max_items": {"type": "integer", "required": False, "description": "Max items, 1..500. Default 100."},
        },
    },
    {
        "name": "quick_search",
        "core": False,
        "description": (
            "Fast Google search as organized JSON (title, url, snippet per result). "
            "Reads up to 5 live result pages in a hidden tab that is closed afterwards; "
            "your tabs, refs and map stay untouched. For opening a result use navigate or manage_tabs."
        ),
        "inputs": {
            "query": {"type": "string", "required": True, "description": "Search query, max 500 chars."},
            "pages": {"type": "integer", "required": False, "description": "How many result pages, 1..20. Default 1."},
        },
    },
    {
        "name": "fetch_pages",
        "core": False,
        "description": (
            "Fetch 1..5 page URLs as clean organized Markdown (title plus text, links resolved). "
            "Really visits each page, scrolls for lazy content, then filters nav/cookie/footer noise. "
            "One hidden tab per URL, all at once, each closed afterwards; failures come back per URL."
        ),
        "inputs": {
            "urls": {"type": "list", "required": True, "description": "1..5 http(s) URLs to fetch."},
            "max_chars": {"type": "integer", "required": False, "description": "Markdown cap per page, 500..50000. Default 8000."},
        },
    },
    {
        "name": "find_elements",
        "core": False,
        "description": (
            "Search live elements by name and optional role; returns refs directly so no snapshot "
            "is needed first. Use when you know the button or field name and want to skip a long map, "
            "or to inspect why a click keeps failing."
        ),
        "inputs": {
            "query": {"type": "string", "required": True, "description": "Element name or substring to search for."},
            "role": {"type": "string", "required": False, "description": "button, link, textbox, checkbox, combobox, ..."},
            "match": {"type": "string", "required": False, "description": "contains or exact. Default contains."},
            "in_ref": {"type": "integer", "required": False, "description": "Search inside one element only."},
            "max_results": {"type": "integer", "required": False, "description": "Max hits, 1..50. Default 10."},
        },
    },
    {
        "name": "inspect_element",
        "core": False,
        "description": (
            "Deep diagnosis of one element: type, attributes, state (visible, enabled, checked, value), "
            "box, covering element and HTML. Use when the map is not enough or a click keeps failing."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": True, "description": "Element ref to diagnose."},
            "include": {"type": "list", "required": False, "description": "Subset of attributes, state, box, styles, parents, html."},
            "max_chars": {"type": "integer", "required": False, "description": "Caps HTML chars, 1..20000. Default 3000."},
        },
    },
    {
        "name": "screenshot",
        "core": False,
        "description": (
            "Viewport, element or full-page capture. Use for canvas, nameless icons or visual "
            "confirmation, or to click by coordinates. annotate=true draws ref numbers onto the image."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "One element only; omit for viewport."},
            "full_page": {"type": "boolean", "required": False, "description": "Capture the full page. Default false."},
            "annotate": {"type": "boolean", "required": False, "description": "Draw ref numbers on the image. Default false."},
            "format": {"type": "string", "required": False, "description": "png or jpeg. Default png."},
        },
    },
    {
        "name": "read_markdown",
        "core": False,
        "description": (
            "Full page or element text as organized Markdown (headings, lists, tables, links, form labels). "
            "Text only, no markup; uncapped by default whatever its size."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "One element only; omit for the whole page."},
            "max_chars": {"type": "integer", "required": False, "description": "Cap, 0 = full text, or 500..200000. Default 0."},
        },
    },
    {
        "name": "extract_image",
        "core": False,
        "description": (
            "Original image bytes by element ref or URL (QR codes, photos): the resource itself, "
            "not a re-render. Find the URL with extract_data(kind=\"images\") or read_markdown first. "
            "For on-screen views use screenshot."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "Element ref (exactly one of ref|src)."},
            "src": {"type": "string", "required": False, "description": "Image http(s) or data: URL (exactly one of ref|src)."},
            "max_bytes": {"type": "integer", "required": False, "description": "Max bytes, 1024..8388608. Default 8388608."},
        },
    },
    {
        "name": "get_page_info",
        "core": False,
        "description": (
            "One-glance page/tab state: URL, title, load readiness, control owner, viewport, scroll "
            "position, page height, iframe count, focused element, open popup. Call before acting "
            "to confirm control is still agent. Read-only: answers 'stopped' without starting "
            "the browser when it is down."
        ),
        "inputs": {},
    },
    {
        "name": "wait_for",
        "core": False,
        "description": (
            "Wait until one condition holds before continuing (spinner, late load, result appearing). "
            "On timeout returns the timeout plus the current page state."
        ),
        "inputs": {
            "condition": {"type": "string", "required": True, "description": "text, text_gone, element_visible, element_hidden, url_contains, network_idle, new_tab, download_complete, console_message or seconds."},
            "value": {"type": "string", "required": False, "description": "Value for the condition (text, url part, seconds count)."},
            "timeout": {"type": "number", "required": False, "description": "Seconds to wait, 1..120. Default 10."},
        },
    },
    {
        "name": "click_verify",
        "core": False,
        "description": (
            "Click like click(), then check conditions like verify_state() in one call. Use after "
            "every click that must land somewhere: one roundtrip, one final picture. Fails when the "
            "click fails (verify skipped) or any check fails."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "Target element ref."},
            "x": {"type": "number", "required": False, "description": "Viewport x in px (alternative to ref)."},
            "y": {"type": "number", "required": False, "description": "Viewport y in px (alternative to ref)."},
            "button": {"type": "string", "required": False, "description": "left, right or middle. Default left."},
            "double": {"type": "boolean", "required": False, "description": "Double click. Default false."},
            "hold_ms": {"type": "number", "required": False, "description": "Sustained press in ms, 0..60000. Default 0."},
            "modifiers": {"type": "list", "required": False, "description": "Modifiers held with the click: ctrl, shift, alt, meta."},
            "checked": {"type": "boolean", "required": False, "description": "Exact state for a checkbox/radio/switch."},
            "checks": {"type": "list", "required": True, "description": "Checks in verify_state shape, max 20."},
        },
    },
    {
        "name": "fill_form",
        "core": False,
        "description": (
            "Fill several fields in order in one step, understanding each field type (text, checkbox, "
            "select), optionally pressing a submit button last. Stops at the first failing field and "
            "names it. Needs human approval first when approvals are on."
        ),
        "inputs": {
            "fields": {"type": "list", "required": True, "description": "List of {ref, value}; value is text, true or false. Max 30 fields."},
            "submit_ref": {"type": "integer", "required": False, "description": "Button pressed after the fields."},
        },
    },
    {
        "name": "press_keys",
        "core": False,
        "description": (
            "Press a real key or combo: Enter, Tab, Esc, arrows, shortcuts. "
            "Fails with invalid_key on a bad name."
        ),
        "inputs": {
            "keys": {"type": "string", "required": True, "description": "Key or combo, e.g. Enter, Shift+Tab, Ctrl+A."},
            "repeat": {"type": "integer", "required": False, "description": "Repeat count, 1..20. Default 1."},
            "ref": {"type": "integer", "required": False, "description": "Focus this element first."},
        },
    },
    {
        "name": "scroll",
        "core": False,
        "description": (
            "Scroll the page or a scrollable element with a real wheel, jump to one element "
            "(sticky-header aware), or go to an edge for infinite-scroll pages."
        ),
        "inputs": {
            "direction": {"type": "string", "required": False, "description": "up, down, left or right."},
            "amount": {"type": "number", "required": False, "description": "Pixels per scroll, 1..10000. Default 400."},
            "to_ref": {"type": "integer", "required": False, "description": "Bring this element into view."},
            "to_edge": {"type": "string", "required": False, "description": "top or bottom of the page."},
            "container_ref": {"type": "integer", "required": False, "description": "Scroll inside this element, not the page."},
        },
    },
    {
        "name": "hover",
        "core": False,
        "description": (
            "Move the mouse gradually over an element. Use for hover-only menus and tooltips. "
            "Returns the newly appeared elements."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": False, "description": "Target element ref."},
            "x": {"type": "number", "required": False, "description": "Viewport x in px (alternative to ref)."},
            "y": {"type": "number", "required": False, "description": "Viewport y in px (alternative to ref)."},
            "duration_ms": {"type": "number", "required": False, "description": "Hover settle in ms, 0..10000. Default 300."},
        },
    },
    {
        "name": "hover_click",
        "core": False,
        "description": (
            "Hover a menu to open it, then click its item, in one call. Use for hover-only menus "
            "whose options appear only after hovering. One roundtrip, one final picture."
        ),
        "inputs": {
            "menu_ref": {"type": "integer", "required": False, "description": "Menu to hover."},
            "menu_x": {"type": "number", "required": False, "description": "Menu x in px (alternative)."},
            "menu_y": {"type": "number", "required": False, "description": "Menu y in px (alternative)."},
            "target_ref": {"type": "integer", "required": False, "description": "Item to click after the menu opens."},
            "target_x": {"type": "number", "required": False, "description": "Item x in px (alternative)."},
            "target_y": {"type": "number", "required": False, "description": "Item y in px (alternative)."},
            "duration_ms": {"type": "number", "required": False, "description": "Hover settle in ms, 0..10000. Default 300."},
        },
    },
    {
        "name": "choose_option",
        "core": False,
        "description": (
            "Pick a dropdown value: native select or custom list (opens it and picks by itself). "
            "Verifies the value really changed; fails with select_failed otherwise."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": True, "description": "The dropdown list ref."},
            "value": {"type": "string", "required": False, "description": "Option value (one of value/label/index required)."},
            "label": {"type": "string", "required": False, "description": "Option label (one of value/label/index required)."},
            "index": {"type": "integer", "required": False, "description": "Option index (one of value/label/index required)."},
            "multiple": {"type": "list", "required": False, "description": "Values to select in a multi-select list."},
        },
    },
    {
        "name": "drag_drop",
        "core": False,
        "description": (
            "Drag an element to another spot with gradual moves (sliders, maps). Accepts refs or "
            "coordinates. Ends in different frames fail with invalid_input."
        ),
        "inputs": {
            "from_ref": {"type": "integer", "required": False, "description": "Drag start element."},
            "from_x": {"type": "number", "required": False, "description": "Start x in px (alternative)."},
            "from_y": {"type": "number", "required": False, "description": "Start y in px (alternative)."},
            "to_ref": {"type": "integer", "required": False, "description": "Drop target element."},
            "to_x": {"type": "number", "required": False, "description": "Drop x in px (alternative)."},
            "to_y": {"type": "number", "required": False, "description": "Drop y in px (alternative)."},
            "steps": {"type": "integer", "required": False, "description": "Gradual moves, 1..100. Default 12."},
        },
    },
    {
        "name": "upload_files",
        "core": False,
        "description": (
            "Attach one or more workspace files to a file field without opening the OS picker. "
            "Paths outside the workspace fail with path_forbidden. Needs human approval first "
            "when approvals are on."
        ),
        "inputs": {
            "ref": {"type": "integer", "required": True, "description": "The input[type=file] ref."},
            "paths": {"type": "list", "required": True, "description": "Working-folder file paths, max 10."},
        },
    },
    {
        "name": "manage_tabs",
        "core": False,
        "description": (
            "List tabs, open one, switch between them, or close one. "
            "Use after a new-tab event or when a popup appears."
        ),
        "inputs": {
            "action": {"type": "string", "required": True, "description": "list, open, switch or close."},
            "id": {"type": "string", "required": False, "description": "Tab id from list (required with switch/close)."},
            "url": {"type": "string", "required": False, "description": "URL for open (http(s) only)."},
        },
    },
    {
        "name": "get_events",
        "core": False,
        "description": (
            "Events since last check: new tab, open JS dialog, permission request, file chooser, "
            "started download, navigation, page crash. Use when an act gives an unexpected result "
            "or any press may have opened something."
        ),
        "inputs": {
            "types": {"type": "list", "required": False, "description": "Filter: new_tab, dialog_open, permission_request, file_chooser, download_started, navigation, page_crash."},
            "since_last": {"type": "boolean", "required": False, "description": "Only new events. Default true."},
        },
    },
    {
        "name": "answer_popup",
        "core": False,
        "description": (
            "Answer a JS dialog (alert/confirm/prompt), a permission request "
            "(camera/location/notifications), or a file chooser. Answer first: every other page act "
            "stalls while one is open."
        ),
        "inputs": {
            "kind": {"type": "string", "required": True, "description": "dialog, permission or file_chooser."},
            "accept": {"type": "boolean", "required": True, "description": "Allow/confirm (true) vs deny/dismiss (false)."},
            "text": {"type": "string", "required": False, "description": "Prompt text for prompt dialogs."},
            "name": {"type": "string", "required": False, "description": "Permission name."},
            "origin": {"type": "string", "required": False, "description": "Permission origin."},
            "paths": {"type": "list", "required": False, "description": "Working-folder paths for file_chooser."},
        },
    },
    {
        "name": "manage_downloads",
        "core": False,
        "description": (
            "List downloads with state/size/path, wait for one to finish, or cancel a running one. "
            "Use after pressing a download link."
        ),
        "inputs": {
            "action": {"type": "string", "required": True, "description": "list, wait or cancel."},
            "id": {"type": "string", "required": False, "description": "One download id."},
            "timeout": {"type": "number", "required": False, "description": "Wait seconds, 1..300. Default 30."},
        },
    },
    {
        "name": "read_console",
        "core": False,
        "description": (
            "Console output (log/info/warn/error plus uncaught exceptions); the last 500 messages are "
            "captured automatically from page load. Use when a button does nothing, a page is blank, "
            "or a form never submits."
        ),
        "inputs": {
            "action": {"type": "string", "required": False, "description": "get or clear. Default get."},
            "level": {"type": "string", "required": False, "description": "all, error, warn, info, log, debug or exception."},
            "contains": {"type": "string", "required": False, "description": "Filter inside the message."},
            "since": {"type": "string", "required": False, "description": "last_action or a message id: only newer messages."},
            "max_items": {"type": "integer", "required": False, "description": "Max messages, 1..200. Default 50."},
            "include_stack": {"type": "boolean", "required": False, "description": "Include stack traces. Default false."},
        },
    },
    {
        "name": "console_command",
        "core": False,
        "description": (
            "Run one JavaScript command in the page console and answer its value (define/spy/read a "
            "variable, last-resort fixes). Real input events stay the way to drive a page. "
            "Needs human approval first when approvals are on."
        ),
        "inputs": {
            "script": {"type": "string", "required": True, "description": "JS expression or statements, max 5000 chars."},
            "ref": {"type": "integer", "required": False, "description": "Run it with the element as this."},
            "timeout": {"type": "number", "required": False, "description": "Seconds, 1..60. Default 10."},
        },
    },
    {
        "name": "list_requests",
        "core": False,
        "description": (
            "Network capture is always on: one line per request (method, URL, status, type, size, "
            "duration, id). Pass id to get one request in full detail. Use to check network requests."
        ),
        "inputs": {
            "url_contains": {"type": "string", "required": False, "description": "Filter by URL substring."},
            "method": {"type": "string", "required": False, "description": "GET, POST, ..."},
            "status": {"type": "string", "required": False, "description": "200, or 4xx/5xx range."},
            "resource_type": {"type": "string", "required": False, "description": "document, xhr, fetch, script, image or websocket."},
            "failed_only": {"type": "boolean", "required": False, "description": "Only failed requests. Default false."},
            "since_last_action": {"type": "boolean", "required": False, "description": "Only requests since the last action. Default false."},
            "max_items": {"type": "integer", "required": False, "description": "Max rows, 1..200. Default 30."},
            "id": {"type": "string", "required": False, "description": "One request id for full detail."},
            "parts": {"type": "list", "required": False, "description": "With id: request_headers, request_body, response_headers, response_body, timing, redirects, initiator."},
            "max_chars": {"type": "integer", "required": False, "description": "Chars per detail part, 1..20000. Default 4000."},
        },
    },
    {
        "name": "export_page",
        "core": False,
        "description": (
            "Save the current page as PDF, MHTML or HTML inside the workspace. Use to keep proof, "
            "a report or a page copy. Needs human approval first when approvals are on."
        ),
        "inputs": {
            "format": {"type": "string", "required": True, "description": "pdf, mhtml or html."},
            "path": {"type": "string", "required": True, "description": "Working-folder path to save to."},
        },
    },
    {
        "name": "verify_state",
        "core": False,
        "description": (
            "Check several conditions at once and get pass/fail per check. Cheaper and more precise "
            "than a screenshot or snapshot for confirming your last act worked."
        ),
        "inputs": {
            "checks": {"type": "list", "required": True, "description": "Max 20 checks: {type: text_present|text_absent|url_contains|title_contains|element_exists|element_state, value|ref, ...}."},
        },
    },
    {
        "name": "batch_actions",
        "core": False,
        "description": (
            "Run several navigation/interaction tools in one call, stopping at the first error. Use "
            "for known, safe step chains to save loop cycles. Keep page-changing steps last: refs "
            "must stay valid."
        ),
        "inputs": {
            "actions": {"type": "list", "required": True, "description": "Max 20 steps: each {tool, args} with a known browser tool name."},
            "stop_on_error": {"type": "boolean", "required": False, "description": "Stop at first error. Default true."},
        },
    },
    {
        "name": "manage_triggers",
        "core": False,
        "description": (
            "List, start, update, or stop page-monitor triggers. Each trigger watches its own "
            "hidden tab on its own cadence while you work; it fires once and dies. "
            "list shows every running trigger; update edits one."
        ),
        "inputs": {
            "action": {"type": "string", "required": True, "description": "list, start, update or stop."},
            "trigger_id": {"type": "string", "required": False, "description": "Trigger id (required with update/stop)."},
            "url": {"type": "string", "required": False, "description": "http(s) site to watch (required with start)."},
            "kind": {"type": "string", "required": False, "description": "Single condition kind (with start)."},
            "value": {"type": "string", "required": False, "description": "Single condition value (with start)."},
            "match": {"type": "string", "required": False, "description": "contains or exact (with start)."},
            "conditions": {"type": "list", "required": False, "description": "OR conditions [{kind, value}] (with start)."},
            "add_conditions": {"type": "list", "required": False, "description": "More OR events (with update)."},
            "timeout_s": {"type": "number", "required": False, "description": "Seconds, 0 = persist."},
            "check_interval_s": {"type": "number", "required": False, "description": "Checks cadence 1..30. Default 5."},
            "refresh_each_cycle": {"type": "boolean", "required": False, "description": "Reload before each check."},
            "reason": {"type": "string", "required": False, "description": "Why this trigger."},
        },
    },
    {
        "name": "open_browser",
        "core": True,
        "description": (
            "Start the hidden Chrome, or answer that it already runs plus its state. "
            "This is the ONLY tool that starts the browser: every other tool fails "
            "with 'browser stopped' while it is down and never starts it by itself. "
            "Close it with close_browser when the user asks."
        ),
        "inputs": {},
    },
    {
        "name": "close_browser",
        "core": False,
        "description": (
            "Close the browser quietly and keep its data (profile, cookies, tabs on disk). The live "
            "link of this session dies with it: it answers Not Connect, and the next tool call starts "
            "a new session with a new link."
        ),
        "inputs": {},
    },
    {
        "name": "restart_browser",
        "core": False,
        "description": (
            "Close and start the browser again on the same profile (same tabs data kept on disk). "
            "Answers state only, no picture. Idempotent: restarting a closed browser simply starts it. "
            "Live links are renewed, so the UI shows a fresh preview automatically."
        ),
        "inputs": {},
    },
    {
        "name": "browser_status",
        "core": False,
        "description": (
            "Report the browser state without starting anything: running or closed, uptime, "
            "who owns control, and the current live links. Read-only: answers 'stopped' without "
            "starting the browser when it is down."
        ),
        "inputs": {},
    },
    {
        "name": "human_control",
        "core": True,
        "description": (
            "Hand the SAME live browser to the user and answer the control link + JSON "
            "(does not block). The only handoff tool: the user returns control from "
            "the UI when done, then the agent continues. Use when the user must type or click."
        ),
        "inputs": {
            "expects": {"type": "string", "required": False, "description": "captcha, 2fa, credentials or other."},
        },
    },
    {
        "name": "live_preview",
        "core": False,
        "description": (
            "Watch-only live link (no remote input at all) + JSON, for a monitor screen or a "
            "read-only embed. Use to let the user watch without touching."
        ),
        "inputs": {},
    },
    {
        "name": "show_preview",
        "core": False,
        "description": (
            "Show the browser to the user in the floating watch-only preview window "
            "(no control handed over) and answer its link. Call it when the user should "
            "watch what you are doing. Idempotent: if that same preview is already on "
            "screen nothing reopens."
        ),
        "inputs": {},
    },
]

DISCOVERABLE_TOOLS = tuple(s["name"] for s in SPECS if not s.get("core"))

ALL_TOOLS = tuple(s["name"] for s in SPECS)


def get_spec(name: str) -> dict | None:
    """Spec dict for one browser tool, or None when unknown."""
    for spec in SPECS:
        if spec.get("name") == name:
            return spec
    return None


def is_core(name: str) -> bool:
    return name in CORE_TOOLS


def is_dangerous(name: str) -> bool:
    return name in DANGEROUS_TOOLS
