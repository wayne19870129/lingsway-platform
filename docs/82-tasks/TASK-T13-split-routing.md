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
