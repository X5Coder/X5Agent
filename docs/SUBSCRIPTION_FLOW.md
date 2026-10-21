# Subscription, licensing and admin — what was built

Answers first (short). Details below, then the one-command install.

| Question | Answer |
| --- | --- |
| Does the client send the aggregated device fingerprint when the account is **created**? | **Yes.** Signup, sign-in and every heartbeat send the machine fingerprint plus the labelled device details. |
| Did we implement every part we agreed on? | **Yes** for the whole client flow (signup → plan → console → subscription → renewal/upgrade → dashboard bar → gates → admin panel). Three items are deliberately out of this round: the real Dodo checkout, TLS/domain and admin 2FA/audit log. |
| Is the current result what we wanted? | **Yes** — the flow works end to end on the live server with the sandbox provider. It is not "finished forever": the three items above are the remaining phases. |

## 1. Device fingerprint — exactly what leaves the machine

Source: `project/core/licensing/device.py` + `project/core/licensing/client.py`.

- `machine_fingerprint()` → one **SHA-256 hex string (64 chars)**, built from `MachineGuid` (Windows) / `/etc/machine-id` (Linux) / `IOPlatformUUID` (macOS) plus `uuid.getnode()`, platform, machine and hostname. It is cached per process.
- `fingerprint_details()` → the same inputs as **labelled fields** (`machine_id`, `node_id`, `hostname`, `system`, `machine`, `platform`, `python`) so an admin can identify a device instead of guessing at a hash.
- Both travel on **account creation** (`register`), **sign-in** (`login`) and every **heartbeat** (`/v1/heartbeat`).
- Server side: `app/licensing.py:update_device_details()` stores them as **bounded JSON (≤ 2048 chars)** on `Device.details_json` — never raw hardware data, never unlimited text.
- Nothing else is collected: no serial numbers, no installed software, no files, no screen contents.

## 2. Implemented checklist (agreed scope)

Onboarding and plans
- [x] **One account**: the console's first-run setup creates the central license account first. A refused signup answers `502` with a readable line and writes **nothing** locally.
- [x] First run is gated: signed in with no plan → every page 302s to `/plans.html`; APIs stay open so the page can work.
- [x] Plan page: no sidebar, no email bar, no technical step label, exact-size skeleton cards (`.plan.is-skel`), cards rise in once, `[hidden]` really hides.
- [x] Catalogue is **Free / Pro / Pro Plus / Ultra** ($0 / $10 / $25 / $75); every card carries the same six features, the "Most popular" badge sits on the plan the admin marks, and longer plans print their saving against the monthly price ("Save $5.00 (17%)"). Names, prices, features and the badge are all edited in the admin panel — nothing is hardcoded.
- [x] Trial is a normal plan; paid plans cheapest-first; **Recommended** on the cheapest paid plan; one column on phones.
- [x] Any pick POSTs `/api/onboarding/choose` and opens the console at once.

Purchases (provider-agnostic)
- [x] `POST /v1/billing/checkout` and `POST /v1/billing/choose` return `{checkout_url | instant, license}`.
- [x] **Sandbox provider completes the purchase server-side** (`billing.auto_complete`): the client sees `instant: true`, `paid: true`, no payment page, no waiting. `checkout-sandbox/` stays the Dodo stand-in.
- [x] Real provider path (link only): `checkout_url` is opened in the same tab, the console marks the pending session and confirms it once on return — no polling loop.
- [x] **Same plan again = renewal** (days are added, paid time is never lost).
- [x] **Upgrade = stacking**: remaining days are kept and the new plan's full duration is added.
- [x] **No downgrade**: a free/trial pick is refused with `409 plan_downgrade_refused` while a paid subscription is active or warned.
- [x] Completion stamps `Account.plan_chosen_at`, so the onboarding gate closes exactly once.

Console side
- [x] Subscription page replaces the old Settings card: current plan, live time left, in-page account form (create / sign-in), the identical plan cards as `/plans.html`, in-place update after payment.
- [x] Dashboard **plan bar**: plan name, live countdown, period progress (`aria-valuenow`), start/end dates, red when ended, amber when it needs attention, real spacing above the charts.
- [x] `x5:license-changed` keeps the bar in sync after every payment/login.
- [x] The license gate locks the console (nav intercepted, only Subscription/Settings reachable) while `can_work` is false — fail-closed.
- [x] Server notices point at the **Subscription page** (never a removed Settings card).
- [x] "Connect a model" button appears in chat when a model is missing, instead of a dead error line.
- [x] `My Subscriptions` shortcut is a glass button with an icon, hint line and a Soon badge (no dead link).
- [x] Subscription cards render **once** per real change (no replaying entrance animation).

