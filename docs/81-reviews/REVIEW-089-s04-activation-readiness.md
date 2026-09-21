# REVIEW-089：S04 激活就绪性审计（2026-09-21，User 触发）

- 触发方式：**User 在对话里明确要求**做一次全仓扫描（ADR-034 之后审查只
  由 User 开口触发，没有任何定时或事件会产生本文件）。
- 审计基线：`main` = `c23ac8b`（PR #168 合并后）。
- 范围：全仓，重点是**架构与接下来的计划**。
- 产出：本文件 + `TASK-S04` / `docs/85` §5 / `docs/83` §8 的同步更新。
- **本次未改任何生产代码，未改任何 ADR。**

> **编号说明**：`docs/81-reviews/` 此前只有 `REVIEW-083`（按主题取号，
> 非严格递增）与一个流程测试夹具。本文件取 **089**，与 `docs/8x` 的既有
> 文件名不冲突。`docs/85` §5.8 的防撞规则只管 ADR 与 TASK 编号，
> 不管 REVIEW，本次未占用任何 ADR/TASK 号。

---

## 0. 一句话结论

**S04 按当前计划全部做完之后，Mihomo 仍然既不会被触发、也不会被运行；
而 S04-C 一旦真的放行 `FORWARDER_PROVIDER=mihomo`，现有开通链路会在
`APPLY_FORWARDER` 步骤逐次失败。** 两条都不是实现瑕疵，是**计划里没有
任何 checkpoint 认领的缺口**。

机械检查本身是干净的：`ruff check backend ops infrastructure scripts`
全过，`pytest backend/tests/guards` 246 passed，全仓零 `TODO`/`FIXME`/
`NotImplementedError`。**问题不在代码质量，在接线与计划覆盖面。**

---

## 1. `C1` —— 开通链路的 forwarder 契约与 Mihomo 实现不兼容（**Critical**）

### 1.1 实测证据

两处不兼容，**各自独立成立**，修好一处另一处仍在。

**(a) desired state 的形状对不上。**

`backend/app/infra/provisioning_state.py:193-198` 的
`desired_forwarder_state()` 产出的是：

```python
DesiredForwarderState({endpoint_id: f"{tenant_id}:{secret_ref}"})
```

即只填 **legacy 的 `listeners: Mapping[str, str]`** 字段，
`listener_specs` / `egress_proxies` / `deployment_constants` 全为空。
实测把这个形状交给 Mihomo 渲染：

```
legacy.listeners      = {'1': 'tenant-abc:egress/1'}
legacy.listener_specs = ()
legacy.egress_proxies = ()
compose: MihomoProjectionError: MIHOMO_CONTROLLER_SECRET_REF_INVALID
```

> **一处必须记下的自我更正**：我最初判断它会抛
> `MIHOMO_LEGACY_LISTENERS_UNSUPPORTED`（`mihomo_projection.py:191`）。
> **实测不是** —— `_validate_deployment_constants()` 在 legacy-listeners
> 检查**之前**运行，所以真正抛出的是
> `MIHOMO_CONTROLLER_SECRET_REF_INVALID`。结论不变，错误码不同。
> 记在这里是因为**下游要写的测试必须断言实测的那个码**。

**补偿路径同样失败。** `provisioning.py:435` 在 `APPLY_FORWARDER` 失败时
执行 `self.forwarder.apply(self.forwarder.render(previous_forwarder))`，
而 `previous_forwarder` 来自 `current_forwarder_state()`
（`provisioning_state.py:182-191`），形状同为 legacy `listeners`：

```
compensation compose: MihomoProjectionError: MIHOMO_CONTROLLER_SECRET_REF_INVALID
```

顺带注意：这两个方法的 `listeners` **值语义还不一样** ——
`current_forwarder_state()` 填的是 `mihomo_listen_port`，
`desired_forwarder_state()` 填的是 `"{tenant}:{secret_ref}"`。
同一个字段两种含义，本身就是一处需要收口的历史遗留。

