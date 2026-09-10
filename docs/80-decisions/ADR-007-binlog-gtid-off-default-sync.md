# ADR-007: MySQL GTID 保持关闭，`sync_binlog` 保持默认值

- 状态: 已接受
- 日期: 2026-09-10（补记，实际生效于 T5，见 `infrastructure/mysql/conf.d/99-binlog.cnf`）

## 问题

`ops/backup/backup.sh` 的加密备份 + binlog 依赖 `infrastructure/mysql/conf.d/99-binlog.cnf`
里的 binlog 配置。是否需要开启 GTID（便于多源复制/故障切换）、是否需要
把 `sync_binlog` 从默认值改成 1（每次事务都刷盘，最强持久性但有额外
IO 开销）。

## 决定

当前配置只声明 `log_bin=mysql-bin` 和 `binlog_format=ROW`，不开启 GTID
（`gtid_mode` 保持 MySQL 默认关闭），`sync_binlog` 不做任何覆盖，保持
MySQL 发行版自带的默认值。这两项都是为单实例 + 定期加密备份的当前部署
形态服务，不是为多主复制或零数据丢失场景服务。

## 约束

1. 新增迁移或运维脚本不得假设 GTID 已开启（例如不能依赖
   `SELECT @@GLOBAL.GTID_EXECUTED` 做位点追踪）。
2. `ops/backup/backup.sh` 的一致性保证来自“加密备份 + binlog 文件”组合，
   不依赖 GTID。
3. 若后续需要开启 GTID 或调整 `sync_binlog`，必须先验证现有备份/恢复
   脚本（`docs/30-backup-restore.md`）在新配置下仍然可用。

## 考虑过的替代方案

1. 开启 GTID：为将来可能的多实例复制预留能力，但当前只有单实例 MySQL，
   开启 GTID 不带来任何即时收益，反而增加运维认知负担，否决（可在真正
   需要复制时再开启）。
2. `sync_binlog=1`：提供每事务刷盘的强持久性，但会显著增加写入延迟，
   当前用加密备份 + binlog 保留窗口已经能满足恢复点目标，没有必要为
   单实例场景牺牲写入性能，否决。

## 后果

保持默认配置意味着极端断电场景下最后极少量未刷盘的事务可能丢失，这个
风险由“定期加密备份 + binlog 保留”这套组合缓解，而不是靠强制刷盘。

## 重新评估条件

当引入第二个 MySQL 实例做复制、或者恢复点目标（RPO）要求收紧到“零事务
丢失”时，重新评估开启 GTID 和调整 `sync_binlog`。