License server
- [x] Ed25519 15-minute entitlement tokens, pinned public key, last-device-wins binding, heartbeat 600s, notices as fixed kinds.
- [x] Plans CRUD, accounts search/detail, warn / clear / suspend / extend / activate, stats, billing snapshot.
- [x] Rate limits per address (signup/login 10/min, admin login 5/min, heartbeat 60/min, admin API 120/min, checkout 10/min).
- [x] Admin panel: one HttpOnly `x5admin` cookie session (`SameSite=Strict`, `ADMIN_SESSION_SECONDS` default 7 days), `GET /v1/admin/session` on boot, `POST /v1/admin/logout` — a refresh or a server restart never asks for the password again. Bearer tokens still work for API clients.

Deliberately **not** in this round
- [ ] Real Dodo checkout + webhook (needs the rotated key and API docs; only the checkout link and callback change).
- [ ] TLS/domain (today plain HTTP, so the admin cookie is not `Secure`).
- [ ] Admin 2FA + audit log, trusted-proxy handling.
- [ ] SMTP (out of scope by design: signup is self-activating, no email step).

## 2b. Hostnames

The central license server is **not** on a hand-managed VPS any more: it runs as
a VPS Manager Room (`x5-license`, 5 GB quota, single container, port 8000 inside)
with its data in the Room volume (`/app/data/license.db` + `/app/data/keys`).
The product console is installed on the **user's own machine** - nothing of
X5Agent lives on the VPS.

| What | Address | Serves |
|---|---|---|
| The central license server | `https://dex.awnlearn.com` | `license-server` FastAPI in the Room (`/` -> 307 -> `/admin` panel) |
| The product console | `http://127.0.0.1:7777` (the user's machine) | `project/web` console (agent, plans page, studio) |
| The gated browser viewer | `http://<machine>:8790` (or `X5_LIVE_PUBLIC_URL` when a public host exists) | `X5Browser/liveviewer.py` |

- `dex.awnlearn.com` is the **only** public entry point, and it serves the admin
  panel of the central server and nothing else.
- The domain is connected through the VPS Manager (`connect_domain`), which owns
  the nginx vhost and reuses the live Let's Encrypt certificate. Do not hand-edit
  a `dex.awnlearn.com` vhost any more: the manager rewrites it on deploy.
- The client default is `DEFAULT_SERVER_URL = "https://dex.awnlearn.com"`
  (`project/core/licensing/config.py`); `X5_LICENSE_SERVER_URL` still overrides it.
- Room environment (`.env` in the Room project, applied by the manager):
  `ENVIRONMENT=production`, `DATABASE_URL=sqlite:////app/data/license.db`,
  `KEYS_DIR=/app/data/keys`, `CHECKOUT_PROVIDER=sandbox`,
  `SANDBOX_PUBLIC_URL=https://dex.awnlearn.com`, plus `JWT_SECRET` and
  `SANDBOX_SHARED_SECRET` (generated, never in the repo).
- The Ed25519 key pair in the Room volume is the **same** pair the shipped client
  pins (`ENTITLEMENT_PUBLIC_KEY_PEM`). Replacing it invalidates every issued
  entitlement token until the client rebuilds with the new public key.

## 3. One command on any device

**Linux / macOS / VPS (recommended)** — installs, updates and starts the console persistently on port 7777 (safe to close SSH).

The repository is **private**, so the one command needs a read-only GitHub token (fine-grained, `Contents: Read` on `X5Coder/X5Agent`). The token is used for one clone/pull and then removed from the git remote, so it is never written to `.git/config`:

```bash
X5_INSTALL_TOKEN=<github-token> bash setup.sh
```

With the owner account preset (no prompt; env vars, so the password is not in the process list):

```bash
X5_INSTALL_TOKEN=<github-token> X5_EMAIL=owner@gmail.com X5_PASSWORD='StrongPass123' bash setup.sh
```

Get `setup.sh` once over `scp` (it is a single file in the repo root), or host it on the main site and keep the same shape:

```bash
X5_INSTALL_TOKEN=<github-token> curl -fsSL https://x5agent.com/setup.sh | bash
```

Without a token the script still tries the public URL, so a public mirror works unchanged. Once the site is live, the token can also be exchanged for a short-lived download link there — the script itself needs no change.

Re-running the same command is the update path: `git pull --ff-only`, data untouched, old server stopped, new one started on the same port. Stop with `x5agent --off`.

**Docker (any OS with Docker Desktop/Engine)** — same private-repo rule:

```bash
git clone https://x-access-token:<github-token>@github.com/X5Coder/X5Agent.git X5Agent
cd X5Agent && git remote set-url origin https://github.com/X5Coder/X5Agent.git
docker compose up --build
```

Data lives in the `x5-data` volume and the embedding weights in `x5-models`, so a rebuild never loses anything.

**Windows (native, no Docker)**: install Python 3.12 + Git, then

```powershell
git clone https://x-access-token:<github-token>@github.com/X5Coder/X5Agent.git
cd X5Agent; git remote set-url origin https://github.com/X5Coder/X5Agent.git
cd project; python install.py --run
```

After it starts, the console is at `http://<machine-ip>:7777` and the owner login is the only gate.
