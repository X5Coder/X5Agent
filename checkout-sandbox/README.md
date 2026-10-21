# Sandbox checkout — the test payment provider

A tiny, separate web service that stands in for a real payment provider
(Dodo) so the whole purchase flow can be exercised end to end before any
real money is involved. It charges nothing, stores no card data and has no
database: a payment is one POST that tells the license server "this session
is paid", authenticated with a shared secret.

```
console  ──POST /api/license/checkout──▶  license server
                                               │ creates a pending row
                                        ◀──────┘ returns  /pay/<session>
browser  ──opens──▶  sandbox :8789/pay/<session>   (plan, amount, Pay button)
sandbox  ──POST /v1/sandbox/pay (X-Sandbox-Secret)──▶ license server
                                               │ marks paid, grants the plan
console  ──polls /api/license/checkout?session=…──▶ paid → plan is live
```

## Configuration

| env | meaning |
| --- | --- |
| `LICENSE_API_URL` | base URL of the license server (default `http://127.0.0.1:8788`) |
| `SANDBOX_SHARED_SECRET` | **required**; must equal the license server's `SANDBOX_SHARED_SECRET` |
| `PORT` | listen port (the systemd unit passes `--port 8789` explicitly) |

On the license server side the matching settings are:

```
CHECKOUT_PROVIDER=sandbox
SANDBOX_SHARED_SECRET=<same value>
SANDBOX_PUBLIC_URL=http://<public-host>:8789
CHECKOUT_TTL_SECONDS=1800
CHECKOUT_RETURN_URL=http://<console-host>:7777/subscription   # fallback only
```

`CHECKOUT_RETURN_URL` is only a fallback: the console sends its own
`return_url` with every checkout (origin/host based), so the "Return to
X5Agent" button lands on the console the purchase was started from.

## Run

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8789
```

Production layout: `/opt/x5checkout` with `.env` (mode 600), the venv beside
it and `deploy/x5checkout.service` installed as `x5checkout.service`
(`User=x5checkout`, `ProtectSystem=strict`, only its own folder writable).
Open TCP 8789 in the firewall — the console and the buyer's browser both
need it.

## Routes

| route | purpose |
| --- | --- |
| `GET /health` | liveness |
| `GET /` | short explanation page |
| `GET /pay/{session}` | the fake payment page for one session (plan, amount, expiry) |
| `POST /pay/{session}` | completes it, then shows "Payment complete" |

Sessions are validated against the license server (never trusted from the
URL), `session` must match `^[0-9a-fA-F-]{8,64}$`, every server call is
bounded (10 s timeout, 64 KB response cap, at most 3 retries) and every
failure renders a readable page instead of a stack trace. Pages are dark,
tokens-only and escape all values they print.

## Tests

```bash
python -m pytest -q          # 10 passed, no network, no license server needed
```

## Replacing it with Dodo

Only two things are provider-specific:

1. `POST /v1/billing/checkout` in the license server returns `checkout_url`
   — for Dodo this becomes the hosted checkout link for the created session.
2. `POST /v1/sandbox/pay` is the callback: Dodo's webhook (signature
   verified) calls the same `billing.complete_checkout()`.

The console, the client and the database schema do not change.
