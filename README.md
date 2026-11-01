# Payment Orchestrator

Payment Orchestrator is an educational simulation of payment routing, transaction state, provider callbacks, durable work, and reconciliation. It uses synthetic data and simulated providers only.

> **Not a payment processor.** Do not use this project for real transactions or enter card numbers, CVVs, credentials, or other sensitive payment data. It is not production-ready and makes no PCI compliance claim.

## What it demonstrates

- FastAPI APIs with PostgreSQL as the authoritative store.
- Database-enforced merchant-scoped idempotency and atomic creation of payment and processing work.
- A centralized state machine with persistent transition history and terminal-state protection.
- Two deterministic simulated providers with success, decline, timeout, lost-response, and temporary-unavailability scenarios.
- A PostgreSQL durable job queue using leases and `FOR UPDATE SKIP LOCKED`; no Redis, Kafka, or separate broker.
- HMAC-authenticated, provider-specific callbacks with provider-scoped event deduplication.
- Reconciliation against the original provider attempt; an unresolved result remains `UNKNOWN`.
- A Next.js dashboard that reads live payment, attempt, event, and reconciliation data from the API.

## Architecture

```text
Browser ──> Next.js dashboard ──> FastAPI ──> PostgreSQL
                                      ▲            ▲
                                      │            │ durable jobs
                                      └──────── Python worker
```

PostgreSQL stores payment intents, provider attempts, synthetic provider records, callback events, state transitions, and durable jobs. Creating an intent and its initial processing job is one database transaction. Workers claim jobs in short transactions, perform provider work outside the claim transaction, and persist the outcome in a separate transaction. The database is the source of truth; the dashboard does not hardcode payment summaries.

### Payment lifecycle

```mermaid
stateDiagram-v2
    [*] --> CREATED: intent and processing job committed
    CREATED --> PROCESSING: worker starts provider attempt
    PROCESSING --> SUCCEEDED: provider confirms success
    PROCESSING --> FAILED: definitive decline or nonacceptance
    PROCESSING --> UNKNOWN: response is ambiguous
    UNKNOWN --> SUCCEEDED: authoritative status or callback evidence
    UNKNOWN --> FAILED: authoritative decline or nonacceptance evidence
    SUCCEEDED --> [*]
    FAILED --> [*]
```

`SUCCEEDED` and `FAILED` are terminal. Late, duplicate, or contradictory events cannot regress a terminal payment. A timeout is not proof of failure: reconciliation queries the **same provider attempt** and only resolves the payment when the simulated provider record supplies definitive evidence. If it remains inconclusive, the payment stays `UNKNOWN`; exhausted reconciliation work is visible as a dead job. The system never fails over an unresolved attempt to another provider. Per the simulation policy, confirmed provider nonacceptance resolves to `FAILED` rather than being retried through another provider.

### Data invariants

| Invariant | Enforcement |
|---|---|
| An idempotency key is unique per merchant | PostgreSQL unique constraint on `(merchant_id, idempotency_key)` |
| A repeated key with a different amount/currency is rejected | Request fingerprint comparison in the intent service |
| Payment intent, initial `CREATED` history, and initial processing job are committed together | One SQL transaction |
| Attempt numbers are unique per payment; attempts reference existing payments/providers | PostgreSQL unique constraint and foreign keys |
| Provider event IDs are unique per provider | PostgreSQL unique constraint on `(provider_id, provider_event_id)` |
| Current payment state changes only along valid, evidence-backed edges | Central state-machine service, payment row lock, and transactional history insert |
| A live job has one lease owner and lease expiry | PostgreSQL constraints plus `FOR UPDATE SKIP LOCKED`; expired leases are reclaimable |
| Provider calls do not hold a database transaction open | Worker claim and provider execution use separate transactions |
| An unresolved payment is not routed to another provider | Orchestration guard and persisted original attempt/reference |

These are educational application-level and database controls, not a production security or reliability guarantee.

## Prerequisites

- Docker Desktop with Docker Compose v2.
- Git.
- Optional for running checks on the host: Python 3.13+ and Node.js 22+ with npm.
- PowerShell 5.1 or later (Windows PowerShell or `pwsh`) to run the included demo script.

No external provider account or payment credentials are needed.

## Local setup

