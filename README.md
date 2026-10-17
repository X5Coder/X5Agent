# X5Agent — v0.16.0

Professional agent with one built-in model (embedding) plus universal
connections to any AI model through **AI Models** templates.

---

## Setup — unified install / update command (v0.16.0)

**One command installs a fresh copy OR updates an existing one** (same command,
safe to re-run — source only via `git pull --ff-only`, `X5Agent/Data` untouched):

```bash
curl -fsSL https://raw.githubusercontent.com/X5Coder/X5Agent/main/setup.sh | bash
```

Verify after update: open Settings → Updates (must show `0.16.0`) or check
`updates.json` → `"version": "0.16.0". Then hard-refresh the console page
(Ctrl+Shift+R) so the new `live-url.js`/`chat.js` take effect.

**Requirements: Python 3.10+** (full browser support — `x5browser==1.6.2`
runs on 3.10+, 3.12 recommended), TCP port **7777** (the installer opens it, plus the gated live-viewer port 8790, via `ufw`/`firewalld`
automatically; else `sudo ufw allow 7777/tcp && sudo ufw allow 8790/tcp`).

**One command (Linux / Mac, fresh VPS)**

```bash
curl -fsSL https://raw.githubusercontent.com/X5Coder/X5Agent/main/setup.sh | bash
```

Non-interactive (email/password passed through to the installer):

```bash
curl -fsSL https://raw.githubusercontent.com/X5Coder/X5Agent/main/setup.sh | bash -s -- --email owner@mail.com --password S3cret123
```

What the one-liner does: clones (or fast-forward updates) the repo into
`~/X5Agent`, auto-installs dependencies (`pip install -e .` inside
`install.py` when missing), runs the full installer (env + browser doctor +
owner once + firewall), then starts the console **persistently** (detached —
safe to close SSH). The console binds every interface, so it is reachable on
the host itself at `http://127.0.0.1:7777/` and from anywhere that routes to
the host at `http://<host-ip>:7777/` (open port 7777 in the firewall). The
owner login is the only gate.
Re-running it updates the source (`git pull --ff-only`, data untouched),
kills the old server and starts a fresh one on the same port.

**Manual (Linux / Mac)**

```bash
git clone https://github.com/X5Coder/X5Agent.git
cd X5Agent/project
python3.12 install.py --run
```

`install.py --run` = install + start in one step (dependencies auto-install,
server binds every interface on port 7777 — reachable here and at the host's
own address; the owner login is the only gate, so firewall the port to who
should reach it).
The owner email + password are asked only once (saved locally, skipped on
later runs). Every browser visit lands on the login page until the owner
signs in (session cookie kept after that).
Without `--run` it only installs; then start manually with `x5agent`.

**Start directly (from anywhere, no update check)**

```bash
x5agent              # persistent serve on port 7777 (survives SSH close)
x5agent --off        # stop all project servers on the port
```

The `x5agent` command is installed on PATH by `pip install -e .`
(the installer adds it automatically when missing). It always runs
**detached** (new session + log in `X5Agent/Data/logs/console.log`), so
closing the terminal/SSH never stops it. Restart semantics: the console
port is always freed first — ANY stale holder on it is stopped (graceful
SIGINT, then forced SIGKILL; suspended ones resumed first) before the fresh
server binds, so exactly one server ends up running. `x5agent --off`
stops every project server on the port (`x5agent-off` alias does the same).

**Windows**

```powershell
git clone https://github.com/X5Coder/X5Agent.git
Set-Location X5Agent/project
py -3.12 -m pip install -e .
py -3.12 install.py              # one command: checks + updates + owner + console URL
py -3.12 -m web.server.server   # start the console (reachable at <this-machine-ip>:7777/)
```

Non-interactive: `python install.py --email owner@gmail.com --password S3cret123`
(or `X5_EMAIL`/`X5_PASSWORD` env). The owner email must be an English Gmail
address and the password min 8 English (ASCII) characters. Optional
`HF_TOKEN=hf_xxx` only raises HuggingFace download rate limits on slow
networks — never required.
Start helper: `scripts/x5-local` (systemd: `scripts/x5console.service`).

The bundled `embedding_model/` folder (~1.2 GB) ships with the project
copy (release zip) and is used offline - nothing is downloaded on first run
(GPU is used automatically when available, else CPU).

---

## Models

| Model | Path | Size |
| --- | --- | --- |
| Embedding (built-in, hidden) | `embedding_model/` | 1.15 GB |

Chat, vision, video, image, speech-to-text and text-to-speech are NOT shipped:
connect any of them in the console under **AI Models** (6 fixed templates,
same 4 fields each, universal Connection JSON).

---

## Commands

```bash
python main.py
```

---

## Structure

```
project/
├── main.py
├── embedding_model/      # bundled embedding weights (offline, ships with release zip)
├── core/
│   ├── embedding/        # built-in embedding bootstrap + service
│   └── ai_models/        # universal connection engine (5 templates)
```
