# Deploy a new server

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