Run these commands from the repository root in PowerShell:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The Compose stack binds PostgreSQL, API, and dashboard ports to `127.0.0.1` only. The sample configuration leaves simulation administration and callback authentication disabled. To use the protected simulation endpoint and callback demo, set three different random local-only secrets in `.env`:

```text
SIMULATION_ADMIN_TOKEN=<random local-only token>
PROVIDER_A_CALLBACK_SECRET=<different random local-only secret>
PROVIDER_B_CALLBACK_SECRET=<different random local-only secret>
```

For example, generate a random hexadecimal value with:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Run it separately for each setting. These values are only for the local simulation; do not reuse production credentials.

The Compose stack keeps PostgreSQL data in the `postgres_data` named volume, so stopping or recreating containers does not erase payment records. The example database password is for local development only. Keep the ports bound to loopback; do not expose this stack to a network.

Start the services:

```powershell
docker compose up --build -d
docker compose ps
```

The API container applies Alembic migrations before serving traffic. Verify readiness:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/ready
```

Open the live-data dashboard at <http://127.0.0.1:3000/>. Interactive API documentation is at <http://127.0.0.1:8000/docs>.

To stop services:

```powershell
docker compose down
```

To also remove the local PostgreSQL data volume and its synthetic records:

```powershell
docker compose down -v
```

`docker compose down -v` is destructive to the local database; use it only when you intend to delete all saved synthetic payments and provider records.

### Configuration reference

| Variable | Purpose | Local behavior |
|---|---|---|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | PostgreSQL container database and account | Defaults are local-only; change them together if desired. |
| `DATABASE_URL` | SQLAlchemy connection used by the API, worker, and migrations | Compose defaults to the `db` service hostname and sample account. Match this URL to any changed PostgreSQL database/account settings. For host-run backend commands, use `127.0.0.1` and the published port. |
| `SIMULATION_ADMIN_TOKEN` | Protects `POST /simulation/payments` | Blank by default; simulation creation is disabled until configured. |
| `PROVIDER_A_CALLBACK_SECRET`, `PROVIDER_B_CALLBACK_SECRET` | Provider-specific callback HMAC keys | Blank by default; callbacks return `503` until the corresponding secret is configured. |
| `API_BASE_URL` | Backend URL used by the dashboard server | Compose defaults to `http://api:8000`; a host-run Next.js server defaults to `http://localhost:8000`. |

Compose reads these values from the repository-root `.env` file. Do not put real payment or production credentials in it.
PostgreSQL applies `POSTGRES_*` initialization values only when creating an empty data volume. Changing those variables does not update an already initialized database account; preserve the existing credentials or intentionally reset the local volume and its data.

## API overview

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Process health |
| `GET` | `/ready` | Database readiness |
| `POST` | `/payments` | Create or idempotently retrieve an intent |
| `GET` | `/payments` | List/filter payments (`merchant_id`, `state`, `limit`, `offset`) |
| `GET` | `/payments/{payment_id}` | Retrieve a payment |
| `GET` | `/payments/{payment_id}/timeline` | View attempts, transitions, provider events, and reconciliation jobs |
| `POST` | `/simulation/payments` | Create a deterministic scenario; requires `X-Simulation-Admin-Token` |
| `POST` | `/simulation/providers/{provider_name}/callbacks` | Submit a signed simulated callback |

Payment creation accepts a synthetic `merchant_id`, positive integer `amount_minor`, uppercase three-letter `currency`, and `idempotency_key`. The unique scope is merchant plus key. Matching requests return the existing payment; payload mismatch returns HTTP 409. Payment amounts are never card data.

Simulation scenarios are `SUCCESS`, `DECLINE`, `TIMEOUT`, `SUCCESS_LOST_RESPONSE`, and `TEMPORARY_UNAVAILABLE`. Provider selection is `provider_a` or `provider_b`; provider B must be explicitly selected for the initial attempt.

Callbacks are authenticated with HMAC-SHA256 over the exact raw JSON body. Send the signature as `X-Provider-Signature: sha256=<lowercase-hex-digest>` and use the secret corresponding to the provider in the URL. Callback event IDs are deduplicated within that provider's scope.

### Example API flow

Create an ordinary payment intent (the worker processes it asynchronously):

