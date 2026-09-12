# ADR-018: `AccountingProvider.create_user()` failure ownership contract

- 状态: 已接受
- 日期: 2026-09-12（同日第二次修订：独立审查对本 ADR 首版实现的第二轮
  复审再次发现两处 Major——(1) `MarzbanAccountingProvider.create_user()`
  内、发生在 `POST /api/user` **dispatch 之前**的请求构造/校验失败
  （naive datetime、pre-epoch tz-aware datetime、未来任何 preflight
  validation）此前仍然只抛出裸 `MarzbanContractError`，未被规范化成
  `AccountingCreateUserError(effect=NO_SIDE_EFFECT)`——这类失败发生时
  POST 根本没有发出，却会落入 `ProvisioningService` 的
  "未分类异常 → 视同 CREATED → 尝试 disable" 默认分支，对一个从未尝试
  创建的 username 执行 `disable_user()`，与本 ADR 的核心目的直接矛盾。
  (2) 新增的两类 phase-A `PENDING_MANUAL`（accounting `AMBIGUOUS`、
  `CREATED` 但 disable 补偿本身失败）复用了
  `services.confirm_payment_and_provision()` 里 `CREATE_TENANT` 时代
  遗留的硬编码业务消息 `"external tenant creation requires review"`，
  导致 `Subscription.provision_error` 在这两种新场景下描述错误的
  subsystem，误导人工处置；同时补偿失败分支把原始 provider 异常文本
  直接拼进 diagnostic 消息，完全丢弃了 disable 本身失败的异常信息。
  本轮修复：(1) `create_user()` 把请求体构造阶段（当前只有
  `_map_expire_to_marzban()`）包在自己的 `try/except
  MarzbanContractError`，统一 `raise
  AccountingCreateUserError(effect=NO_SIDE_EFFECT) from exc`——见下方
  "Pre-dispatch request-construction failures" 一节。(2) 新增
  `PendingManualReason`（typed、closed-set 的业务分类）+
  `PENDING_MANUAL_BUSINESS_MESSAGES`（固定、安全、可持久化的消息映射），
  `ProvisionOutcome` 新增 `reason` 字段，三个 phase-A `PENDING_MANUAL`
  来源（`CREATE_TENANT`、accounting `AMBIGUOUS`、accounting 补偿失败）
  各自标注正确的 `reason`；`services.py` 改为按 `reason` 查表得到
  `Subscription.provision_error` 的消息，不再硬编码、也不解析
  `pending_manual_error` 的自由文本；`_compensate_created_accounting_user()`
  的 diagnostic 消息改为只携带异常*类型名*（原始 create 失败类型 +
  disable 失败类型），不再把 disable 异常整体丢弃，也不把任意 provider
  文本当业务数据持久化。详见下方 "Pre-dispatch request-construction
  failures" 与 "Business-facing pending reason vs. run-level diagnostic"
  两节。同日第三次修订：独立审查第三轮复审发现一个 Major——
  `MarzbanAccountingProvider.create_user()` 只把 `400`/`409` 归类为
  `NO_SIDE_EFFECT`，其余非 `200` 状态码一律 `AMBIGUOUS`；但重新核对
  pinned exact source（`app/routers/user.py::add_user`）后确认，
  `add_user(new_user: UserCreate, ...)` 的 `new_user` 是一个普通
  Pydantic-model body 参数（没有包在 `Depends()` 里），FastAPI 请求
  处理管线会先对它做校验/解析（校验失败抛
  `RequestValidationError`，被 FastAPI 转换成 `422` 响应），这个过程
  发生在路由函数体（因此也在 `crud.create_user()` 的 `db.commit()`）
  执行之前——这是 FastAPI 框架本身的保证，不是 Marzban 自己的逻辑，
  和本 ADR 已经用来证明 `401`（`Admin.get_current` 这个
  `Depends()`）的推理属于同一类。因此 `422` 此前被错误分类为
  `AMBIGUOUS`，会把一个确定无 side effect 的请求校验失败错误地送进
  `PENDING_MANUAL`，保留 phase-A 的部分 DB 进度，而不是按
  `NO_SIDE_EFFECT` 回滚。本轮修复：把 `422` 加入
  `create_user()` 判定 `NO_SIDE_EFFECT` 的状态码集合
  （`(400, 409, 422)`）。同时按审查要求重新核对了 `add_user()`
  之外是否还有其它状态码能从 exact source 明确证明发生在 create
  之前——确认没有：`403`/`404` 不出现在 `add_user()` 路径上，其余
  post-commit 失败点（后台任务调度、`UserResponse.model_validate()`、
  `report.user_created()`）仍然只能在 `crud.create_user()` 的
  `db.commit()` 成功之后才可能触发，因此继续保持 `AMBIGUOUS`，不得
  扩大 `NO_SIDE_EFFECT` 的范围。详见下方 "422 validation rejection is
  NO_SIDE_EFFECT" 一节。）
