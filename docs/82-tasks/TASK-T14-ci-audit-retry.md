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

## 现状复核（2026-09-10）

`.github/workflows/security.yml` 的 `npm-audit` job 当前仍是单次调用：

```yaml
  npm-audit:
    ...
    steps:
      - run: npm ci
      - run: npm audit --audit-level=high
```

没有任何重试逻辑，本任务尚未开始实现。

## 实现建议（供 Codex 参考，不强制具体写法，但需满足上面的验收标准）

把 `npm audit` 那一步换成一段内联的重试脚本，思路：

1. 用 `npm audit --audit-level=high --json` 而不是纯文本输出，把 stdout 落盘
   （例如 `audit.json`），同时保留退出码。
2. 判断是否重试，只看两类信号，不要用“非 0 退出码”一刀切：
   - stdout 不是合法 JSON（registry 抖动时 npm 有时会把 HTML/纯文本错误页
     打到 stdout），或
   - stderr / npm 的错误输出里出现已知的瞬时故障特征字符串，例如
     `ECONNRESET` `ETIMEDOUT` `ENOTFOUND` `EAI_AGAIN` `ECONNREFUSED`
     `E5` (5xx 类 npm error code) `Service Unavailable`。
   命中以上任一条，判定为“外部服务/网络类失败”，sleep 后重试（建议
   2s/6s/18s 或类似指数退避），最多 3 次尝试。
3. 只要 `audit.json` 是合法 JSON 且包含 `vulnerabilities` 汇总（说明 npm
   audit 已经成功跟 registry 对话、只是报出了真实漏洞），一律不重试，直接
   按原始退出码失败，并把漏洞列表打印到日志里。
4. 三次尝试都命中“网络类失败”特征后，job 必须非零退出，日志里要明确写
   “npm audit 因外部服务/网络问题重试 3 次后仍失败”，不得吞掉、不得转
   warning、不得加 `continue-on-error`。
5. `npm ci` 那一步保持不变，不要用它的结果替代独立的 audit 执行。

验证方式：本地可以用一个假的 `npm config set registry` 指向一个会返回
503 或直接 connection refused 的地址，观察重试与最终失败行为；再用正常
registry 但人为在 `package.json` 里引入一个已知有漏洞的旧版本依赖，确认
这种情况下**不重试**、直接失败并打印漏洞详情。

