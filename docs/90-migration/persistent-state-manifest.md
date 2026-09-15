# Persistent-state migration manifest

This manifest is a review and backup inventory for the future VPS migration.
It does not perform SCP, rsync, restore, or deployment. Destination paths are
resolved from the target inventory and environment; this document intentionally
contains no provider hostname, VPS name, IP address, credential, or token.

| State | Source on the current deployment | Secret material | Copy/restore rule | Verification after restore |
| --- | --- | --- | --- | --- |
| Application MySQL | Configured database URL or an operator-created consistent backup | May contain encrypted secret ciphertext and customer data | Required; use a transactionally consistent backup and restore it before reconciliation | Schema/version check, row counts, and read-only application health |
| Marzban data | Compose `marzban_data` volume | May contain Marzban state and user data | Required; preserve the volume as an opaque application data set | Marzban health and read-only API checks |
| Shared Xray candidate | Inventory `verification.xray_config_file` (normally `data/marzban/xray_config.json`) | May contain rendered credentials; treat as sensitive | Required for rollback evidence; never commit or print it | JSON parse, `xray run -test`, and exact repo-owned fingerprint |
| Xray applied baseline | Sibling `xray_config.last_applied.json` in the same persistent Marzban data directory | No plaintext credentials by contract | Required; do not regenerate or auto-adopt a missing baseline | Strict sidecar schema and fingerprint match |
| Reality identity | Application `Secret` row `gateway/xray/reality-identity`, including its encrypted value | Yes, encrypted secret; plaintext must never be copied to logs | Required through the protected DB backup/restore path; never export plaintext | Purpose-bound read and safe Xray render verification |
| Marzban internal TLS | `data/marzban/internal.crt` and `internal.key` bind-mounted into Marzban | Private key | Required if the existing deployment is retained; preserve permissions | Certificate/key pair and Marzban HTTPS health |
| Operator configuration | Target `/opt/lingsway/.env` and root-owned `/etc/lingsway/*.conf` | Yes | Recreate from protected operator backup; do not place in Git or this manifest | File ownership/mode checks and redacted configuration validation |
| Mihomo state | `data/mihomo` bind mount and any configured provider files | May contain credentials | Copy only when the selected inventory enables the transport | Read-only listener/provider checks from inventory |
| Xray reload state | `data/xray-reload` bind mount | May contain operational state | Copy when present; do not infer missing data | Read-only reload/health verification |
| Gateway probe state | `data/gateway-probe` bind mount | No expected secret; still treat as operational data | Copy when the probe profile is enabled | Probe status check |
| Caddy state | Compose `caddy_data` and `caddy_config` volumes, when enabled | Certificates/private keys | Required for continuity of issued certificates; preserve permissions | Caddy config validation and HTTPS health |
| Uptime Kuma state | Compose `uptime_kuma_data` volume, when monitoring is enabled | May contain notification credentials | Conditional copy based on inventory feature flags | Monitoring service health |

The migration order is: protected backup and checksum, target filesystem and
volume restore, read-only service checks, candidate/baseline fingerprint check,
then the explicitly triggered existing-data reconciliation tool. Reconciliation
is never a startup hook, Alembic migration, scheduler action, or automatic
deployment step. A missing or ambiguous state item fails closed and requires an
operator decision.

