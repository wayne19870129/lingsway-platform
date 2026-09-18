# Backup and restore

## 交付状态

TASK-S07 已补齐 `ops/backup/backup.sh`、`restore.sh` 和 `install-cron.sh`，
并将部署验收硬闸门接到真实的本地备份和 R2 完整性证据。备份能力仍然是
生产部署的前置条件，不是可以忽略的 dry-run 标记。

## 备份流程

`backup.sh` 从 `/etc/lingsway/backup.conf` 读取 `BACKUP_GPG_RECIPIENT` 和
既有 R2 配置，使用 `mysqldump --single-transaction --routines --events
--triggers --hex-blob` 生成临时明文，随后用 GPG 加密。明文只位于受限临时
目录，退出时清理。最终本地目录只留下唯一命名的：

- `*.sql.gpg` 加密备份；
- `*.sql.gpg.sha256`，内容同时记录 SHA-256 和对应文件名。

两个产物都会上传到同一 R2 前缀。脚本不会自动 retention cleanup，也不会
覆盖或删除 `/var/backups/lingsway` 中已有的备份。任何 dump、GPG 或 R2 失败
都会返回失败，不会打印凭据或把状态伪装成成功。

## 只读验证

`backup.sh --verify` 选择指定的 `BACKUP_ARCHIVE` 或本地最新加密备份，先核对
本地 `.sha256`，再用 R2 head 检查加密对象和校验证据，并读取远端校验证据
与本地值比较。这个路径不生成 dump、不调用 GPG 加密、不覆盖本地文件，也不
上传对象。R2 ETag 不被当作 SHA-256。

## 恢复流程

恢复必须明确指定加密归档、目标数据库，并携带
`--confirm-empty-target`。脚本先核对本地 SHA-256，再查询目标数据库的表数；
目标不是空库或校验不匹配时立即失败。只有校验和空目标检查均通过后才解密
并导入。脚本不包含自动清空现有数据库的逻辑。

示例：

```sh
ops/backup/restore.sh \
  --archive /var/backups/lingsway/mysql-...sql.gpg \
  --target-database lingsway_restore \
  --confirm-empty-target
```

## cron 与 secret 权限

`install-cron.sh` 安装 root-owned、仅 root 可写的 wrapper 和 `/etc/cron.d`
条目。wrapper 以 root 读取 `root:root 0600` 的 `/etc/lingsway/backup.conf`，
再通过 `runuser --preserve-environment` 以 `deploy` 身份运行备份脚本；不通过
放宽配置文件权限解决读取冲突。应用 `.env` 只用于传递数据库连接参数，备份
配置随后重新加载，以保证 GPG/R2 值来自受保护文件。

## 部署验收

`deploy/lib/70_verify.sh::check_13_backup` 要求 cron、wrapper（或明确的备份
脚本）和可执行的 `backup.sh` 同时存在，并直接运行真实 `--verify`。任一缺失、
本地校验不匹配、R2 对象缺失或远端校验证据不匹配都会 fail-closed；不能用
任意环境变量命令替代验证。

本任务不执行真实生产备份、真实生产恢复、真实生产 migration，也不启用真实
production deployment workflow。恢复演练仍应在人工批准的临时环境中单独完成。