- 决策范围: TASK-T16 Phase 2B7 独立复审（ChatGPT，针对 PR #64 exact head
  `d03e519199699310f659bd643c5d9addbf2ee77e` 的 Major 1）明确授权的、
  仅限于 `AccountingProvider.create_user()` 失败时 external-side-effect
  certainty / compensation ownership 的最小 architecture correction。
  **不**扩展成通用 provider retry/idempotency 框架，**不**引入
  409-reconciliation（"GET 已存在用户并接管"）逻辑——那仍然是一个未做出
  的、独立的 idempotency 架构决策，本 ADR 明确不做。
- 前置: `ADR-016-route-identity-architecture-unblock.md` (Decision 3 —
  `routing_principal` 契约，unchanged)，`ADR-017-provisioning-lock-hold-span-narrowing.md`
  (run-persistence-ordering / PENDING_MANUAL 语义，unchanged，本 ADR 复用
  其既有 `ProvisionOutcome(PENDING_MANUAL, pending_manual_error=...)`
  模式，不重新设计)。

## Context

TASK-T16 Phase 2B7（PR #64）实现了 `MarzbanAccountingProvider`。其
`create_user()` 在收到 HTTP `409`（"用户已存在"）时正确地 fail closed、
不做任何 reconciliation。但 `ProvisioningService.provision_prepare()`
的 `CREATE_ACCOUNTING_USER` 步骤对 **任何** `create_user()` 异常都无条件
执行：

```python
except Exception as exc:
    self.accounting.disable_user(request.username)
    ...
```

当 `create_user()` 因为 `409` 失败时，`request.username` 对应的 Marzban
用户**并非本次 provisioning 创建**——可能是数据早已存在的、与本次请求无
关的另一条业务记录（例如用户名冲突、或历史遗留账号）。无条件
`disable_user(request.username)` 会在这种情况下禁用一个不属于本次
saga、且本来完全正常的外部账号，这是一个严重的 ownership 安全缺口。

同样的问题也存在于 transport-ambiguous 失败：`POST /api/user` 的请求可能
根本没有到达服务器、到达但未创建、已经创建成功但响应丢失、或服务器返回
了 `409` 但响应丢失——在这些情况下都无法确认"本次调用是否真的创建了
这个用户"，因此同样不能盲目 `disable_user()`。

修复要求引入一个不靠猜测 HTTP 状态码、而是基于 exact-source 验证的
"这个失败发生在 side effect 提交之前还是之后（或无法确定）"分类。

## Decision: a typed three-way `AccountingCreateEffect` classification at the provider boundary

`backend/app/providers/base.py` 新增：

```python
class AccountingCreateEffect(Enum):
    NO_SIDE_EFFECT = "no_side_effect"
    CREATED = "created"
    AMBIGUOUS = "ambiguous"


class AccountingCreateUserError(RuntimeError):
    def __init__(self, message: str, *, effect: AccountingCreateEffect) -> None:
        self.effect = effect
        super().__init__(message)
```

这是唯一跨越 provider/domain 边界的新契约类型——`domain/provisioning.py`
只依赖 `backend/app/providers/base.py`，永远不 import
`backend.app.providers.accounting.marzban`（或任何其他具体 provider 模块）。
`effect` 是一个 typed `Enum` 字段，调用方不得靠字符串 grep 或
`status_code` 猜测语义。

### 三种分类语义

1. **`NO_SIDE_EFFECT`** — provider 能够（基于 exact-source 证据）证明本次
   请求在服务端创建 side effect **之前**就已经被拒绝。
   - 不允许 `disable_user()`。
   - `state.rollback_database()` → 该 run 标记 `FAILED` → 原异常
     `raise`。
   - 不进入 `APPLY_GATEWAY`，不获取 ADR-016 named lock（`CREATE_ACCOUNTING_USER`
     本来就在 `provision_prepare()` 里，phase B 尚未开始）。

