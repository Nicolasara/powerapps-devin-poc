# Internal tools without Power Apps: a code-first prototype

This is a prototype for the Power Apps build-vs-buy evaluation. It asks whether a fintech's internal tools,
which run today on Microsoft Power Apps, Dataverse and Power Automate, could instead be built as ordinary code
in a repository, with Devin doing most of the building and maintenance.

The stack is deliberately boring: FastAPI, SQLAlchemy 2.0 on SQLite, and server-rendered Jinja2 HTML. There is no
JavaScript framework. Every change ships as a pull request, CI checks it and a person reviews it.

## What's in it

### Shared foundation

Apps don't reimplement the controls a regulated business needs. They share one foundation in `app/platform/`:

| Concern | Power Apps equivalent | This prototype | Code |
|---|---|---|---|
| Identity / SSO | Entra ID sign-in | Dev login standing in for an OIDC flow (Entra group claims would map to roles) | `app/platform/auth.py` |
| Authorization | Dataverse security roles | RBAC: roles `viewer`, `operator`, `approver` and `admin` map to permissions, enforced server-side on every mutating route | `app/platform/rbac.py` |
| Audit | Dataverse auditing | Append-only audit log with actor, action, before/after and reason | `app/platform/audit.py` |
| Approvals | Power Automate approvals | Maker/checker state machine. The checker must hold the required permission and cannot be the maker | `app/platform/approvals.py` |
| Notifications | Teams connector | Notifier adapter. The demo stores messages so you can see them at `/notifications` | `app/platform/notify.py` |
| Integration | Dataverse triggers | Outbox events, written in the same transaction as the change and dispatched after commit | `app/platform/events.py` |

### Apps

Each app lives in `app/apps/<name>/`, with its own models, routes and templates. Apps never read each other's
tables. They communicate through events.

- **Feature flags** (`/flags`): one row per flag with dev → staging → prod columns and a **rollout plan** per key
  (default `dev 100 → staging 100 → prod 10 → prod 50 → prod 100`, editable on the flag page). **Roll forward**
  takes the next step; **roll back** steps down and applies immediately everywhere, prod included, because reducing
  exposure is the safe direction. Stage toggles and rollout overrides remain on the flag page. Non-prod changes apply
  immediately; prod steps and changes go through approval by someone holding `flags.approve`. Every change is audited, emits
  `flag.changed.v1` and posts a notification. Service SDKs poll `GET /flags/api/snapshot?environment=prod`, which
  returns an `ETag` and answers `304 Not Modified` while the snapshot is unchanged.
- **Refunds** (`/refunds`): look up a charge and request a refund. Refunds under $100 execute immediately.
  Refunds from $100 to $999.99 need an approver, and $1,000 or more needs an admin (`refunds.approve_large`).
  Each request carries an idempotency key, so a double submit never refunds twice. Requests that exceed the
  remaining refundable amount are rejected. The payment processor is a fake adapter. The app shows a KYC hint
  on a transaction, built from `kyc.decision.v1` events only.
- **Feedback queue** (`/feedback`): every page has a floating **Feedback** button for reporting friction. It opens an
  inline drawer over the current page and submits in the background, so a half-filled form on that page is never lost
  (without JavaScript it falls back to the standalone `/feedback/new` page). Tickets
  land on a board (open / filed / shipped). Admins (the platform team) triage a ticket into a pre-filled GitHub
  issue; operators never need to open GitHub.
- **KYC review** (`/kyc`): cases arrive from the KYC vendor's **signed webhook** (`POST /kyc/webhook/vendor`,
  HMAC-SHA256 in `X-Vendor-Signature`, idempotent on the vendor's id). Deterministic, explainable risk rules run on
  intake: shared device fingerprint / address / IP with a rejected applicant, repeated device fingerprints, vendor check
  results, disposable email domains. Clean passes auto-approve; everything else waits for an analyst with `kyc.review`.
  Rejecting a case re-scores every pending case linked to it. Decisions emit `kyc.decision.v1`, which the refunds app
  consumes for its risk hint; refunds never reads KYC tables. The queue has a "Simulate vendor webhook" button that signs a
  payload and runs it through the real intake path (scenarios: clean, same device as a rejected applicant, vendor review).

The shell (`app/main.py`) provides login, a home page, a cross-app approvals inbox (`/approvals`), the audit log
(`/audit`) and the notification log (`/notifications`).

## How to run

Requires Python 3.10+ and nothing else (no Node, no Docker, no external services).

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m app.seed reset          # create poc.db with demo users and data
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in as one of the demo users:

| User | Role | Can |
|---|---|---|
| Alice Viewer (`alice@corp.test`) | viewer | read everything |
| Oscar Operator (`oscar@corp.test`) | operator | create and toggle flags, request refunds |
| April Approver (`april@corp.test`) | approver | approve prod flag changes and refunds under $1,000 |
| Adam Admin (`adam@corp.test`) | admin | everything, including refunds of $1,000 or more |

Triage creates the GitHub issue through the API (`app/platform/github.py`, label `devin` so an automation can pick it up). Without `GITHUB_TOKEN` a fake tracker is used; set `GITHUB_TOKEN` and `GITHUB_REPO=owner/repo` for real issues, and `FEEDBACK_AUTO_FILE=1` to file on submission instead of on triage.

### Configuration

Everything runs with no configuration. These environment variables change behaviour:

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./poc.db` | SQLAlchemy URL; SQLite in dev, Postgres-compatible |
| `APP_ENV` | `dev` | Anything else disables dev fallbacks (e.g. the KYC webhook secret below) |
| `KYC_WEBHOOK_SECRET` | `dev-kyc-webhook-secret` in dev only | HMAC key the KYC vendor signs webhooks with (`X-Vendor-Signature`) |
| `GITHUB_TOKEN` | unset → fake tracker | Token used to create GitHub issues from feedback triage |
| `GITHUB_REPO` | `Nicolasara/powerapps-devin-poc` | Repository those issues are filed in |
| `DEVIN_ISSUE_LABEL` | `devin` | Label applied to filed issues; the Action below triggers on it |
| `FEEDBACK_AUTO_FILE` | unset | `1` files the GitHub issue on submission instead of on admin triage |
| `DEVIN_API_KEY` (repo secret) | required for the Action | Used by `.github/workflows/devin-on-label.yml` to start a Devin session |

### A two-minute tour

- **Feature flags** (`/flags`): each flag follows a rollout plan; **Roll forward** on a dev flag, then on to prod
  (which creates an approval for April), **Roll back** to step down immediately.
- **Refunds** (`/refunds`): open a transaction and request $20 (executes), $500 (needs April) or $2,000 (needs Adam).
  Transaction #3 shows the red KYC banner fed by `kyc.decision.v1`.
- **KYC** (`/kyc`, April or Adam): open a high-risk case to see the device-fingerprint links, view the specimen ID
  document, then use **Simulate vendor webhook** → "Same device as rejected applicant".
- **Feedback**: click the floating button on any page; as Adam, open the ticket and **Create GitHub issue**.

## How to test

```bash
ruff check .
pytest -q
```

The tests use a temporary SQLite database, so `poc.db` is never touched. They cover RBAC, flag approvals and the
snapshot ETag, refund thresholds, idempotency and over-refund checks, the KYC event subscriber and the feedback
queue. A smoke test renders every page as every role against the full demo seed. CI
(`.github/workflows/ci.yml`) runs the same two commands on every push and pull request.

## Demo flow

1. Log in as **Oscar Operator** and use the app normally: toggle a dev flag, request a refund.
2. When something is awkward, click the floating **Feedback** button and describe the friction in the drawer that
   opens; you stay on the page you were working on. The ticket records which page it came from.
3. Log in as **Adam Admin** (the platform team). On the ticket, click **Create GitHub issue**: the app files it with the `devin` label and links it.
4. The `devin` label triggers `.github/workflows/devin-on-label.yml`, which starts a Devin session through the Devin API (repo secret `DEVIN_API_KEY`). Devin changes the code and opens a pull request. CI runs lint and tests, and a person
   reviews and merges. The fix ships like any other code change.

The point of the demo is the loop: friction a user reports becomes a reviewed code change. An example in this
repository: issue #5 was filed from a feedback ticket, the Action started a Devin session when the `devin` label
was applied, and that session opened PR #11 about four minutes later.

## Not production-ready

This is a prototype for an evaluation, not a deployable system. Known gaps:

- **Authentication:** a dev login picker instead of Entra ID / OIDC. The session secret is a dev default, and
  forms have no CSRF tokens.
- **Database:** SQLite, with `create_all` instead of migrations. Production needs Postgres and Alembic.
- **Integrations are fakes:** the payment processor and the Teams/Slack notifier are stand-ins. Outbox events are
  dispatched in-process, with no retry worker or broker.
- **No field-level PII masking:** every role sees full customer names and emails in refunds; KYC pages (applicant address, device, documents) are limited to approver/admin via `kyc.read`.
- **No hosting, monitoring or alerting:** there is no deployment config, observability, backups or on-call setup.
- **No load or security testing:** there has been no performance testing, penetration testing or threat model.
  The flag snapshot endpoint is unauthenticated, where production would require per-service tokens.
