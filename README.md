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

## Risk-based deployment

Pull requests receive one risk label from `risk-classify.yml`. Higher risk wins
when a change touches multiple classes.

| Risk | Paths | Deployment mode |
| --- | --- | --- |
| `risk:low` | `frontend/**`, `docs/**`, `backend/app/api/**`, `backend/app/domain/**`, `backend/app/schemas/**`, `backend/tests/**` | `deploy-auto.yml` after merge; currently dry-run only |
| `risk:medium` | gateway/forwarder/egress providers, `ops/gateway/**`, `ops/forwarder/**`, Marzban and compose infrastructure | Manual `deploy-gateway.yml`; currently dry-run only |
| `risk:migration` | `infrastructure/alembic/versions/**` | Manual `deploy-migration.yml` with a mandatory backup gate; currently dry-run only |
| `risk:high` | Any path outside the classes above, or destructive/credential/host actions | No automated deployment; human VPS procedure required |

The deploy workflows are intentionally fail-closed with `DRY_RUN=true` until T7
implements the remote deployment scripts and production Actions secrets are
configured.

## Explicitly out of scope

- Referral rewards, ticketing, wallet balances, and online customer-service plugins
- Payment webhooks; payment remains manually confirmed
- Xray gRPC runtime additions
- Full provisioning state machine and automatic retries
- Concurrent-claim load testing
- A second staging VPS; this stage uses local Docker
- Unconditional automatic production deployment
- Target-site frontend replication before deployment and recovery verification