2. **`CREATED`** — provider 能够证明本次请求**已经**在服务端创建了这个
   用户，但随后的 contract/postcondition 校验失败（例如响应缺少
   `routing_principal`、`username` 不匹配、或返回了一个不可用的
   `status`）。ownership 已确立：
   - 尝试 `disable_user()`（never `DELETE`——AGENTS.md 铁律 4）。
   - 若 `disable_user()` 成功：`state.rollback_database()` → `FAILED` →
     原异常 `raise`（与本 ADR 之前的既有行为完全一致）。
   - 若 `disable_user()` 本身失败：外部账号可能仍然是 enabled 状态，
     **不得**伪装成一个已经完整补偿的普通 `FAILED`。改为
     `PENDING_MANUAL`（复用 ADR-017 已有的
     `ProvisionOutcome(PENDING_MANUAL, pending_manual_error=...)` 模式，
     与 `CREATE_TENANT` 步骤的既有先例完全同构——不重新设计
     run-persistence-ordering，见"复用 ADR-017 既有模式"一节）。

3. **`AMBIGUOUS`** — provider 无法证明请求发生在 side effect 提交之前
   还是之后（例如 dispatch 阶段本身的 transport 失败：超时/连接重置/
   响应丢失，或一个未在 exact-source 中被证明是 pre-commit 的意外状态码）。
   - 不自动重试 `create_user()`（保持 TASK-T16 Phase 2B7 已确立的
     "ambiguous write 从不自动重试" 规则不变）。
   - 不盲目 `disable_user()`（这正是本 ADR 要修复的 ownership bug）。
   - 不继续 `APPLY_GATEWAY`，不获取 named lock。
   - `PENDING_MANUAL`，与 `CREATE_TENANT` 同构。

### 未分类（不是 `AccountingCreateUserError` 的）异常

任何 provider（包括测试用的 `MockAccountingProvider`）抛出的、不是
`AccountingCreateUserError` 的普通异常，视为与 `CREATED` 相同的处理路径
（尝试 disable，成功则按原有方式 FAILED+raise，失败则 PENDING_MANUAL）——
这是本 ADR 之前 `ProvisioningService` 对 `create_user()` 任何失败的既有
默认行为，保持向后兼容（现有 `test_step_6_failure_disables_user_and_never_deletes`、
`test_external_failure_keeps_paid_order_marks_subscription_failed_and_disables_user`
均依赖"任意异常 → 尝试 disable → FAILED"这一既有语义，且均使用普通
`RuntimeError` 注入，不使用 `AccountingCreateUserError`）。一个尚未升级
到本 ADR 类型化契约的 provider 因此不会退化成"完全不补偿"——它只是拿不到
`NO_SIDE_EFFECT`/`AMBIGUOUS` 更精确分类带来的保护，这是该 provider 自己
应尽快迁移的技术债，而不是本 ADR 引入的新风险。

## 复用 ADR-017 既有模式，不重新设计 run-persistence-ordering

`CREATE_TENANT` 步骤（`provision_prepare()`）早已确立"外部 side effect
结果不确定时不要在 provider 边界内部直接 `mark_status(PENDING_MANUAL)`，
而是返回 `ProvisionOutcome(PENDING_MANUAL, pending_manual_error=...)`，
由调用方 `services.confirm_payment_and_provision()` 在自己的业务提交
(`order_state.mark_provision_pending()`) 真正完成之后，才调用
`ProvisioningService.mark_run_pending_manual()`（best-effort）"这一完整
的、已经过独立审查四轮打磨的 ordering 契约（见 ADR-017"PENDING_MANUAL
terminal persistence ordering"一节）。本 ADR 的 `AMBIGUOUS`/
`CREATED`-disable-失败 两条路径**原样复用**这个既有契约，不引入任何新的
persistence-ordering 设计：

- `provision_prepare()` 内这两条分支只做既有的 best-effort
  `self.runs.record_step(run_id, CREATE_ACCOUNTING_USER, "PENDING_MANUAL")`，
  不直接 `mark_status()`。