**(b) 协议步数对不上（与 (a) 无关，独立成立）。**

| 实现 | 步数 |
|---|---|
| `ForwarderProvider` Protocol（`base.py:583-588`） | `render → apply`（**2 步**） |
| `MockForwarderProvider` | `render → apply`（2 步） |
| `MihomoForwarderProvider`（`mihomo.py:200/217/243`） | `render → finalize(resolver) → apply`（**3 步**） |

`domain/provisioning.py:432` 调的是 2 步：
`self.forwarder.apply(self.forwarder.render(desired_forwarder))`。
而 `MihomoForwarderProvider.apply()`（`mihomo.py:244-247`）要求
`isinstance(candidate, MihomoCandidateConfig)` 且 finalization proof 有效。
实测：

```
render() 的返回类型 ProjectionTemplate 是 MihomoCandidateConfig 吗: False
=> apply() 的 isinstance 检查必然失败
```

**类型系统抓不到这条。** `ProjectionTemplate` 是 `CandidateConfig` 的子类
（`base.py:337`），所以 `render() -> ProjectionTemplate` 是对
`-> CandidateConfig` 的合法收窄，`mypy --strict` 全绿。
**这正是它至今没被发现的原因。**

### 1.2 后果

S04-C 放行 `FORWARDER_PROVIDER=mihomo` 的那一刻：

1. 每一次开通走到 `APPLY_FORWARDER` 都抛错；
2. 补偿 `render(previous_forwarder)` 抛同一个错；
3. 按 ADR-026，补偿失败 ⇒ 落 `PENDING_MANUAL`，**且 `CREATE_TENANT` 创建的
   外部 tenant 已经存在**、凭据已存库。

**是 fail closed 的**（不会静默配错），但结果是开通全线停摆。

### 1.3 这不是"实现时顺手处理"

`domain/**` 与 `providers/base.py` 受**铁律 5** 约束：改协议形状属于改架构
决策，**必须先有 ADR**。而现有 ADR 里没有一条裁定过
「`ForwarderProvider` 是 2 步还是 3 步」「legacy `listeners` 何时退役」。
`ADR-037 §8` 只说了不授权激活，没说这条链路怎么接。

**归属：尚无 owner。** S04-B2-B2c 只做 render/finalization 内部；
S04-C 的目标与验收标准（TASK-S04）只写 `build_registry()` 接受、
`.env.example`、fail-closed 校验 —— **没有一条涉及 `domain/provisioning.py`**，
`domain/` 甚至不在 S04-C 的允许文件清单里。

---

## 2. `C2` —— Mihomo reconciliation 既无生产者也无运行者（**Critical**）

### 2.1 实测证据

```
reconcile_mihomo_job 的全部引用：
  backend/app/infra/mihomo_reconciliation.py:550   （定义本身）
  backend/tests/**                                  （4 处，全是测试）
scheduler 里的 mihomo 提及：无（workers/ 下零命中）
```

`enqueue_mihomo_reconciliation()` 的唯一调用者是
`prepare_mihomo_reconciliation()`，而后者在 `main` 上**尚不存在**
（属 B2-B2.6），落地后也**没有任何调用者**。

### 2.2 对照：Xray 侧有完整的三件套

| 角色 | Xray（已存在） | Mihomo（不存在） |
|---|---|---|
| **producer**（业务事件入队） | `services.py:223`、`workers/accounting_sync.py:475` 调 `enqueue_gateway_reconciliation()` | 无 |
| **runner**（后台排干） | `main.py:22-53` 的 `_gateway_reconciliation_loop()`，在 FastAPI lifespan 里按 `gateway_provider == "xray_file"` 起 asyncio task | 无 |
| **inline**（同事务内处理） | `reconcile_gateway_job_in_session()` | 无 |

### 2.3 后果

