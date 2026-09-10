# TASK-T13-split-routing

## 目标

在明确业务决策后，为客户订阅引入可验证的客户端分流规则，降低不必要的
国内流量经 VPS 与住宅出口转发，同时保持订阅兼容性、可观测性和可回退性。

## 约束

- 变更前必须确认目标客户端自带可用的 GEOSITE/GEOIP 数据库，并记录确认
  依据；不得假设客户端一定具备该数据库。
- 必须先在单个订阅上灰度，验证 Clash Verge 及其他目标客户端的实际解析、
  命中规则、连通性、流量消耗和住宅 IP 风控表现后，才能扩大范围。
- 必须保留一键回退到 `MATCH,SUBSCRIPTION` 的能力；回退路径也必须经过
  测试和验收。
- 不得改变 Xray 服务端的 `geoip:private -> BLOCK`、`tcp,udp -> BLOCK`
  等服务端路由安全不变式。
- 任何影响存量客户连接的改动必须提供回滚方案，并先完成单用户灰度记录。

## 允许修改的文件

- `backend/app/domain/subscription_render.py`
- `backend/tests/unit/test_domain.py`
- `backend/tests/guards/test_routing_invariants.py`
- `docs/70-external-facts.md`
- `docs/80-decisions/`
- `docs/60-runbooks/`

## 验收标准

- 目标客户端的 GEOSITE/GEOIP 能力有实际证据，且单订阅灰度成功。
- 分流规则内容、顺序和命中结果有自动化测试。
- 灰度订阅可正常更新、解析和连接；未命中规则不会回落到不安全的
  `DIRECT` 兜底。
- 一键回退后，订阅 `rules` 恢复为唯一的 `MATCH,SUBSCRIPTION`，并通过
  自动化测试和实际灰度验证。
- 记录灰度前后带宽消耗、住宅出口使用量、错误率和 IP 风控观察结果。
- 变更前后均通过 `ruff`、`mypy`、单元测试和相关护栏测试。

## 现状复核（2026-09-10）与推进方式

`backend/app/domain/subscription_render.py` 里的 `CORE_RULES`（13 条
分流规则）本轮迁移只是**保留定义、未接入渲染路径**（见 ADR-012），
`rules` 目前恒为 `MATCH,SUBSCRIPTION`。这是有意为之，不是遗漏。

本任务卡住的不是代码，是证据：验收标准第一条要求的“目标客户端
GEOSITE/GEOIP 能力实测”和“单订阅灰度成功”都需要真实客户端环境
（Clash Verge / Mihomo 实际连通性、流量消耗、住宅 IP 风控表现），这类
数据 Claude 无法在仓库里凭空产出，也不应该在没有证据的情况下先把分流
规则接入生产渲染路径——那样等于绕开了 ADR-012 定的门槛。

把本任务拆成两个阶段，Phase 0 现在就可以推进，不依赖任何代码改动：

**Phase 0 — 证据收集（人工/运维执行，不改代码）**

1. 挑一个内部测试订阅（非付费客户），确认其客户端为 Clash Verge 或
   Mihomo 内核，且能访问、更新 GEOSITE/GEOIP 数据库版本号——记录版本号
   和更新时间，不能假设默认自带最新库。
2. 手工把 13 条 `CORE_RULES` 临时写进这一个订阅的渲染结果（可以是本地
   改一份 YAML 直接导入客户端，不需要碰生产渲染代码），观察：
   - 各条规则的实际命中情况（哪些域名/IP 段命中了哪条规则）；
   - 连通性是否正常（尤其是命中 `GEOSITE,CN` / `GEOIP,CN` 之前的国外
     域名规则顺序是否生效）；
   - 与不启用分流规则（纯 `MATCH,SUBSCRIPTION`）相比，住宅出口流量、
     VPS 流量的变化幅度；
   - 观察期内是否出现连接失败率上升或住宅 IP 被目标网站风控（验证码
     增多、请求被拒等）。
3. 把上述实测结果按 `docs/70-external-facts.md` 的格式（状态：已实测/
   未验证 + 实测数据 + 结论 + 影响）记录成新条目。

**Phase 1 — 代码接入（Phase 0 数据齐全后才能开始）**

Phase 0 记录的事实台账条目就绪后，再回到本任务书按原有验收标准执行：
接入 `CORE_RULES`、补自动化测试、实现一键回退、正式单订阅灰度、记录
灰度前后指标。在 Phase 0 证据没有落进 `docs/70-external-facts.md` 之前，
不要开始改 `subscription_render.py` 的渲染路径。