- 返回 `ProvisionOutcome(run_id, PENDING_MANUAL, pending_manual_error=...)`。
- `services.confirm_payment_and_provision()` 已有的
  `if isinstance(prepared, ProvisionOutcome):` 分支（`order_state.mark_provision_pending()`
  → `provisioning.mark_run_pending_manual()`）**不需要任何修改**——它是
  泛化处理任意 phase-A `PENDING_MANUAL` 结果的，`CREATE_ACCOUNTING_USER`
  产生的 `PENDING_MANUAL` 与 `CREATE_TENANT` 产生的走的是完全相同的
  下游代码路径。

### 是否需要 `state.rollback_database()`

`CREATE_TENANT` 的 `PENDING_MANUAL` 分支不调用 `rollback_database()`，
因为该步骤运行时（steps 1-6 中的第 3 步）尚未有任何 DB mutation 被
flush。`CREATE_ACCOUNTING_USER`（第 6 步）运行时，`STORE_CREDENTIALS`
(4)、`APPLY_FORWARDER` (5) 已经 flush 了真实的凭证/forwarder-desired-state
mutation——这些代表已经真实发生的外部/业务进度。本 ADR 决定：
`AMBIGUOUS`/`CREATED`-disable-失败 两条 `PENDING_MANUAL` 分支同样**不**
调用 `rollback_database()`，让这些已经真实发生的进度随调用方
`order_state.mark_provision_pending()` 的业务提交一并提交，而不是被
静默丢弃——manual-review 状态的本质是"业务结果不确定，需要人工核实"，
而不是"回滚重来"；已经真实发生的 `STORE_CREDENTIALS`/`APPLY_FORWARDER`
进度和"accounting 侧不确定"是两件独立的事，前者的真实性不应因为后者的
不确定性被抹去。`NO_SIDE_EFFECT`（确定失败，无 side effect）和
`CREATED`-disable-成功（已完整补偿）两条路径则维持本 ADR 之前的既有行为：
`rollback_database()` → `FAILED` → `raise`，把这次 provisioning 尝试
完整地撤销。

## 命名/接口最小化

`AccountingProvider` Protocol（`base.py`）本身签名不变——Python
`Protocol` 不声明异常类型，`create_user()` 仍然是
`(username, quota_bytes, expire_at) -> AccountUserDTO`，只是其文档新增
一句：真实 provider 在能够分类失败时应 `raise AccountingCreateUserError`
而不是裸露的 provider-specific 异常。`MarzbanAccountingProvider`
（`backend/app/providers/accounting/marzban.py`）是本仓库第一个、也是
目前唯一一个产生该分类的 provider；`create_user()` 内部通过两个已有的、
区分"是否曾经真正 dispatch 到网络层"的异常子类
（`MarzbanAuthenticationError`——认证失败，请求从未以合法身份到达
路由；`MarzbanTransportError`——请求已经 dispatch，但传输层失败，
结果不确定）加上对响应状态码的 exact-source 验证结果（`400`/`409` 均已
证明发生在 `crud.create_user()` 的 `db.commit()` 之前，因此
`NO_SIDE_EFFECT`；其余非 `200` 状态码——包括 5xx——`crud.create_user()`
本身在 `add_user()` 路由函数体内先于任何后续失败点执行 `db.commit()`，
无法从状态码本身证明提交发生在此之前，因此一律 `AMBIGUOUS`，不得默认为
`NO_SIDE_EFFECT`），把这一切映射到 `AccountingCreateEffect`。不引入任何
新的通用 retry/backoff/idempotency-key 机制。

## Pre-dispatch request-construction failures（第二次修订新增）

`create_user()` 在构造请求体时会调用 `_map_expire_to_marzban(expire_at)`，
这一步可能因为 naive datetime、或换算后 epoch `<= 0` 的 tz-aware
datetime而失败——这两种失败**都发生在任何网络请求被发出之前**，与
`400`/`409`/认证失败一样，是"绝对确定没有 side effect"的一类失败，
理应与它们分类为同一个 `NO_SIDE_EFFECT`。第二次修订前的实现遗漏了这一
点：`_map_expire_to_marzban()` 抛出的 `MarzbanContractError` 没有在
`create_user()` 内被捕获、规范化成
`AccountingCreateUserError(effect=NO_SIDE_EFFECT)`，而是原样向上传播，
落入 `ProvisioningService` "未分类异常 -> 视同 CREATED -> 尝试 disable"
的向后兼容默认分支——对一个从未尝试创建的 username 执行了
`disable_user()`，恰恰是本 ADR 存在的理由。

