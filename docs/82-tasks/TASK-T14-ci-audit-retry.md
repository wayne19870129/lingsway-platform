# TASK-T14-ci-audit-retry

## 目标

为 `security.yml` 中的 `npm audit` 步骤增加针对临时外部服务故障的有限次
重试。npm audit 依赖外部 registry 服务，曾因 `503 Service Unavailable`
导致 CI 失败（run `33868697921`，2026-09-04）；该次失败与代码无关，且
`npm ci` 已报告 `found 0 vulnerabilities`。

## 约束

- 重试仅针对 HTTP 5xx 和网络层错误；不得对 audit 真实报出的漏洞进行重试。
- 重试次数耗尽后必须以失败告终，不得降级为 warning 或使用
  `continue-on-error`。
- 不得用 `npm ci` 的 `0 vulnerabilities` 结果替代独立的 audit 检查。
- 不得修改 audit 的严重等级门槛或隐藏真实漏洞结果。
- 每次重试应保留可审计的日志，明确记录重试原因和最终结果。

## 允许修改的文件

- `.github/workflows/security.yml`
- `docs/82-tasks/TASK-T14-ci-audit-retry.md`

## 验收标准

- 构造或模拟 HTTP 5xx 时，`npm audit` 按配置的有限次数重试。
- 构造或模拟 DNS、连接重置、超时等网络层错误时，按同一有限次数重试。
- audit 返回真实漏洞时不重试，并保留漏洞的包名、严重等级和退出失败结果。
- 重试次数耗尽时 workflow job 非零退出；没有 warning-only 或
  `continue-on-error` 路径。
- `npm ci` 报告 `found 0 vulnerabilities` 时，独立的 `npm audit` 仍然执行。
- CI 日志能够区分“外部服务/网络重试”和“真实漏洞失败”。
- 通过 Security workflow 验证，并确认不引入任何生产凭据或远程副作用。

