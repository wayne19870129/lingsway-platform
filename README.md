# Lingsway Platform

Private monorepo for the Lingsway provider-pluggable network subscription platform
on Debian 12. Interfaces, pure domain logic, side-effecting providers, and
infrastructure are kept behind explicit boundaries.

Repository construction follows `docs/REPO_ARCHITECTURE.md` and the staged task
sequence in `docs/CODEX_PROMPT.md`.

## Safety invariants

- Configuration is rendered in full from the database; incremental concatenation is forbidden.
- Unmatched user traffic is blocked and never falls back to `DIRECT`.
- External writes are allowlisted at the lowest client layer with no bypass switch.
- The repository contains `.env.example` only. Real credentials remain outside Git.

## Development and Cloud setup

### Stack and runtime requirements

- Backend: Python `>=3.12`, SQLAlchemy, Alembic, PyMySQL, PyJWT, and cryptography.
- Frontend: Next.js `16.3.1`, React `19.2.8`, TypeScript, and ESLint.
- JavaScript package manager: npm, using `frontend/package-lock.json`.
- Python package manager: pip, with Hatchling as the build backend.
- Node.js `22` is used by CI.
- GNU Make is required for the repository-level targets.
- Docker and Docker Compose are required for image builds, local services, and MySQL-backed integration tests.
- MySQL `8.4` is required by the integration test.
- The deployment target documented by the repository is Debian `12`.

### Install dependencies

Run from the repository root:

```sh
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
cd frontend && npm ci && cd ..
```

### Test, lint, and build commands

Unit tests use mock/noop providers and do not require a database:

```sh
make test-unit
```

The complete test target requires `TEST_DATABASE_URL` and a reachable MySQL
`8.4` instance:

```sh
TEST_DATABASE_URL=mysql+asyncmy://lingsway:test-only@127.0.0.1:3306/lingsway_test make test
```

Run the complete lint suite:

```sh
make lint
```

Build the frontend:

```sh
cd frontend && npm run build
```

Build the backend image:

```sh
docker build -f backend/Dockerfile -t lingsway-backend:t4 .
```

The repository-level build target is also available:

```sh
make build
```

At the current stage, `infrastructure/compose/compose.base.yml` does not define
services, so the target does not yet assemble a complete application stack.

### Environment variables

The standard Cloud setup, lint, unit tests, and frontend build use the defaults
in `backend/app/core/config.py` and do not require additional environment
variables.

The following variables are required for specific operations:

- `TEST_DATABASE_URL`: required by `make test` and integration tests.
- `WEBSHARE_API_KEY`: required by the read-only procurement CLI.
- `DATABASE_URL`: set this for a real database-backed runtime; development defaults to in-memory SQLite.
- `JWT_SECRET`: in production, must be at least 32 non-default characters.
- `SECRET_ENCRYPTION_KEY`: in production, must be a configured valid Fernet key.
- `MARZBAN_BASE_URL`, `MARZBAN_ADMIN_USERNAME`, and `MARZBAN_ADMIN_PASSWORD`: required when the Marzban provider is enabled.

Provider selections currently use mock/noop implementations in the registry. The
additional names in `.env.example` include legacy migration inventory and are
not all consumed by the current code.

## T1 verification

```sh
python -m pip install -e ".[dev]"
cd frontend && npm ci && cd ..
make lint
make test-unit
```

## Delivery sequence

Tasks T0 through T8 are completed one at a time. Every task is submitted as a PR,
reviewed, and merged before work starts on its successor.

## Explicitly out of scope

- Referral rewards, ticketing, wallet balances, and online customer-service plugins
- Payment webhooks; payment remains manually confirmed
- Xray gRPC runtime additions
- Full provisioning state machine and automatic retries
- Concurrent-claim load testing
- A second staging VPS; this stage uses local Docker
- Unconditional automatic production deployment
- Target-site frontend replication before deployment and recovery verification