修复：`create_user()` 现在把 `_map_expire_to_marzban(expire_at)` 的调用
包在自己的 `try/except MarzbanContractError`，统一转换为
`raise AccountingCreateUserError(str(exc), effect=NO_SIDE_EFFECT) from exc`，
在构造请求体（因此也在任何 HTTP 调用）之前完成分类。这条规则对
`create_user()` 内**任何**未来的、发生在 dispatch 之前的本地/preflight
校验失败都成立，不只是 `expire_at` 一项——凡是在
`self._call("POST", "/api/user", ...)` 被调用之前抛出的异常，都必须先
规范化为 `NO_SIDE_EFFECT`，不得让它以裸 provider 异常类型逃逸。这不
改变 `set_expire()`/`set_quota()`/`disable_user()` 等其它方法的异常
contract——它们不产生 `AccountingCreateUserError`，本次修订只收紧了
`create_user()` 自己的 classification boundary。

## Business-facing pending reason vs. run-level diagnostic（第二次修订新增）

`ProvisionOutcome.pending_manual_error`（自由文本，run-level 诊断，写入
`Job`/run 审计记录）与 `Subscription.provision_error`（业务字段，人工
处置时实际看到的消息）此前被 `services.confirm_payment_and_provision()`
混为一谈：只要 `provision_prepare()` 返回任何 phase-A
`ProvisionOutcome(PENDING_MANUAL)`，该函数就无条件把 CREATE_TENANT 时代
的硬编码字符串 `"external tenant creation requires review"` 写入
`Subscription.provision_error`。ADR-018 首版新增了两类新的 phase-A
`PENDING_MANUAL` 来源（accounting `AMBIGUOUS`、`CREATED` 但 disable
补偿本身失败）之后，这个硬编码就变成了错误信息——人工处置人员会被
引导去检查一个完全无关的 subsystem（egress tenant creation）。

修复：新增 `PendingManualReason`（`backend/app/domain/provisioning.py`，
`StrEnum`，closed set）：

```python
class PendingManualReason(StrEnum):
    EXTERNAL_TENANT_CREATION = "EXTERNAL_TENANT_CREATION"
    ACCOUNTING_CREATE_AMBIGUOUS = "ACCOUNTING_CREATE_AMBIGUOUS"
    ACCOUNTING_COMPENSATION_FAILED = "ACCOUNTING_COMPENSATION_FAILED"
```

与一个固定、安全、可直接持久化的消息映射
`PENDING_MANUAL_BUSINESS_MESSAGES: Mapping[PendingManualReason, str]`——
每条消息都是本仓库自己写的静态字符串，从不嵌入任何 provider 异常的
`str()`、`repr()`、状态码、或原始 HTTP body。`ProvisionOutcome` 新增
`reason: PendingManualReason | None` 字段；`provision_prepare()` 的三个
phase-A `PENDING_MANUAL` 构造点（`CREATE_TENANT` 的两个异常分支、
accounting `AMBIGUOUS` 分支、`_compensate_created_accounting_user()`
的 disable-失败分支）各自标注正确的 `reason`。
`services.confirm_payment_and_provision()` 改为：

```python
business_message = (
    PENDING_MANUAL_BUSINESS_MESSAGES[prepared.reason]
    if prepared.reason is not None
    else "provisioning requires manual review"
)
order_state.mark_provision_pending(command, business_message)
```

不再硬编码、也不解析 `pending_manual_error` 的自由文本来猜测 reason
（该字段的内容和格式从未被当作可靠的 parse 目标）。`pending_manual_error`
继续作为 run-level 诊断保留（写入 `Job`/run 记录，供排查用），但不再是
`Subscription.provision_error` 的数据来源。

同时修正 `_compensate_created_accounting_user()`：此前 disable 本身失败
时，diagnostic 消息完全丢弃了 disable 失败的异常，只保留原始
create/postcondition 失败的文本。现在改为只携带两者的异常**类型名**
（`type(exc).__name__`/`type(disable_exc).__name__`），既保留了两次
失败的安全上下文，又不把任何 provider 异常的完整消息（可能在未来某个
provider 实现中意外携带更敏感的内容，即使当前 Marzban 适配器已验证过
不会）当作既成事实持久化——"安全失败上下文用类型/稳定代码，而不是原始
异常文本"这条原则，同时适用于 run-level 诊断字段和业务字段，只是业务
字段（`Subscription.provision_error`）额外要求完全静态、与 `reason`
一一对应，不包含任何动态内容。

