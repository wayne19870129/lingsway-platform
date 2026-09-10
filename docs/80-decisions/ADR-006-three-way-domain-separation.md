# ADR-006: 订阅域名与站点域名三分离

- 状态: 已接受
- 日期: 2026-09-10（补记，实际生效于 T1/T7）

## 问题

站点、API、订阅链接可以共用一个域名，也可以分成独立的域名配置项。当前
全部指向 `lingsway.com`，是否需要在配置层面就把三者拆开。

## 决定

`SITE_DOMAIN` / `API_DOMAIN` / `SUBSCRIPTION_DOMAIN` 是三个独立的环境变量
（见 `.env.example`、`infrastructure/caddy/Caddyfile.tmpl`），即便现在
值相同，代码和配置渲染路径必须始终把它们当作三个独立值处理。所有订阅
链接一律由 `SUBSCRIPTION_DOMAIN` 拼接生成，禁止在代码里硬编码域名。

## 约束

1. `backend/app/domain/subscription_render.py` 及任何生成 `subscription_url`
   的代码，只能读取 `SUBSCRIPTION_DOMAIN` 配置项，不得拼接 `SITE_DOMAIN`
   或写死字符串。
2. `docs/40-domain-migration.md` 记录的域名迁移手册必须保持“只改一个
   环境变量即可切换订阅域名”这条能力有效。

## 考虑过的替代方案

1. 三者共用同一个 `DOMAIN` 变量：实现更简单，但一旦订阅域名因为被墙、
   被举报等原因需要单独更换，会被迫连带修改站点和 API 域名，扩大变更
   影响面，否决。

## 后果

现在三个变量值相同看起来是冗余配置，但保证了订阅域名可以独立切换而不
影响站点和 API 的可用性，这是应对“订阅域名可能被封锁”这类风险的前置
准备。

## 重新评估条件

无需重新评估；这是一个几乎零成本的前置约束，除非平台规模缩小到不再需要
应对域名风险。
