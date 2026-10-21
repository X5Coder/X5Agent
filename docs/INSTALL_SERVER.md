# Installing X5Agent on an Ubuntu server

Two ways: the **compiled release** (no source on the machine, recommended for a
server) or **from source** (private repo, needs a token).

The compiled archives are tied to one Python ABI, so pick the archive that
matches the server's interpreter:

| Server | `python3 -V` | Archive |
|---|---|---|
| Ubuntu 22.04 LTS | 3.10 | `x5agent-linux-x64-ubuntu22.04.zip` |
| Ubuntu 24.04 LTS | 3.12 | `x5agent-linux-x64-ubuntu24.04.zip` |
| Ubuntu 24.04, no console | 3.12 | `x5agent-linux-x64-ubuntu24.04-headless.zip` |

The `headless` archive has no `web/` folder: `launch.pyc` starts the terminal
agent loop instead of the console.

---

## 1. Compiled release

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip unzip

mkdir -p ~/x5agent && cd ~/x5agent
unzip x5agent-linux-x64-ubuntu24.04.zip     # the file you downloaded
cd x5agent-linux-x86_64-full                # the folder inside the archive

python3 -m venv .venv                       # must be the same 3.10 / 3.12 as the build
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt             # ~2 GB, one time

PYTHONNOUSERSITE=1 python launch.pyc        # console on 0.0.0.0:7777
```

Open `http://<server-ip>:7777/`. The first visit asks you to create the owner
account; the same form creates the account on the central license server
(`https://dex.awnlearn.com`) and then sends you to the plan page.

`X5_PORT=9000 python launch.pyc` changes the port. User data is written to
`~/x5agent/x5agent-linux-x86_64-full/X5Agent/` (Workspace + Data) — that folder
is the whole backup.

### Keep it running with systemd

```bash
sudo useradd -r -m -d /opt/x5agent -s /bin/bash x5agent   # once
sudo rsync -a ~/x5agent/x5agent-linux-x86_64-full/ /opt/x5agent/app/
sudo tee /etc/systemd/system/x5agent.service >/dev/null <<'UNIT'
[Unit]
Description=X5Agent console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=x5agent
WorkingDirectory=/opt/x5agent/app
Environment=PYTHONNOUSERSITE=1
Environment=X5_PORT=7777
ExecStart=/opt/x5agent/app/.venv/bin/python launch.pyc
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now x5agent
sudo systemctl status x5agent --no-pager
```

Logs: `journalctl -u x5agent -f` (the console also writes `console.log` in its
own folder).

### Firewall

```bash
sudo ufw allow 7777/tcp     # console
sudo ufw allow 8790/tcp     # live browser viewer (only if you use it remotely)
```

Put it behind a reverse proxy with TLS if it must be reachable from the
internet. The console binds `0.0.0.0`; the owner login is the only gate, so
plain HTTP on a public IP exposes the password — terminate TLS first.

---

## 2. From source (private repository)

```bash
sudo apt install -y git python3 python3-venv python3-pip
git clone https://<your-token>@github.com/X5Coder/X5Agent.git
cd X5Agent
python3 project/install.py --run
```

`install.py` installs the dependencies, syncs the bundled embedding model,
checks the browser stack, asks once for the owner e-mail + password and starts
the console detached on port 7777. It never touches `X5Agent/Data`, so it is
also the update command (`git pull --ff-only` + re-run).

---

## Updating a compiled install

Download the newer archive, stop the service, unpack over the app folder
**without deleting `X5Agent/`**, reinstall requirements if the pin list changed,
start again:

```bash
sudo systemctl stop x5agent
cd /opt/x5agent && sudo mv app app.old && sudo mkdir app
sudo unzip ~/x5agent-linux-x64-ubuntu24.04-<new>.zip -d /tmp/x5new
sudo rsync -a /tmp/x5new/x5agent-linux-x86_64-full/ /opt/x5agent/app/
sudo rsync -a /opt/x5agent/app.old/X5Agent/ /opt/x5agent/app/X5Agent/   # user data
sudo /opt/x5agent/app/.venv/bin/pip install -r /opt/x5agent/app/requirements.txt
sudo systemctl start x5agent
```
