# Deploy a new server

## Xray runtime rendering gate

The checked-in `infrastructure/marzban/xray_config.base.json` is only a
Reality inbound skeleton. Bootstrap runs the encrypted pre-migration backup
gate, starts MySQL, and executes `alembic upgrade head` before `40_stack_up.sh`.
That step then invokes `ops/gateway/render_xray_routes.py` in a disposable
backend container as the module `ops.gateway.render_xray_routes`. The renderer reads the migrated database and writes the
complete runtime file to `/opt/lingsway/data/marzban/xray_config.json` before
Marzban starts. An empty database produces one `BLOCK` blackhole outbound,
zero user routes, and the private-IP/tcp-udp BLOCK sentinels; it never invents
a gateway outbound. `XRAY_REALITY_DEST` and `XRAY_REALITY_SERVER_NAME` must be
provided by the target `.env`; the Reality private key and short ID are
generated during rendering.

The host verifier reads that host path for the JSON and routing checks, while
the container Xray test uses `/app/data/marzban/xray_config.json`. Keeping
these paths explicit avoids treating the checked-in skeleton or a missing
bind-mounted file as a runtime configuration. The verifier remains fail-closed
if rendering, JSON validation, `xray run -test`, or either routing sentinel
check fails.

The migration/render order is intentional: starting the full Compose stack
first would make backend-api depend on Marzban, while Marzban requires the
rendered file. If a target's Compose implementation cannot run the disposable
backend build, capture that failure and fix the deployment environment or
workflow; do not mount the base skeleton directly. The scripts explicitly
build the backend image and then run the one-shot container because Compose v1
does not support `run --build` and can otherwise print usage without making
the intended command run.

Marzban's internal HTTPS listener is separate from the public Caddy
certificate. On a fresh target, `40_stack_up.sh` creates a self-signed
localhost certificate/key pair only when both files are absent and mounts them
read-only into Marzban. A partial pair is a hard failure. This prevents the
image's exit-code-0 restart behavior from hiding a missing internal TLS
dependency; public certificate validation remains verifier item 5.

## T7 stage-two field findings (2026-09-02)

The temporary host `45.32.74.42` (`lingsway-t7-stage2`, Debian 12) reported
about 17 GiB free on `/` and about 679 MiB available memory. The deployment
preflight defaults require 20 GiB free disk and 2 GiB available memory, so
`00_preflight.sh` safely stopped at 0 seconds with:
`at least 20GiB free disk is required`. This is a failed preflight, not a
skipped check. A real deployment must use a host that meets the guard, or an
operator must explicitly review any threshold override.

Running `70_verify.sh` separately then exposed a fail-fast defect: when Docker
was absent, the shared command helper exited the whole verifier and prevented
the remaining 14 checks from being reported. The verifier now returns a
failure per missing prerequisite and continues, so the final report can
classify every check independently.

Only freshly generated staging secrets were used. R2, Telegram, and Webshare
were not configured, and production host `45.77.9.5` was not contacted. Since
the temporary host did not satisfy the resource gate, bootstrap did not reach
database migration, stack startup, or final verification; this run must not
be described as a successful deployment.

The first full bootstrap attempt on the upgraded host then reached
`10_system.sh` and stopped with `docker group does not exist; install Docker
before creating deploy access`. The stage-one script had assumed Docker was
already installed on a blank Debian host. The fix installs the Debian Docker,
Compose, sudo, UFW, fail2ban, and GnuPG packages when Docker/Compose is absent,
then starts Docker before configuring the deploy user. Package installation is
still target-local; no production host is contacted.

The following bootstrap attempt reached the package install step and failed
with `E: Unable to locate package docker-compose-v2`. The configured Debian 12
repositories provide `docker-compose` v1 instead. The deployment now selects
`docker-compose-v2`, `docker-compose-plugin`, or `docker-compose` according to
package availability, and the shared Compose helper supports both command
layouts.

