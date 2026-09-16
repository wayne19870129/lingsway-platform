# ADR-021 — Xray runtime control boundary

- Status: Accepted
- Date: 2026-09-16
- Decision owners: repository owner / human approval
- Scope: TASK-T16 Phase 2C3A
- Related decisions: [ADR-009](ADR-009-provider-abstraction.md), [ADR-014](ADR-014-xray-desired-state-ownership.md), [ADR-015](ADR-015-marzban-ownership-and-route-identity.md), [ADR-016](ADR-016-route-identity-architecture-unblock.md), [ADR-017](ADR-017-provisioning-lock-hold-span-narrowing.md), [ADR-019](ADR-019-xray-desired-state-credential-boundary.md), [ADR-020](ADR-020-xray-full-config-composition-boundary.md)

## 1. Context

The Phase 2C3 audit found that the existing Xray file provider and the
actual Compose topology do not currently share a valid runtime-control
boundary.

backend-api is a normal Python container. Its image contains the pinned
Xray binary and assets, which is valid for candidate xray run -test, but
the container is not a systemd host and does not own the running Xray
process. The Marzban container owns the Xray process and mounts the same
persistent xray_config.json artifact that the backend-side composition
path writes.

LocalXrayRuntime.reload() currently defaults to systemctl reload xray.
That command cannot be treated as a valid control path from backend-api
for this topology. This ADR defines the narrow application-time activation
boundary required before selecting a real Xray gateway provider in the
registry. It does not implement that wiring.

This ADR is subordinate to, and does not supersede, ADR-009, ADR-014,
ADR-015, ADR-016, ADR-017, ADR-019, or ADR-020. It preserves their
provider contract, ownership rules, safe-reload ordering, operation-scoped
credential boundary, full-config composition, and dynamic-client
ownership.

## 2. Verified Marzban endpoint behavior

