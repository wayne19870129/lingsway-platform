# TASK-T15 — 下单阶段出口 IP 可用性预检

风险等级：low

## 目标

已查证确认，`POST /orders` 目前只校验 provider 的 GB 额度（`ensure_capacity`），不校验数据库中 `EgressEndpoint` 是否存在可用 IP。产品规则是每客户一个专属 IP，因此真正的容量瓶颈是 IP 数量而非流量额度。

IP 可用性检查仅存在于旧 `confirm_payment_and_provision` 中，且受 `_automatic_egress_provisioning_enabled` 开关控制。该开关要求 `app_env == production` 且 `accounting_provider == marzban`。开关关闭时，旧代码会先标记订单 `PAID` 再尝试分配 IP；分配失败后订阅置为 `PROVISION_FAILED`，但订单保持 `PAID`，形成客户已付款但无可用订阅的状态，且无自动发现或清理机制。

本任务目标是在下单环节增加 IP 可用性预检，让客户在无可用 IP 时无法下单。

## 约束

- 预检只做只读查询，不得在下单时占用或预留 IP，避免未付款订单锁死库存。
- 确认付款时仍须再次校验，因为下单到付款之间 IP 可能被他人占用。
- 不得移除确认付款阶段的现有校验。
- `PROVISION_FAILED` 且订单为 `PAID` 的历史状态需要单独的对账或人工处理路径，本任务不负责。

## 允许修改的文件

- `backend/app/api/public.py`
- `backend/app/domain/`
- `backend/tests/`
- 与测试所需说明直接相关的 `docs/` 文件

## 验收标准

- 补充测试覆盖：无可用 IP 时，下单请求被拒绝。
- 补充测试覆盖：下单时有可用 IP，但确认付款时该 IP 已被占用，确认付款被拒绝且不会标记订单为 `PAID`。
- 预检使用只读查询，不改变 `EgressEndpoint`、`EgressBinding` 或库存预留状态。
- 确认付款阶段的二次 IP 可用性校验仍然存在。
- 不引入自动清理或历史 `PAID` / `PROVISION_FAILED` 对账逻辑。