The next run exposed a false-success hazard: an empty Compose skeleton caused
stack startup, migration, and the Xray check to appear successful under some
Compose versions even though no backend service existed. The deployment now
requires declared services (including `backend-api` for migration/Xray) and
uses a Compose-v1-compatible `ps` check before reporting PASS.

After Docker installation, Compose v1 rejected the repository's top-level
`name: lingsway` with `services 'name' must be a mapping not a string`. The
project name is now passed explicitly with `-p lingsway`, and the base Compose
file no longer uses the v2-only top-level key.

Compose v1 then interpreted the empty `services:` map as a service and reported
`Service services has neither an image nor a build context specified`. The
empty Compose skeletons now carry an explicit `version: "3.8"` for v1/v2
compatibility; no service was added by this fix.

The following bootstrap attempt showed all three secret files as `600
root:root`, but `20_secrets.sh` compared the mode to the literal `0600` and
rejected them. Linux `stat -c '%a'` returns `600`; the mode comparison now uses
that exact representation.

The next bootstrap attempt installed Docker/Compose and created the deploy
user, then failed with `tmp: unbound variable` in the sudoers cleanup trap.
The trap referenced a function-local variable after the function returned.
The cleanup now uses an explicit script-scope temporary-file variable, so
`set -u` cannot turn normal cleanup into a deployment failure.

The first full stack attempt on the upgraded temporary host exposed three
additional deployment prerequisites. The Compose file bind-mounts
`data/marzban/xray_config.json` and `db.sqlite3`; when either host file is
absent, Docker creates a directory at that path and the container fails before
its process starts. A deployment must create file-typed runtime paths before
`compose up`. The checked-in `xray_config.base.json` is intentionally only a
skeleton: it is not runnable until the database renderer supplies valid
outbounds, Reality values, and BLOCK routing. (Correction, TASK-T16 Phase
2A / ADR-014: the renderer does not supply inbound `clients` — it inherits
whatever `inbounds` the skeleton file already has, unchanged, and that
skeleton's client list is empty. Whether and how a running Marzban process
populates that shared file's `inbounds.settings.clients` at runtime is not
established by this repository's code; see ADR-014 for the still-open
question.)

The backend image also previously copied application source without installing
the dependencies declared in `pyproject.toml`. That allowed image build to
finish while runtime imports were unavailable. The image now installs the
project package during build. Finally, Compose v1 can return zero from
`exec` for a declared service that has no running container. The verifier now
requires a running `backend-api` container before executing its Alembic and
Xray checks, preventing a false PASS.

On the temporary host, Marzban then reached its own process but remained
unhealthy because the deliberately empty staging skeleton had no outbounds;
this is a fail-closed result, not evidence that a production Xray config is
valid. Root password SSH login remained enabled because SSH hardening requires
an operator-approved change under the repository permission policy. R2,
Telegram, and Webshare remained unconfigured by design.

The verifier self-audit also found that Compose-v1 status parsing could accept
one running container while another declared service was absent, and that the
UFW policy fallback could theoretically accept a default-policy line without
first proving UFW was active. It now checks every non-profile service through
Compose labels, skips only the explicitly disabled attribution profile, checks
the exit status of command substitutions, and requires both `Status: active`
and the default-deny policy. It also samples each container's running state and
restart count. Missing files, failed `curl`, failed `grep`, and
failed `docker exec`/Compose commands therefore remain failures rather than
being converted to PASS.

The backend API must expose a running process, not just an importable module.
On 2026-09-02, `docker logs --tail 100` for `backend-api` was empty and
`docker inspect` showed exit code 0 with repeated restarts. Running the image
entry point in the foreground also exited 0 because the migrated
`backend/app/main.py` was still a placeholder. The entry point now starts
Uvicorn and exposes `/health`; any restart loop must be diagnosed from actual
logs, inspect output, and a foreground run before being attributed to the
target environment.