The decision is based on the exact upstream Marzban v0.8.4 source at commit
[7f396db3e703d71a28060bc9ce4a532ec64cb1f4](https://github.com/Gozargah/Marzban/blob/7f396db3e703d71a28060bc9ce4a532ec64cb1f4/app/routers/core.py),
specifically app/routers/core.py.

### 2.1 POST /api/core/restart is rejected

The upstream handler first computes:

`startup_config = xray.config.include_db_users()`

and then calls:

`xray.core.restart(startup_config)`.

That handler starts from Marzban's current in-memory `xray.config`. It does
not first reload the externally modified shared `XRAY_JSON` file into that
in-memory configuration. Therefore:

`Lingsway installs candidate file -> POST /api/core/restart`

does not prove that the candidate just written by Lingsway is the candidate
Marzban activates. This endpoint is rejected as the Lingsway activation
boundary.

### 2.2 PUT /api/core/config is selected

The exact upstream handler:

1. constructs `XRayConfig(payload, api_port=xray.config.api_port)`, rejecting
   invalid payloads;
2. replaces the in-memory `xray.config`;
3. writes the payload to `XRAY_JSON`;
4. calls `include_db_users()`;
5. restarts the Xray core and connected nodes; and
6. returns the submitted payload.

Accordingly, the next implementation slice shall use authenticated
`PUT /api/core/config` as the Marzban runtime activation boundary. The
payload must be the exact full candidate that Lingsway has already validated
and atomically installed in the shared artifact. The API response must be
checked as an acceptance of that same candidate before the provider reports
activation success.

The endpoint requires sudo-admin authentication in the upstream handler.
Authentication is operation-time and lazy. Construction of the provider and
registry remains free of network I/O.

## 3. Ownership and boundary

### 3.1 Lingsway ownership

Lingsway/backend remains responsible for:

- DB desired routing state and the operation-scoped credential-resolution
  boundary;
- complete repo-owned Xray composition and candidate validation;
- the repo-owned shared-file projection;
- writer baseline, backup, rollback orchestration, and drift detection; and
- post-activation projection verification.

Marzban does not become the source of Lingsway's routing or outbound desired
state. In particular, Lingsway does not derive desired state from
`runtime.current()`, and it does not ask Marzban to generate repo-owned
routing/outbound topology.

### 3.2 Marzban responsibility

Marzban owns the actual Xray process lifecycle. Its API is used only as the
explicit runtime activation boundary for a candidate that Lingsway already
owns and has composed.

The fact that `PUT /api/core/config` also writes `XRAY_JSON` is an
authorized activation write only when all of these are true:

- Lingsway explicitly initiates the PUT;
- the payload is the exact candidate Lingsway just installed;
- Marzban is not generating Lingsway's DB desired routing/outbound state; and
- Lingsway continues with post-activation projection verification and
  fail-closed rollback if the shared artifact no longer equals the
  repo-owned candidate.

An operator or other writer issuing the same endpoint without Lingsway's
writer coordination remains out-of-band drift. The existing writer baseline
and drift rules are not bypassed.

### 3.3 Dynamic Marzban users

ADR-015 remains unchanged. Marzban's ordinary user CRUD and its runtime
Handler API own dynamic accounting-user membership. The repo-owned file
candidate keeps the accepted file skeleton, including
`settings.clients=[]`; dynamic runtime clients are not copied from
`runtime.current()` into the desired candidate and do not participate in
repo desired equality.

During `PUT /api/core/config`, Marzban's existing `include_db_users()`
behavior may inject its accounting users into the runtime startup
configuration. That runtime membership remains Marzban-owned and is not a
handoff of repo desired-state ownership.

## 4. Required activation sequence

The next Xray implementation PR must use the existing XrayFileProvider
safety model and ADR-017 ordering. It must not create a second reload
algorithm.

The required semantic sequence is:

1. back up the current shared configuration;
2. enforce the writer-baseline guard;
3. render the full desired candidate using the existing
   `GatewayProvider.render(desired, resolver)` contract and the
   operation-scoped Xray resolver;
4. run the pinned Xray binary/assets in the backend image with
   `xray run -test`;
5. atomically install the exact candidate into the persistent
   `xray_config.json` shared by backend and Marzban;
6. activate it with authenticated `PUT /api/core/config`, using that exact
   installed candidate as the payload;
7. verify that Marzban accepted the candidate;
8. verify runtime health;
9. verify the repo-owned post-activation projection; and
10. only then persist the APPLIED writer baseline.

On any failure after installation, the existing rollback path must restore
the exact backup, activate the restored configuration through the same
authenticated Marzban endpoint, and reverify runtime health. If rollback
cannot be proven successful, the existing DEGRADED/fail-closed semantics
remain authoritative.

No deployment command, Marzban restart, Docker command, or other Xray
mutation may occur after APPLIED promotion.

## 5. Control URL and health contract

The Marzban admin/control API and the Xray inbound are different runtime
targets and must not be conflated:

- Marzban's Uvicorn/admin API listens on container port 8000. The
  application-time control URL is `https://marzban:8000` on the Compose
  backend network, represented by central `MARZBAN_BASE_URL`.
- Xray listens on container port 8443. The runtime TCP health target is
  `marzban:8443` on that same backend network.
- The host-published 8443 port is not the application control API, and Caddy
  does not proxy the Marzban admin API.
- `127.0.0.1:8443` inside backend-api is not the real Xray runtime target.

A real Xray runtime health result must require both:

- the authenticated Marzban core API at `https://marzban:8000` is available
  and reports the core as started/running (the upstream `GET /api/core`
  response exposes `CoreStats.started`); and
- `marzban:8443` is TCP reachable from the backend network.

Any health failure follows the existing rollback path.

The selected normal production architecture is verified internal TLS, not
`MARZBAN_VERIFY_TLS=false`. The internal server certificate must include
at least `DNS:marzban`, `DNS:localhost`, and `IP:127.0.0.1`. The
backend-side explicit trust anchor is the mounted
`/app/data/marzban/internal.crt`, whose host-side source is the persistent
Marzban data directory. The next implementation may expose this path through
a central `MARZBAN_CA_CERT_PATH` setting, with the production default
`/app/data/marzban/internal.crt`; the provider must not read environment
variables as a second configuration source.

When `MARZBAN_VERIFY_TLS=true`, a valid configured trust path is mandatory
and the HTTP client must load that certificate through an explicit
`ssl.SSLContext` (or equivalent httpx TLS configuration). It must not rely
on system-CA auto-trust for this self-signed internal certificate and must not
use `verify=False` as the production default.

## 6. Authentication, credential lifetime, and certificate migration

The next implementation may reuse the central Settings values
`MARZBAN_BASE_URL`, `MARZBAN_ADMIN_USERNAME`,
`MARZBAN_ADMIN_PASSWORD`, and `MARZBAN_VERIFY_TLS`. The gateway provider
must not depend on a concrete `MarzbanAccountingProvider`; provider
categories remain independent.

The existing `MarzbanAccountingProvider` already establishes the accepted
secret-retention safety pattern: private admin username/password fields,
zero-network construction, safe representation, and lifecycle ownership by
the registry. The concrete Xray Marzban-control helper may use the same
pattern. Specifically, it may privately retain the process-stable
`base/control URL`, admin username, admin password, and TLS trust
configuration supplied by central Settings. The admin password is a secret
and may be a private process-lifetime field only under this redaction
contract:

- a custom safe `repr` must omit the password;
- the password must not appear in logs, exceptions, audit records, metrics,
  PR/docs text, or plaintext test output; and
- construction must perform no HTTP request.

The bearer token is operation-local. The helper authenticates lazily when an
operation needs the control API, uses the obtained token for the
candidate-activation and health operation, and discards the token reference
when that operation completes. No process-lifetime token cache is required
by this ADR. If a later implementation proves caching necessary, it must add
explicit redaction and invalidation tests without weakening this boundary.

This design supplies the existing `GatewayProvider.apply(candidate)`
shape without reading global environment state during an operation, keeping
a hidden closure around a Session/resolver, or changing the generic provider
contract. `build_registry(settings)` is the configuration assembly
boundary: it passes validated Settings-derived values to the concrete
helper; the helper does not call `os.environ` as a second source. A small
concrete Xray runtime/control helper or lazy authenticated client is
permitted, but no generic credential or control-plane abstraction is
required. Any owned client that needs closing is owned and closed through
the existing process/application-lifetime `ProviderRegistry` lifecycle.

The registry/provider must not retain a SQLAlchemy Session, operation
resolver, Reality plaintext, mutable operation context, or bearer token.
Construction of the registry/provider must make no network request, real
reload, Docker call, or deployment-side effect.

The TLS trust configuration is shared by all Lingsway clients that access
the same Marzban internal HTTPS endpoint. This includes the existing
`MarzbanAccountingProvider` and the concrete Xray Marzban-control helper.
Provider categories remain independent: neither provider depends on the other,
and they do not share a concrete client instance or token cache. They share
only central Settings-derived connection and TLS configuration. Before
`ACCOUNTING_PROVIDER=marzban` and `GATEWAY_PROVIDER=xray_file` can be
enabled together, both clients must load `MARZBAN_CA_CERT_PATH` through the
same explicit `ssl.SSLContext` (or strictly equivalent httpx trust
configuration) when `MARZBAN_VERIFY_TLS=true`. A split-brain state in
which gateway TLS succeeds but accounting TLS fails is not permitted.

Before Phase 2C3B, the deployment certificate generator created a self-signed
pair with SAN `DNS:localhost,IP:127.0.0.1`. That localhost-only pair is not
valid for `https://marzban:8000`. An existing certificate must never be
silently overwritten or replaced behind a live process. Before a future
implementation/deployment uses internal TLS, it must inspect the existing
certificate SANs. If `DNS:marzban` is absent, it must fail closed with the
stable reason code `MARZBAN_INTERNAL_TLS_MIGRATION_REQUIRED` (or an
equivalent stable, secret-safe code). A separately authorized certificate
rotation/migration step must then generate and install the new certificate
and key pair, with explicit restart/activation coordination. A fresh VPS
must generate a new pair that already satisfies the SAN contract.

## 7. Provider selection and portability




The Phase 2C3B implementation allows `GATEWAY_PROVIDER=xray_file` while
leaving the default `GATEWAY_PROVIDER=mock` unchanged. Even with
`ACCOUNTING_PROVIDER=mock`, a real gateway selection must fail closed when
the required Marzban runtime-control credentials are missing or invalid,
because Marzban still owns the Xray process.

The boundary uses central Settings, Compose internal DNS, and the existing
persistent volume layout. It must work on fresh Debian 12, Vultr, 搬瓦工, and
other ordinary Docker VPS hosts without relying on a provider-specific API,
fixed IP/hostname, Docker socket, host systemd, or SSH localhost control.

## 8. Explicitly rejected alternatives

This ADR rejects:

1. `LocalXrayRuntime.reload()`'s current default `systemctl reload xray`:
   backend-api is not the systemd/Xray owner;
2. Docker socket or Docker API access from backend: it grants excessive host
   and container control;
3. privileged backend or host PID namespace: it expands compromise impact and
   is not a narrow runtime capability;
4. invoking `docker restart`/Compose restart from the application: the
   application must not control the Docker daemon and this is not a narrow
   activation boundary;
5. `POST /api/core/restart`: it restarts from Marzban's current in-memory
   `xray.config` and cannot prove activation of an externally written
   candidate; and
6. allowing Marzban to regenerate Lingsway routing/outbound desired state:
   it violates DB/repository ownership.

## 9. Consequences and implementation gate

This decision gives the concrete Xray provider one narrow runtime control
boundary without adding a generic control-plane abstraction or changing the
existing gateway contract. It also makes the current `systemctl` default
an explicit topology mismatch rather than an implicitly accepted production
path.

The Phase 2C3B implementation adds contract tests for exact-payload
activation, authenticated operation-time behavior, Marzban acceptance,
internal-DNS health, rollback through the same endpoint, and fail-closed
handling. This ADR itself performs none of those runtime operations.

Mihomo is deliberately not addressed here. Its desired-state boundary and
compensation/concurrency semantics require a dedicated ADR before any real
forwarder wiring.