## 422 validation rejection is NO_SIDE_EFFECT（第三次修订新增）

`POST /api/user`（pinned `app/routers/user.py::add_user`）的签名是：

```python
def add_user(
    new_user: UserCreate,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
```

`new_user: UserCreate` 是一个普通的 Pydantic-model 请求体参数——**没有**
包在 `Depends()` 里。FastAPI 的请求处理管线会在调用这个路由函数体之前，
先把入站 JSON body 解析并校验成 `UserCreate` 实例；校验失败会抛
`RequestValidationError`，FastAPI 的默认异常处理器把它转换成 HTTP
`422` 响应——这整个过程完全发生在 `add_user()` 函数体（因此也在
`crud.create_user()` 的 `db.commit()`）执行之前。这是 FastAPI 框架自身
的请求生命周期保证，不依赖 Marzban 的任何自定义逻辑，与本 ADR 已经用来
证明 `401`（`Admin.get_current` 这个 `Depends()` 在路由体之前执行）的
推理属于完全同一类"框架级保证"，只是这次保证来自请求体校验而不是
一个显式的 `Depends()`。

因此 `422` 与 `400`/`409` 一样，是"确定没有 side effect"的一类失败：
`create_user()` 现在把 `422` 加入判定 `NO_SIDE_EFFECT` 的状态码集合
`(400, 409, 422)`。修复前，`422` 落在"其余非 200 状态码" 的
`AMBIGUOUS` 分支里，会把一个确定的 pre-commit 拒绝错误地送进
`PENDING_MANUAL`——不仅语义上不准确，还会让 phase-A 已经 flush 的
`STORE_CREDENTIALS`/`APPLY_FORWARDER` 进度被保留下来（`PENDING_MANUAL`
路径不 `rollback_database()`，见"Business-facing pending reason vs.
run-level diagnostic"一节），而正确行为是像任何其它 `NO_SIDE_EFFECT`
失败一样完整回滚、标记 `FAILED`。

审查同时要求核实 `add_user()` 路径上是否还有其它可以从 exact source
证明发生在 create 之前的状态码。核对结果：没有。`add_user()` 自身的
`responses=` 声明只列出 `400`/`409`（`401` 来自路由级
`responses={401: ...}` 和 `Admin.get_current`），FastAPI 隐式的
`422` 来自请求体校验；`403`/`404` 不出现在 `add_user()` 路径（它们只
出现在 `get_user`/`modify_user` 等其它路由）。`crud.create_user()`
成功 `db.commit()` 之后的所有代码路径（`bg.add_task(...)`、
`UserResponse.model_validate(dbuser)`、`report.user_created(...)`、
`logger.info(...)`）仍然只能在提交**之后**失败，继续正确地保持
`AMBIGUOUS`——`NO_SIDE_EFFECT` 的范围没有被扩大到任何缺乏 exact-source
证据的状态码。

## 不做的事（显式拒绝）

- **不**实现 `409` 或任何其它状态的自动 reconciliation（GET 已存在用户
  并"接管"/合并）——这仍然是一个未做出的 idempotency 架构决策。
- **不**引入通用 provider retry 框架——本 ADR 严格限定在
  `create_user()` 一个方法的失败分类。
- **不**修改 ADR-016 Decision 3 的 `routing_principal` 契约、ADR-017 的
  named-lock 拓扑或 run-persistence-ordering 设计——两者原样复用。
- **不**让 `disable_user()`/`set_quota()`/`set_expire()`/
  `get_connection_links()`/`get_usage()` 的失败处理发生任何变化——本
  ADR 只影响 `create_user()` 的失败路径。

## 重新评估条件

- 若未来确有必要为 `409`（或其它确定的 pre-commit 拒绝）引入
  reconciliation 逻辑，需要一份独立的、显式的 idempotency ADR，而不是
  在本 ADR 或后续 PR 中静默加入。
- 若为其它 `AccountingProvider` 实现（非 Marzban）接入本契约，且其
  exact-source 无法证明某个状态码的 pre/post-commit 归属，应默认归类为
  `AMBIGUOUS`，不得靠猜测归类为 `NO_SIDE_EFFECT`。
