# Secrets layout

Real credentials never enter Git. Target-host configuration is stored in
root-owned mode-`0600` files under `/etc/lingsway`, plus `/opt/lingsway/.env` as
defined by the architecture.

## GitHub Actions secret names

Development and test workflows use the `STAGING_*` namespace. Production deploy
workflows use only these explicitly named secrets:

- `PROD_VPS_HOST`, `PROD_VPS_USER`, `PROD_VPS_SSH_KEY`
- `PROD_DATABASE_URL`
- `PROD_SECRET_ENCRYPTION_KEY`, `PROD_JWT_SECRET`, `PROD_WEBSHARE_API_KEY`
- `PROD_R2_ACCOUNT_ID`, `PROD_R2_BUCKET`, `PROD_R2_ACCESS_KEY_ID`, `PROD_R2_SECRET_ACCESS_KEY`
- `PROD_TELEGRAM_BOT_TOKEN`, `PROD_TELEGRAM_CHAT_ID`
- `PROD_RESEND_API_KEY`, `PROD_RESEND_FROM_EMAIL`
- `PROD_TURNSTILE_SITE_KEY`, `PROD_TURNSTILE_SECRET_KEY`
- `PROD_BACKUP_GPG_RECIPIENT`

`PROD_*` references are forbidden in `ci.yml` and `security.yml`; they may appear
only in `deploy-*.yml`. T1 defines names and policy only and does not create or
read any production secret.
