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

## 现状复核（2026-09-10）与实现建议

`backend/app/api/public.py` 的 `place_order`（约第 118-168 行）目前只调用
`ensure_capacity()` 校验 Webshare 侧的 GB 额度，完全没有查询
`EgressEndpoint` 表，确认本任务尚未开始。

真正的“确认付款时二次校验”已经存在，位于
`backend/app/infra/provisioning_state.py` 的
`SqlAlchemyProvisioningState.allocate_endpoint`（约第 59-77 行），条件是：

```python
EgressEndpoint.status == "AVAILABLE",
EgressEndpoint.purpose == "PRODUCTION",
EgressEndpoint.capacity == 1,
EgressEndpoint.current_count == 0,
```

且用 `with_for_update()` 加锁后原子分配。**这段代码不用改，也不要重复
加锁**——本任务只是在下单这一步补一个更早的只读提示，让用户在真正没有
IP 的时候不用走完下单流程才发现失败。

建议的新增只读预检，条件应与上面完全一致（不要引入按 `route_group_code`
/ `EgressGroup` 分组的新过滤逻辑——当前 `allocate_endpoint` 本身就是全局
分配，不分组，预检若加上分组过滤反而会造成“预检通过、确认付款却分配到
别的池子”的不一致）：

```python
has_ip = db.scalar(
    select(func.count()).select_from(EgressEndpoint).where(
        EgressEndpoint.status == "AVAILABLE",
        EgressEndpoint.purpose == "PRODUCTION",
        EgressEndpoint.capacity == 1,
        EgressEndpoint.current_count == 0,
    )
) > 0
```

放在 `ensure_capacity` 校验之后、`db.add(order)` 之前；没有可用 IP 时返回
`409`（跟容量不足复用同一状态码更一致），`detail` 说明是 IP 库存不足而
非流量额度不足，方便前端/客服区分。

测试可以直接复用 `backend/tests/integration/test_db_adapters.py` 里已有
的 `EgressEndpoint` fixture 写法（约第 101-116 行，`status="AVAILABLE"`,
`capacity=1`, `current_count=0`），以及 `backend/tests/unit/test_domain.py`
里现有的 mock provisioning state 写法，构造“预检通过但确认付款时已被占用”
的并发场景。