B2-B2.1 → .6 → B2-B2c → B2-B2d → B2-B3 → B2-B4 → writer → S04-C
**全部合并之后**，系统里依然没有任何代码会创建一条 `MIHOMO_RECONCILE` job，
也没有任何进程会去认领它。整条流水线是**可构造、可测试、被门控的死代码**。

`S04-C` 的允许文件清单里**确实**有 `main.py` 与 `workers/scheduler.py`
（TASK-S04「允许修改的文件 / S04-C」），但它的**目标与验收标准**里没有
任何一条要求建 producer 或 runner。**文件在清单里 ≠ 工作在计划里** ——
按现在的 S04-C 描述派活，执行者没有任何理由去写这两样东西。

### 2.4 为什么这是架构问题而不是实现问题

「什么事件应该触发一次 Mihomo reconcile」是一个**需要裁定的设计选择**，
不是可以顺手发明的。候选触发源至少有：出口增删、`EgressBinding` 变化、
凭据轮换（`Secret.revision` 递增）、ACTIVE assignment 变化、
transport 重新 materialize。选哪些、在哪一层入队、与 Xray 的
`enqueue_gateway_reconciliation()` 是否共用事件点 —— 都会改变
fingerprint 的产生频率与锁竞争面。**按 ADR-036 §2，这类缺口报告 User，
不由执行者自选。**

---

## 3. `M1` —— `plans` 表没有任何 seed 路径（**Major，上线阻断**）

- `GET /plans`（`api/public.py:112-131`）**从数据库读** `Plan` 行；
- 全仓唯一提到 `Plan(` 构造的地方是 `models/billing.py:54` 的类定义本身 ——
  **没有任何代码创建 `Plan` 行**；
- `deploy/lib/60_seed.sh:21` 刻意拒绝隐式种子
  （`SEED_COMMAND must be explicitly configured`）。

⇒ **全新部署上 `/plans` 返回空数组，客户看不到任何套餐，下不了单。**

`docs/83` §8 第 4 条记过这件事，但措辞是「declared but not yet in effect」，
**低估了严重度** —— 它不是"价格没生效"，是"商品列表为空"。
`docs/85` §5.6 第 3 条把它记成"人工核对"，**而它根本不在派发队列里**。

本审计不替 User 决定怎么解决（写 seed 脚本 vs 建库时人工插行 —— 前者意味着
本仓库开始在部署期写业务数据，`docs/83` §8 已指出那需要 User 签字）。

---

## 4. 次要项

### 4.1 `m1`：`docs/83` §8 第 4 条的收尾段已过期（本次已修）

原文写着 `RENEWAL` / `UPGRADE` / `ADDON` 「仍是 `api/admin.py:159-169` 的
未实现 stub」且「**完全没有周期滚动代码**」。**两句都已不成立**：

- `api/admin.py:161-168` 三个方法现在都路由到 `enqueue_subscription()`；
- `workers/accounting_sync.py:181` 的 `apply_expiry_policy()` 有真实实现，
  docstring 就是「Close an exhausted/expired period and activate the FIFO
  queued period」，并按 `UsagePeriodStatus.QUEUED` 取队首。

S07（PR #152）落地后没有回头修这段，属**改一处、别处现状文字变陈旧**
的老毛病。本次随本 PR 改正。

### 4.2 `m2`：前端单行组件问题仍然存在（`docs/83` §8 第 6 条准确）

实测 8 个客户端页面各有 1–2 行超过 400 字符：
`plans` / `orders/new` / `orders` / `register` / `login`（2 行）/
`subscriptions/[id]` / `subscriptions` / `portal`。
`eslint` / `tsc` 全过，**功能没问题**，但 diff 不可读，与 `AGENTS.md`
「PR 时间线要能完整重建」相冲突。**本次不改**（属前端重构，需要独立 TASK）。

### 4.3 `m3`：本容器里 `mypy` 的输出全是环境噪音（**给后续 session 的坑**）