The same run showed Caddy's actual error:
`ambiguous site definition: 45.32.74.42.sslip.io`. The staging inventory used
one temporary hostname for all three independent domain settings, while the
template emitted two overlapping site blocks. The template now uses one site
block containing all three variables, with an API host matcher when the API
domain is distinct, so equal staging values cannot make Caddy crash-loop.

The verifier accepts the Caddy certificate from its named Docker volume when
the legacy host certificate path is absent. It still requires a real
certificate and an expiry beyond 14 days; this only corrects the storage-path
lookup for the Compose deployment.

Compose v1 on Debian 12 uses `-T` for non-interactive `exec`; the verifier
uses that portable spelling so a successful Alembic or Xray command is not
silently captured as an empty result. The Marzban 1080 listener is defined in
an explicit `compose.socks.yml` override and is included only when
`ENABLE_SOCKS_1080=true`; the default transport file publishes only 8443.

The Xray skeleton finding was reviewed separately. Adding a guessed outbound
to `infrastructure/marzban/xray_config.base.json` would hide the fact that
user routes, Reality values, and BLOCK routing must come from the database
renderer (inbound `clients` are not part of what the renderer supplies —
see the correction above and ADR-014). The correct rule is therefore to
render the full runtime config before stack-up. `40_stack_up.sh` now requires file-typed
`xray_config.json` and `db.sqlite3`, parses the Xray JSON, and rejects an
unrendered config with no outbounds or routing rules. It never injects a
synthetic outbound. A fresh deployment must complete the database-backed
renderer step before Compose startup; the checked-in base file remains a
template only.

Deployment instructions are delivered and machine-verified in T6/T7.

## GitHub settings that require manual configuration

These settings cannot be configured safely by a repository workflow. Configure
them in the GitHub web UI for `wayne19870129/lingsway-platform`.

### Protect `main`

1. Open **Settings → Code and automation → Branches**.
2. Under **Branch protection rules**, choose **Add classic branch protection
   rule** (or create an equivalent ruleset) for `main`.
3. Enable **Require a pull request before merging**.
4. Enable **Require approvals** and choose the repository's review policy.
5. Enable **Require status checks to pass before merging**, then select the CI
   and Security checks, including backend, lint, frontend, backend-image,
   gitleaks, python-audit, and npm-audit.
6. Enable **Require branches to be up to date before merging**.
7. Enable **Do not allow bypassing the above settings** where available.
8. Ensure **Allow force pushes** and **Allow deletions** are disabled.
9. Save the rule. Direct pushes to `main` must be rejected; all changes must
   arrive through a pull request with green required checks.

### Add Actions secrets

Open **Settings → Secrets and variables → Actions → New repository secret** and
add each name below individually. Enter values only in GitHub's secret-value
field; never commit them or place them in workflow source.

Staging/test names use the `STAGING_*` namespace as needed by future test
workflows. Production names are reserved for the post-T7 deployment workflow:

- `PROD_VPS_HOST`
- `PROD_VPS_USER`
- `PROD_VPS_SSH_KEY`
- `PROD_DATABASE_URL`
- `PROD_SECRET_ENCRYPTION_KEY`
- `PROD_JWT_SECRET`
- `PROD_WEBSHARE_API_KEY`
- `PROD_R2_ACCOUNT_ID`
- `PROD_R2_BUCKET`
- `PROD_R2_ACCESS_KEY_ID`
- `PROD_R2_SECRET_ACCESS_KEY`
- `PROD_TELEGRAM_BOT_TOKEN`
- `PROD_TELEGRAM_CHAT_ID`
- `PROD_RESEND_API_KEY`
- `PROD_RESEND_FROM_EMAIL`
- `PROD_TURNSTILE_SITE_KEY`
- `PROD_TURNSTILE_SECRET_KEY`
- `PROD_BACKUP_GPG_RECIPIENT`

Do not add production secrets to `ci.yml` or `security.yml`; the repository
policy workflow rejects any `PROD_*` reference there. T6 deploy workflows are
dry-run skeletons and intentionally do not read these secrets yet.
