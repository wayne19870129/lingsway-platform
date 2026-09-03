# 从 root 平滑迁移到 deploy 用户

本 runbook 只描述目标机上的操作顺序。阶段一只提交脚本和文档，不连接生产主机，也不修改生产 SSH 配置。

## 目标权限

`deploy` 负责 `/opt/lingsway` 下的代码、Compose 和运维脚本。`/etc/lingsway/*.conf` 继续由 `root:root` 持有、权限为 `0600`，deploy 用户不能通过 sudo 读取密钥。`/etc/sudoers.d/lingsway-deploy` 只允许 Docker、`docker compose` 和 `systemctl restart lingsway-*`。

## 迁移步骤

1. 以 root 登录目标机，先完成 `deploy/lib/00_preflight.sh`、备份和当前服务健康检查。不要先删除 root 的 SSH key。
2. 将源码和非敏感部署文件放到 `/opt/lingsway`，执行 `deploy/lib/10_system.sh -i /path/to/inventory.yml`。脚本会创建 deploy 用户、加入 docker 组、安装最小 sudoers；默认还会在公钥前置检查通过后按 `ssh-lockout.md` 加固 SSH。它不会修改 `authorized_keys`。
3. 将应用配置放在 `/opt/lingsway/.env`，将备份和应用密钥分别放在 `/etc/lingsway/backup.conf`、`/etc/lingsway/app-secrets.conf`。逐个执行 `deploy/lib/20_secrets.sh`，确认所有文件为 `root:root` 和 `0600`。脚本只检查变量名，不打印值。
4. 以 deploy 用户运行一次只读检查：`id`、`docker compose ps`、`/opt/lingsway/current/deploy/lib/70_verify.sh`。若 Docker 组尚未在当前会话生效，重新登录 deploy 用户后再检查。
5. 在 root 仍可用的窗口内完成一次加密备份、一次迁移演练和一次回滚演练。确认备份、Xray 九步保护和通知链路均有审计记录。
6. 连续观察一个维护窗口：订阅、现有用户路由、8443、Mihomo listener、备份 cron 和 Telegram 验证全部通过后，才进入收紧阶段。
7. 收紧 root 访问必须另行人工确认：先更新 root 的 `authorized_keys`，保留可回退会话，验证新会话可用后再移除旧 key。不得由本脚本自动修改 SSH 配置或账户。

## 回滚

若 deploy 用户验证失败，保留 root 会话和原有 release，修正权限或切回已知 release：

```bash
sudo /opt/lingsway/current/deploy/rollback.sh \
  --release-dir /opt/lingsway/releases/<known-good-release>
```

该命令只原子切换 `/opt/lingsway/current` 符号链接，不删除备份、不重启 transport 服务，也不改变 SSH 配置。Xray 回滚必须走 provider 的九步保护和对应 runbook。

## 完成条件

- deploy 用户能在 `/opt/lingsway` 内完成发布流程。
- `sudo -l -U deploy` 只显示预期的三类命令。
- deploy 用户不能读取 `/root` 或 `/etc/lingsway/*.conf`。
- root 仍保留可验证的紧急回退路径，直到人工确认收紧。
