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

- **Feature flags** (`/flags`): per-environment flags (dev, staging, prod). Non-prod changes apply immediately.
  Prod changes go through approval by someone holding `flags.approve`. Every change is audited, emits
  `flag.changed.v1` and posts a notification. Service SDKs poll `GET /flags/api/snapshot?environment=prod`, which
  returns an `ETag` and answers `304 Not Modified` while the snapshot is unchanged.
- **Refunds** (`/refunds`): look up a charge and request a refund. Refunds under $100 execute immediately.
  Refunds from $100 to $999.99 need an approver, and $1,000 or more needs an admin (`refunds.approve_large`).
  Each request carries an idempotency key, so a double submit never refunds twice. Requests that exceed the
  remaining refundable amount are rejected. The payment processor is a fake adapter. The app shows a KYC hint
  on a transaction, built from `kyc.decision.v1` events only.
- **Feedback queue** (`/feedback`): every page has a floating **Feedback** button for reporting friction. Tickets
  land on a board (open / filed / shipped). Admins (the platform team) triage a ticket into a pre-filled GitHub
  issue; operators never need to open GitHub.
- **KYC review** is **design-only**. There is no KYC app in this repository. The refunds app consumes its
  `kyc.decision.v1` event contract, and the demo seed publishes sample events.

The shell (`app/main.py`) provides login, a home page, a cross-app approvals inbox (`/approvals`), the audit log
(`/audit`) and the notification log (`/notifications`).

## How to run

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

"File as GitHub issue" targets `Nicolasara/powerapps-devin-poc` by default; set `GITHUB_REPO=owner/repo` to change it.

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
2. When something is awkward, click the floating **Feedback** button and describe the friction. The ticket
   records which page it came from.
3. Log in as **Adam Admin** (the platform team). On the ticket, click **File as GitHub issue**. This opens a
   pre-filled issue with the page, the kind, the reporter's role and the description. Paste the issue URL back
   to mark the ticket as filed.
4. Devin picks up the issue, changes the code and opens a pull request. CI runs lint and tests, and a person
   reviews and merges. The fix ships like any other code change.

The point of the demo is the loop: friction a user reports becomes a reviewed code change.

## Not production-ready

This is a prototype for an evaluation, not a deployable system. Known gaps:

- **Authentication:** a dev login picker instead of Entra ID / OIDC. The session secret is a dev default, and
  forms have no CSRF tokens.
- **Database:** SQLite, with `create_all` instead of migrations. Production needs Postgres and Alembic.
- **Integrations are fakes:** the payment processor and the Teams/Slack notifier are stand-ins. Outbox events are
  dispatched in-process, with no retry worker or broker.
- **No field-level PII masking:** every role sees full customer names and emails.
- **No hosting, monitoring or alerting:** there is no deployment config, observability, backups or on-call setup.
- **No load or security testing:** there has been no performance testing, penetration testing or threat model.
  The flag snapshot endpoint is unauthenticated, where production would require per-service tokens.
