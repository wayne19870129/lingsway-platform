# External facts ledger

Verified, documented, support-confirmed, and unverified provider behavior is recorded here in T7. Guesses are never treated as facts.

## Webshare 客户端统一：procurement 与 backend 的行为差异

- 状态: 已由代码审计与单元测试核验
- 旧 procurement 客户端: 使用独立 `requests.Session`，在客户端层只允许 GET
  与 `dry_run=true` replacement；其 base URL 已包含 `/api/v2/`。
- 当前 backend 客户端: `backend/app/providers/egress/webshare.py` 是唯一 HTTP
  请求实现；Provider 层按业务需要允许 `/api/v2/subuser/` 的受控写操作，供开通
  编排使用，同时保留购买、续费、账务和非 dry-run replacement 的硬拦截。
- procurement 适配层: `WebshareReadOnlyAdapter` 在进入 Provider 前再次收紧为
  GET 与 `dry_run=true` replacement，不获得子用户写权限；JSON 解析和分页均由
  该适配层提供，实际请求仍经过同一个底层 `request()` guard。
- 路径差异: 旧客户端通过 `/api/v2/` base URL 拼接相对路径；适配层把普通采购
  路径归一化到 `/api/v2/`，proxy 路径归一化到 `/api/v3/`，绝对 URL 也会重新
  经过归一化。
- 限速差异: 旧客户端以 bucket 间隔限速；当前 Provider 使用 General 240/min
  与 Proxy 60/min 滑动窗口，采购适配层复用该限制。
- 429 差异: 旧客户端曾把 Retry-After 最少抬高到 60 秒；当前统一实现优先按
  响应 Retry-After 原值等待，无该响应头时按 1/2/4 秒退避，最多重试 3 次后
  记录并放弃。
- 代理差异: 旧客户端关闭环境代理并允许调用方显式传入当前进程代理；新的
  `UrllibTransport` 默认不读取环境代理，CLI 的 `--proxy-url` 仍只注入当前
  进程 transport。

## 客户订阅分流规则：生产当前为全局代理

- 状态: 已实测
- 实测方法: 使用 Clash Verge 更新现有订阅，读取返回配置中的 `rules`；实际
  结果仅包含一条 `MATCH,SUBSCRIPTION`，未返回 13 条客户端分流规则。
- 代码依据: 旧生产来源 `F:\Codex\network-subscription-platform\backend\app\subscriptions.py:149-153`。
  `_mihomo_rules()` 的注释原文为:

  > `# Keep the public subscription compatible with older Clash Meta builds.`
  > `# MATCH sends every request to the selected foreign egress and does not`
  > `# require the client to ship a GEOSITE/GEOIP rule database.`

  该函数实际固定返回 `['MATCH,SUBSCRIPTION']`；同文件 `:180-182` 的另一个
  UA 分支也固定返回 `['MATCH,SUBSCRIPTION']`。
- 结论: 全局代理是当前的实际产品行为，不是故障。PROJECT_HANDOFF 中记录的
  13 条规则属于目标状态，与生产现实脱节。
- 影响: 客户国内流量也会经由 VPS 与住宅出口，消耗双向带宽额度；未来行为
  模式复杂化时，可能提高住宅 IP 被风控的概率。
- 后续: 是否启用客户端分流规则属于待决业务事项。任何变更必须先在单个
  订阅上灰度验证，再决定是否推广。
