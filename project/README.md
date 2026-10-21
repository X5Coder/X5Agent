# X5Agent

---

## Requirements

- **Python 3.10+** (3.12 recommended for full browser support — `x5browser==1.5.2`
  runs on 3.10+; the browser check is the source of truth).
- Linux / Mac / Windows + `pip`.
- TCP port **7777** reachable on the machine (the installer tries to open
  it via `ufw`/`firewalld` automatically; otherwise run manually:
  `sudo ufw allow 7777/tcp`).

---

## Setup

**Linux / Mac**

```bash
git clone https://github.com/X5Coder/X5Agent.git
cd X5Agent/project
python3.12 -m pip install -e .
python3.12 install.py              # one command: checks + updates + owner + console URL
python3.12 -m web.server.server   # start the console (reachable at <this-machine-ip>:7777/)
```

**Windows**

```powershell
git clone https://github.com/X5Coder/X5Agent.git
Set-Location X5Agent/project
py -3.12 -m pip install -e .
py -3.12 install.py              # one command: checks + updates + owner + console URL
py -3.12 -m web.server.server   # start the console (reachable at <this-machine-ip>:7777/)
```

The console binds every interface and is reachable at
`http://127.0.0.1:7777/` on the machine itself and at
`http://<this-machine-ip>:7777/` from anywhere that can route to it. The
owner login (email + password, created once) is the only gate, so keep the
port firewalled to who should reach it.

### Non-interactive install (automation)

```bash
# Everything answered by flags — no prompts, no stdin injection:
python install.py --email owner@gmail.com --password S3cret123

# Same via environment (password safer here than on the CLI):
X5_EMAIL=owner@gmail.com X5_PASSWORD=S3cret123 python install.py --yes
```

| Flag | Effect |
| --- | --- |
| `--email`, `--password` (or `X5_EMAIL`/`X5_PASSWORD`) | Owner account without prompting (English Gmail address + min 8 English characters) |
| `--yes` / `-y` | Assume yes to optional prompts (Chrome install) |
| `--skip-browser` | Skip the browser doctor check |
| `--skip-firewall` | Skip the firewall port step |
| `--no-open-browser` | Do not open the console URL at the end |

### Optional: HF_TOKEN (faster embedding download on slow networks)

The embedding model (~1.2 GB) downloads anonymously by default — fine on
normal networks. On slow or rate-limited networks, an optional
HuggingFace token raises the rate limits:

```bash
HF_TOKEN=hf_xxx python install.py --email owner@mail.com --password S3cret123
```

The token is used **only** for the download (never stored, never logged).

### Running the console (scripts)

```bash
./scripts/x5-local    # start the console on this machine (address <ip>:7777)
```

As a systemd unit (edit `User=` + `WorkingDirectory=` first):

```bash
sudo cp scripts/x5console.service /etc/systemd/system/
sudo systemctl enable --now x5console
```

---

## Commands

```bash
python main.py
```

The bundled `embedding_model/` folder (~1.2 GB) downloads automatically
on first run (deltas only, resume on restart) and is used offline after.

---

## License

The agent runs only with a valid license. Open **Settings → License** in the
console, sign in with your subscription account, and the subscription allows
this device to run the agent. No account yet? Use **Create account** in the
same card — the account and its 7-day trial start immediately (no email
confirmation step). One account is active on one device at a time. The license
is checked at startup and every 10 minutes, and the card shows the state,
plan, remaining time and the exact blocking reason.

**Subscription page** (the nav entry directly above Settings) shows the current
plan and sells the paid plans. Right after **Create account** it opens itself
with a "Step 2 of 2" banner so the plan step is the next thing you see; paying
opens the provider page in a new tab and the page follows the payment until the
plan is live. The provider is behind the license server, so the console only
asks for a checkout session — today that is the sandbox provider (charges
nothing, stores no card data), later the same flow runs against a real provider.
Buying the same plan again extends it; a different plan replaces it.

- **Server URL:** `X5_LICENSE_SERVER_URL` (defaults to the current test
  deployment). Use an `https://` endpoint in production.
- **Plain HTTP:** allowed for the current test deployment only. Set
  `X5_LICENSE_ALLOW_HTTP=0` once TLS is in place; the client then refuses
  `http://` so a password or token is never sent in clear text.
- **State:** `X5Agent/Data/database/license.json` holds the refresh token.
  Treat it as a secret and never copy it between machines.
- **Tests:** `X5_LICENSE_BYPASS=1` is honored only inside an active pytest
  run, so it cannot disable licensing in production.

The console **owner** login (the gate for the web page itself) is separate
from the license account. It is set during install; to replace it, run
`python scripts/set_owner.py` on the machine (interactive, or
`X5_EMAIL`/`X5_PASSWORD`). The console no longer exposes a password-change
form.

The central license server (FastAPI + PostgreSQL) is a separate deployment —
see `license-server/README.md` at the repository root.

---

## Models

The only built-in model is the hidden embedding model:

```
embedding_model/  (bundled embedding model, ~1.2 GB, offline, GPU auto / CPU fallback)
```

Chat, vision, video, image, speech-to-text and text-to-speech models are NOT
shipped: connect any of them in the console under **AI Models**
(6 fixed templates, same 4 fields each, universal Connection JSON).
