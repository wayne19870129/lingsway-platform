# Backup and restore

> **状态：能力本身尚不存在。** 本文件此前只有一句"Backup, restore, and
> PowerShell GPG instructions are delivered in T6/T7"。这不是"文档欠撰写"，
> 而是**工具本身没有写**：`ops/backup/` 目录下只有一个 `.gitkeep`。

## 这个缺口的实际影响

`ops/backup/{backup.sh,restore.sh,install-cron.sh}` 全部缺失，直接导致四处
已经存在的引用指向不存在的文件：

| 引用位置 | 后果 |
|---|---|
| `AGENTS.md`「允许自主执行」→「执行 alembic upgrade（前提：先成功跑一次加密备份）」 | **该前提当前无法满足**，因此这条授权在实践中不成立 |
| `Makefile` 的 `backup` / `restore` 目标 | 调用即失败 |
| `deploy/lib/80_schedule.sh` | 会安装一条指向不存在脚本的 cron，安装时不报错 |
| `.github/workflows/deploy-migration.yml` | dry-run 里只 echo，真实执行时无脚本可调 |
| ADR-007 | 其"加密备份 + binlog 文件"一致性论证依赖 `ops/backup/backup.sh` |

**但部署不会因此静默通过**：`deploy/lib/70_verify.sh` 的 `check_13_backup`
在脚本缺失时返回失败（fail-closed），所以 `bootstrap.sh` 跑到验收阶段一定
会红。换句话说，**这是当前通往生产的硬闸门，不是一个可以延后的文档任务。**

## 补齐时必须满足的约束

- 备份必须加密（GPG，收件人来自 `/etc/lingsway/backup.conf` 的
  `BACKUP_GPG_RECIPIENT`），上传 R2，并做 SHA-256 校验——三项都是
  `ARCHITECTURE.md` §8 验收清单第 13 项的组成部分。
- `backup.sh` 必须支持 `--verify`（`70_verify.sh` 已经在这样调用它）。
- 删除或覆盖 `/var/backups/lingsway` 下的备份属于
  `AGENTS.md`「禁止自主执行」，任何脚本都不得内置自动清理旧备份的行为而不经
  人工确认。
- 恢复演练（`ARCHITECTURE.md` §9 的 P5）是独立验收项：在临时机器上用加密备份
  完整还原并验证订阅可用。当前恢复能力是"理论上可以"，不是"验证过可以"。

## 优先级

`docs/84-implementation-roadmap-2026-09.md` §5 把备份/恢复工具列为**第 1 优先
级**，并建议它落在任何"引入真实外部系统写副作用"的接线之前（即 Webshare /
Mihomo 的注册表放行之前）。2026-09-18 审计复核，该排序仍然成立。
