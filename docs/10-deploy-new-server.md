# Deploy a new server

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

The following bootstrap attempt showed all three secret files as `600
root:root`, but `20_secrets.sh` compared the mode to the literal `0600` and
rejected them. Linux `stat -c '%a'` returns `600`; the mode comparison now uses
that exact representation.

The next bootstrap attempt installed Docker/Compose and created the deploy
user, then failed with `tmp: unbound variable` in the sudoers cleanup trap.
The trap referenced a function-local variable after the function returned.
The cleanup now uses an explicit script-scope temporary-file variable, so
`set -u` cannot turn normal cleanup into a deployment failure.

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