```powershell
$payment = Invoke-RestMethod `
  -Uri http://127.0.0.1:8000/payments `
  -Method Post `
  -ContentType "application/json" `
  -Body (@{
    merchant_id = "demo-merchant"
    amount_minor = 1250
    currency = "INR"
    idempotency_key = "demo-order-001"
  } | ConvertTo-Json)

$payment
Invoke-RestMethod -Uri "http://127.0.0.1:8000/payments/$($payment.id)/timeline"
```

Repeat the same request to receive the same payment (`200`); reusing its merchant and idempotency key with a different amount or currency returns `409`. A newly created intent initially returns `CREATED`; use its current state or timeline endpoint to observe worker progress. The simulation endpoint is separate, requires the admin token, and is used by the demo script.

## Reproducible demo

1. Configure the three local-only secrets above in `.env`, then start the stack.
2. From the repository root, run:

   ```powershell
   pwsh -File .\scripts\demo.ps1
   ```

   On systems without `pwsh`, run the same script with `powershell -File .\scripts\demo.ps1`.

The script creates fresh synthetic intents and waits for worker results. It demonstrates confirmed success, definitive decline, a lost response that reconciliation resolves using the provider's retained synthetic record, and a timeout that remains `UNKNOWN`. It then submits a signed success-shaped callback with no confirming provider record; the callback is recorded as contradictory, and sending it again is reported as a duplicate without changing the payment state. The script prints the created payment IDs; inspect their timelines in the dashboard.

The demo deliberately does **not** fabricate provider evidence to turn an uncertain outcome into success.

## Development and tests

For the normal local workflow, start PostgreSQL with `docker compose up -d db`, then run the backend from `backend`:

```powershell
python -m pip install -e ".[test]"
alembic upgrade head
uvicorn app.main:app --reload
```

In another terminal, run the worker from `backend`:

```powershell
python -m app.worker
```

For frontend development, run from `frontend`:

```powershell
npm ci
npm run dev
```

The host-run dashboard uses `http://localhost:8000` by default. To run the complete containerized stack instead, use the Compose commands in [Local setup](#local-setup); avoid starting duplicate host and container API/worker processes on the same database while testing.

Run backend tests and migration drift checks from `backend`:

```powershell
python -m pytest -q
alembic check
```

Tests require PostgreSQL. They create a randomly named temporary schema, run against it, and drop only that schema afterward. Set `TEST_DATABASE_URL` to a dedicated local test database if needed; the configured application `DATABASE_URL` is used when that variable is unset. The test database account needs permission to create and drop schemas. Do not use a shared or production database for tests.

Frontend checks (run from `frontend`):

```powershell
npm ci
npm run lint
npm run typecheck
npm run build
```

The tests cover idempotency including concurrent duplicates and payload conflicts; state-machine edges and history consistency; provider outcomes; timeout and lost-response handling; bounded reconciliation retries; concurrent worker claims and lease recovery; interruptions after provider submission and before job acknowledgement; callback authentication, deduplication, and races; and live timeline data.

Useful Compose operations from the repository root:

```powershell
docker compose ps
docker compose logs -f api worker
docker compose down
```

`docker compose down` stops and removes the containers and network but preserves the PostgreSQL volume. Use the `-v` option only for an intentional full local data reset.

## Continuous integration

GitHub Actions runs three checks on pushes and pull requests: PostgreSQL-backed backend tests with migrations and Alembic drift validation; frontend dependency installation, lint, type-check, and production build; and Docker Compose configuration validation.

## Deployment scope

Docker Compose is provided only for local development and demonstration. There is no supported hosted or production deployment process: ordinary payment APIs are unauthenticated, the sample database credentials are not production secrets, and this simulation has not been designed or assessed for internet exposure, financial use, or regulatory compliance. Keep it on loopback with synthetic data. The GitHub Actions workflow validates builds and tests; it does not deploy the application.

## Project boundaries

- This repository is a local educational simulation using synthetic data.
- It does not integrate with real payment providers, process real payments, or accept/store card credentials.
- Administrative/simulation controls are disabled when their secrets are unset.
- Local secrets belong in `.env`, which is ignored by Git. Never commit real secrets.
- No production, PCI compliance, or financial reliability claim is made.