`CLAUDE.md`「Implementation」要求推送前跑
`mypy backend/app backend/tests`。在本远程容器里直接跑会得到
**352 条错误**，其中 189 条 `import-not-found` + 101 条
`untyped-decorator`，**全部源于 `mypy` 装在 `/root/.local/bin`（pipx 独立
venv），看不到项目的 site-packages**。CI 的 `lint` job 是先
`pip install -e ".[dev]"` 再 `make lint`，所以 CI 绿是真的绿。

**不要把这 352 条当成代码缺陷去"修"。** 本地要有意义地跑 mypy，
必须让它跑在装了项目依赖的解释器里。

---

## 5. 建议的处置（**本文件不替 User 做决定**）

| # | 缺口 | 建议 | 谁来定 |
|---|---|---|---|
| C1 | forwarder 契约不兼容 | **需要一条新 ADR**：裁定 `ForwarderProvider` 是 2 步还是 3 步、legacy `listeners` 何时退役、`domain/provisioning.py` 的 `APPLY_FORWARDER` 在 Mihomo 模式下是直接驱动还是改为入队。触及 `domain/` + `providers/base.py`，**铁律 5 要求先有 ADR** | **User**（ADR-036 §2）→ 决定后由 Claude 写 |
| C2 | 无 producer / 无 runner | **需要一条新 ADR 或至少一个新 checkpoint**：裁定哪些业务事件入队、runner 放 lifespan 还是 scheduler | **User**（同上） |
| M1 | `plans` 无 seed | 需要 User 拍板「本仓库是否在部署期写业务数据」 | **User** |
| m1 | docs/83 过期段落 | 本 PR 已修 | — |
| m2 | 前端单行 | 独立 TASK，不在 S04 范围内 | 排期问题 |
| m3 | mypy 环境噪音 | 已记录在本文件，供后续 session 参照 | — |

> **C1 与 C2 都是 S04-C 的硬前置。** 在它们被裁定之前派 S04-C，
> 等于把一个"放行即停摆"的开关合进 `main`。本审计已把这两条写进
> `TASK-S04` 与 `docs/85` §5.1 的闸门状态。

---

## 6. 本次检查过但**没有**发现问题的地方（避免以后重复审）

- **铁律 1（全量重建）**：`full_desired_routing_snapshot()` 与
  `SqlMihomoDesiredSnapshotLoader.load_current()` 都是全量 SELECT 重建，
  没有增量拼接路径。
- **铁律 2（BLOCK 兜底）**：Xray 侧 `xray_composition.py:656-661` 强制
  private BLOCK 在首、`tcp,udp` BLOCK 在末，违反即抛；Mihomo 侧
  `mihomo_projection.py:279-286` 强制**恰好一条** `MATCH` 兜底、必须在
  最后、且**不得是 `DIRECT`**。两侧都是校验而非仅拼接。
- **铁律 4（账务不 DELETE）**：全仓无 `delete_user` / `remove_user` 调用。
- **铁律 7（Alembic 历史冻结）**：`pyproject.toml` 的 `extend-exclude`
  把 `infrastructure/alembic/versions/` 排除在 ruff 之外，理由正确
  （lint 只会产出"必须不修"的 finding）。
- **凭据边界**：`desired_forwarder_state()` 放进 `listeners` 的是
  `secret_ref`（opaque 引用）而非明文，不违反 ADR-037 §6。
  它的问题是形状（C1），不是泄漏。
- **代码卫生**：全仓零 `TODO` / `FIXME` / `XXX` / `NotImplementedError`；
  `ruff` 全过；`pytest backend/tests/guards` 246 passed。
- **断路器**：`.github/automerge-enabled` = `false`，与 `AGENTS.md` 铁律 8
  和 `docs/85` §0 的记述一致。
- **`docs/86`**：TASK-S04 引用的 `docs/86-codex-operating-instructions.md`
  确实存在，不是悬空引用。
