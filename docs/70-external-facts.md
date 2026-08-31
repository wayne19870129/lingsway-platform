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
