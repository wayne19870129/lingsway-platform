# TASK-S09 — 加密备份与恢复工具

## 范围

本任务补齐 MySQL 备份、备份校验、恢复和定时任务安装能力，并把部署验收
`check_13_backup` 接到真实的本地 SHA-256 证据和 R2 对象检查。任务不修改
MySQL 的 GTID、`sync_binlog` 或既有 Alembic revision，也不接线 Mihomo、
Webshare 或其它 provider。

## 已实现的安全边界

- `ops/backup/backup.sh` 使用 `mysqldump --single-transaction`，以
  `BACKUP_GPG_RECIPIENT` 加密，生成唯一名称的 `.sql.gpg` 和同名 `.sha256`
  完整性证据，再上传这两个对象到既有 R2。
- 明文 dump 只存在于 `umask 077` 的临时目录，成功和失败路径都会清理；本地
  备份目录不会自动清理、覆盖或删除已有备份。
- `--verify` 只读取选定的本地备份，核对本地 SHA-256，并以 R2 head/read
  检查上传的加密对象和 SHA-256 证据；它不会 dump、加密、覆盖或上传新备份。
- `restore.sh` 需要显式目标数据库和 `--confirm-empty-target`，先核对本地
  SHA-256，再检查目标数据库为空，最后解密并导入；不包含自动清空目标的路径。
- `install-cron.sh` 安装 root-owned wrapper。wrapper 以 root 读取保持
  `root:root 0600` 的 `/etc/lingsway/backup.conf`，然后用 `runuser` 以
  `deploy` 身份执行备份，不通过放宽 secret 文件权限解决冲突。
- `70_verify.sh::check_13_backup` 不接受任意自定义验证命令绕过，脚本、cron
  wrapper 和真实 `--verify` 均缺失或失败时都 fail-closed。

## 配置

备份配置继续使用 `/etc/lingsway/backup.conf` 中的：

- `BACKUP_GPG_RECIPIENT`
- `R2_ENDPOINT_URL`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET`

数据库连接参数来自部署环境中的 `MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、
`MYSQL_PASSWORD`、`MYSQL_DATABASE`，默认主机为 `mysql`、端口为 `3306`。不
开启或依赖 MySQL GTID。

## 验证与生产边界

提交前必须执行 shell 语法检查、Ruff、mypy、unit/guard 测试和
`git diff --check`。本任务测试只使用假的 dump、GPG、MySQL 和 R2 命令，不
连接真实生产数据库、真实 R2 或生产 GPG 私钥。

本任务不执行真实生产备份、真实生产恢复或真实生产 migration，也不启用真实
production deployment workflow。真实恢复、生产数据删除或覆盖仍需人工批准。
