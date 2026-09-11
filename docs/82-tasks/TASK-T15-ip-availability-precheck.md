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

## 现状复核（2026-09-11,Issue #52)

**本任务的核心实现已经完成,不是"尚未开始"**(这份文档在 2026-09-10 写下
的"现状复核"一节,当时如实记录了那个时间点的真实状态,但后续一次会话已经
把预检实现并合并进 `main`,这份文档没有跟着同步更新——Issue #52 在这一节
把它改成准确的当前状态)。

`backend/app/api/public.py` 的 `place_order` 现在在 `ensure_capacity()`
校验之后、`db.add(order)` 之前,有一段只读的 `EgressEndpoint` 库存查询:

```python
available_ip_count = db.scalar(
    select(func.count())
    .select_from(EgressEndpoint)
    .where(
        EgressEndpoint.status == "AVAILABLE",
        EgressEndpoint.purpose == "PRODUCTION",
        EgressEndpoint.capacity == 1,
        EgressEndpoint.current_count == 0,
    )
)
has_available_ip = (available_ip_count or 0) > 0
if not has_available_ip:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="No dedicated egress IP is currently available",
    )
```

条件和 `SqlAlchemyProvisioningState.allocate_endpoint`（
`backend/app/infra/provisioning_state.py`）的授权分配条件逐字一致,没有引入
`route_group_code`/`EgressGroup` 分组过滤,`allocate_endpoint` 本身的
`with_for_update()` 加锁分配逻辑完全没有改动。

Issue #52 核实这段实现之后,发现验收标准要求的测试覆盖里,有两项此前没有
补上,本次一并补齐:

- `backend/tests/unit/test_order_subscription_api.py::test_place_order_rejects_when_only_mismatched_egress_endpoints_exist`——
  分别构造"状态不对""purpose 不对""current_count 不为 0"三种单独不满足
  条件的 endpoint,验证预检不会误判为可用(`capacity` 因为有
  `CheckConstraint("capacity = 1")` 约束,现实中不可能出现不等于 1 的数据,
  没有单独测)。
- `backend/tests/unit/test_order_subscription_api.py::test_place_order_precheck_does_not_mutate_egress_endpoint`
  和 `backend/tests/integration/test_db_adapters.py::test_place_order_precheck_does_not_reserve_or_bind_the_endpoint`——
  验证预检真的是只读:`EgressEndpoint.status`/`current_count` 在下单前后
  不变;后者额外验证 `place_order` 不会创建任何 `EgressBinding` 行(这个表
  用了 MySQL-only 的生成列 SQL,断言这一点必须在真实 MySQL 集成测试里做,
  sqlite 单测建不出这张表)。

已有的、覆盖"预检通过但确认付款时已被占用"并发场景的
`test_ip_precheck_pass_does_not_prevent_authoritative_recheck_from_rejecting`
（`backend/tests/integration/test_db_adapters.py`）本次未改动,继续通过。
