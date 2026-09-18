# Domain migration

> **状态：未撰写。** 本文件此前只有一句"Domain migration instructions are
> delivered in T7"；T 系列编号已被 S 系列取代（见 `83-project-continuity.md`
> §4b），没有任何已排期的任务会兑现这句承诺。

## 现有的、已实现的机制

换订阅域名**不需要改业务代码**，这是 ADR-006「三域分离」已经落地的性质：

- `SITE_DOMAIN` / `API_DOMAIN` / `SUBSCRIPTION_DOMAIN` 是三个独立配置项，
  即使当前都填同一个域名（见 `ARCHITECTURE.md` §7）。
- 订阅链接一律由 `SUBSCRIPTION_DOMAIN` 拼接，禁止硬编码；客户订阅详情页读的
  是当前配置，换域名后回到页面自动看到新链接。
- `infrastructure/caddy/Caddyfile.tmpl` 由这三个变量渲染。

## 本文件补齐时需要覆盖的内容

1. 切换顺序：先让新域名 DNS 生效并签出证书，再改 `SUBSCRIPTION_DOMAIN`，
   最后才回收旧域名——`deploy/lib/30_dns_verify.sh` 在解析不通过时不签证书，
   顺序写反会卡在这里。
2. 存量客户的过渡窗口：旧订阅链接在旧域名上还能用多久，谁来通知。
3. 回滚方式：改回原值需要哪些步骤，证书是否还在有效期内。
4. 与 `60-runbooks/` 的关系：域名切换是否需要一篇独立 runbook。

在补齐之前，任何域名切换都必须由人工按上述机制逐项确认，不得假设有一份
现成手册可以照做——`ARCHITECTURE.md` §12 T7 的验收标准是"照做能独立完成，
不需要口头补充"，本文件目前**不满足**这个标准。
