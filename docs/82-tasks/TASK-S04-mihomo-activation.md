# TASK-S04-Mihomo 激活

> 建立本文件的原因（Review 5250709934 Major 3）：S04-B2-B 将触及
> `backend/app/models/`、`backend/app/infra/`、`infrastructure/alembic/versions/`
> 与凭据处理，按现行规则必须先有专属 S 系列 TASK。此前唯一被引用的是
> 遗留总括任务 `TASK-T16-real-provider-registry-wiring.md`，而它的
> 「允许修改的文件」**并不授权** 这些路径（实际只有
> `providers/`、`core/config.py`、`tests/`、两处 docs）；同时现行约定明确
> 不再新开 T 系列编号。
>
> 架构依据：**ADR-023**（全量配置与激活边界）、**ADR-025**（projection
> generation authority + transport materialization receipt）。
> 冲突时 ADR > AGENTS.md > REVIEW。

## 目标

把 Mihomo 从"已有投影与持久化协调基础、但不可达"推进到"可在生产被选中并
激活"，分三个**各自独立 PR**的阶段。当前 `build_registry()` 对
`FORWARDER_PROVIDER != "mock"` 一律抛 `ProviderConfigurationError`。

### S04-B2-A — ADR / 架构（**COMPLETE**，PR #128 已合并）

交付 ADR-025：全局 projection generation authority、authority taxonomy
（保留 ADR-023 §1.2 三类输入 + deployment constants 的读取边界）、
durable transport materialization receipt 的 producer/commit/crash 契约、
以及 B2-B 的 planned schema。**仅文档，不含任何实现。**

### S04-B2-B — 实现（**READY，下一个要做的任务**）

> **2026-09-19：两道前置闸门都已满足，可以开工。**
> - ADR-025 状态已是 **`Accepted`**（PR #128 已合并，状态行已按该 ADR 自己的
>   迁移契约翻转）；
> - 本文件已合并。
>
> **不要再把 ADR-025 的状态当作 blocker。** 开工前仍要读 ADR-025 正文，
> 但那是读**内容**，不是等**状态**。

交付：

1. **两个 additive migration + 两个 ORM model**（ADR-025 §8）：
   `mihomo_projection_generations` 与 `mihomo_transport_materializations`。
2. **materialization receipt producer**：成功的 transport sync 在写完
   atomic cache 之后、于**同一个 DB transaction** 内 upsert 非机密 receipt
   （ADR-025 §3a 的 5 步顺序）。
3. **canonical manifest builder + fingerprint + generation allocator**。
4. **真实 desired snapshot loader**：从 DB 全量渲染（铁律 1），消费
   receipt 而非对当前文件现算 hash。
5. **SQL controller-secret resolver**（purpose-bound，短事务，网络 I/O 之前）。
6. **exact runtime readback verifier** 与 candidate 校验。
7. **controlled recovery service**：receipt 缺失/不匹配一律 fail closed，
   **绝不**从磁盘文件重新铸造 receipt。
8. Mihomo DNS 字段/类型的 candidate-schema 校验闸门（S04-A 只保证了
   canonical mapping/value ownership）。

### S04-C — 注册表放行与生产选择（NOT STARTED）

`build_registry()` 接受 `FORWARDER_PROVIDER=mihomo`，
`Settings.validate_runtime_safety()` 加入与 Marzban 同级的 fail-closed 校验，
`.env.example` 补齐变量，lifecycle/factory 边界。**生产激活本身仍需 User
明确批准，不由本任务授权。**

## 约束

- **B2-B 的两个前置闸门缺一不可**：ADR-025 状态为 `Accepted`，且本文件已合并。
  **（2026-09-19：两条都已满足，闸门已开。）**
- **铁律 1**：Mihomo 配置一律从数据库全量生成，禁止增量拼接。
- **铁律 3**：不提供任何 `force` / `override` / `bypass` 开关。
- **铁律 7**：不得改写任何已有 Alembic revision；两个新迁移必须**幂等**。
- **不得削弱 `write_provider_cache()` 现有的 atomic write → fsync → replace**。
- **receipt 只能由成功的 transport sync 生产**；loader、recovery、运维脚本
  一律不得铸造 receipt。
- **crash 语义必须 fail closed**：新文件 + 旧 receipt ⇒ hash mismatch ⇒
  拒绝，不得自动信任、不得自动修复。
- **receipt 表不得存**：cache 内容、凭据、subscription URL/token、
  渲染后的候选配置。新增列须同时满足 ADR-025 §8.3 三项，否则要新 ADR。
- 构造必须零 I/O（`build_registry()` 不得发起网络请求或读运行时状态）。
- 凭据只能以 `secret_ref` 流经投影层；`ProjectionTemplate` 在 `finalize()`
  之前必须保持 secret-free、不可安装。
- **三个阶段不得合并成一个 PR。** B2-B 不得顺带放行注册表；
  S04-C 不得顺带改协调语义。
- **不得触碰 `backend/app/domain/`**（本任务全部工作在
  models / infra / providers / workers / registry 层）。
- 不得触碰 `deploy/`、生产凭据、或任何真实外部写操作。

## 允许修改的文件

未列出的路径一律不得修改。笼统的 `backend/app/**` 不被接受。

### S04-B2-A（PR #128，仅文档）

```
docs/80-decisions/ADR-025-mihomo-projection-generation-authority.md
docs/82-tasks/TASK-S04-mihomo-activation.md
docs/82-tasks/TASK-T16-real-provider-registry-wiring.md
docs/83-project-continuity.md
```

### S04-B2-B

```
backend/app/models/__init__.py
backend/app/models/egress.py
backend/app/models/ops.py
backend/app/infra/mihomo_reconciliation.py
backend/app/infra/mihomo_projection_lock.py
backend/app/infra/mihomo_blocker.py
backend/app/infra/mihomo_generation.py              # 新增：manifest/fingerprint/allocator
backend/app/infra/mihomo_materialization.py         # 新增：receipt 读取与校验
backend/app/infra/credential_resolver.py
backend/app/providers/forwarder/mihomo.py
backend/app/providers/forwarder/mihomo_projection.py
backend/app/providers/transport/subscription.py     # 仅为 receipt producer 计算 content_hash
backend/app/workers/transport_sync.py               # 仅为在同一事务内提交 receipt
infrastructure/alembic/versions/0025_mihomo_projection_generations.py     # 新增
infrastructure/alembic/versions/0026_mihomo_transport_materializations.py # 新增
backend/tests/unit/test_mihomo_projection.py
backend/tests/unit/test_mihomo_reconciliation.py
backend/tests/unit/test_transport_provider.py
backend/tests/unit/test_mihomo_generation.py        # 新增
backend/tests/unit/test_mihomo_materialization.py   # 新增
backend/tests/guards/test_mihomo_reconciliation_safety.py
backend/tests/guards/test_secret_leak.py
backend/tests/integration/test_mihomo_reconciliation_mysql.py
backend/tests/integration/test_db_adapters.py
backend/tests/integration/test_models.py                # 2026-09-19 补入，依据见下
docs/80-decisions/ADR-025-mihomo-projection-generation-authority.md      # 仅状态改为 Accepted
docs/82-tasks/TASK-S04-mihomo-activation.md
docs/83-project-continuity.md
```

### 迁移编号：**已更新为 `0025` / `0026`**（2026-09-19）

原文预留 `0024` / `0025`。**S07（PR #152）已占用 `0024_usage_period_queue`**，
实测 `main`（`f23bb39c7b6243fd854f52a1b96ec3a0018e4419`）上最大编号就是 `0024`。
两个新迁移因此定为：

| 文件 | `down_revision` |
|---|---|
| `0025_mihomo_projection_generations.py` | **`0024_usage_period_queue`** |
| `0026_mihomo_transport_materializations.py` | **`0025_mihomo_projection_generations`** |

**铁律 7：不得改写任何已有 revision。** 两个新迁移必须**幂等**
（表存在则跳过），且只能 additive。

**开工时仍然要自己再确认一次编号**：对着当时的 `main` **加上所有 open PR**
一起查，取下一个连续编号；与本文件写的不一致时以实际为准，并在 PR 描述里
说明。只看 `main` 会撞号（2026-09-18 已因此撞过一次）。

### 2026-09-19 范围闭包检查（S04-B2-B，开工前）

沿四条轴各走一遍，对着 `main`（`f23bb39c`）实测。**共补入 1 个文件。**

| 轴 | 查了什么 | 结果 |
|---|---|---|
| **数据库 schema** | 新增两张表会打破什么 | ❌ **发现漏项**，见下 |
| **函数 / 协议** | B2-B 触及模块的公开签名与协议落点 | 未发现可证明的漏项 |
| **API 契约** | B2-B 是否产生 API 表面 | 无——交付物全在 `infra/` `providers/` `workers/` `models/`，不碰 `api/` 或 `schemas/` |
| **渲染字段** | projection 渲染相关文件 | `mihomo_projection.py` 与其单测均已在清单内 |

**补入：`backend/tests/integration/test_models.py`**

依据是一行硬断言：

```python
# backend/tests/integration/test_models.py:11
assert len(Base.metadata.tables) == 38
```

实测当前 `main` 上正是 **38** 张表。B2-B 新增 `mihomo_projection_generations`
与 `mihomo_transport_materializations` 两张，**这个断言必然失败**，而该文件
原本不在清单里。

> **这与 S07 第二次漏项是同一类**（`UsagePeriod.order_id NOT NULL` 打破了清单外
> 的既有 fixture）。**区别是这次由开工前的闭包检查发现，不是由 CI 发现**——
> 少一轮返工。

**查过但明确不加（防范围蔓延）：**

- `backend/tests/guards/test_persistent_state_manifest.py` —— 它校验的是
  `docs/90-migration/persistent-state-manifest.md` 里的 compose 状态与独立
  SQLite 路径，**不枚举数据库表**，新增表不影响。
- `backend/tests/unit/test_drift_check.py` —— 它按**具名**表
  （`audit_logs` / `egress_endpoints` / `egress_groups`）从 metadata 里取，
  不做全量计数，新增表不影响。
- `backend/tests/unit/test_desired_routing_snapshot.py` —— 它覆盖的是
  **Xray** 路由快照（`XrayOutboundDTO`、`GatewayRouteBinding`），
  不是 B2-B 第 4 项的 Mihomo desired snapshot loader。
- `backend/app/providers/base.py` —— receipt producer 的改动写在
  `providers/transport/subscription.py` 与 `workers/transport_sync.py` 内部
  （"仅为计算 `content_hash`" / "仅为同事务提交 receipt"），**没有证据表明
  需要改 Protocol**。要改它属于 `AGENTS.md` 铁律 5 的范围，**停下来上报**。

> **一条给执行者的补充规则（这次闭包检查的副产品）：**
> `backend/tests/integration/test_mihomo_projection_lock.py` **不在清单里**，
> 而它直接 import 了 `mihomo_projection_lock` / `mihomo_blocker` /
> `mihomo_reconciliation` 三个 B2-B 允许修改的模块。目前**无法证明**它必然
> 需要改（它测的是命名锁语义，不是 generation 分配）。
> **所以：如果你的改动动到了这三个模块的公开签名，停下来上报，不要自行
> 修改那个测试文件。** 这属于 `docs/86` §7 的"需要改清单外文件"情形。

### S04-C

```
backend/app/providers/registry.py
backend/app/core/config.py
backend/app/main.py
backend/app/workers/scheduler.py
.env.example
backend/tests/unit/test_registry.py
backend/tests/unit/test_application_lifecycle.py
backend/tests/unit/test_scheduler.py
docs/82-tasks/TASK-S04-mihomo-activation.md
docs/83-project-continuity.md
```

## 验收标准

```sh
make lint
python -m pytest backend/tests/unit backend/tests/guards
python -m pytest backend/tests                # 需 TEST_DATABASE_URL（MySQL 8.4）
```

### S04-B2-A

- ADR-025 状态为 `Proposed`，且含明确的状态迁移归属（谁在什么时候改成
  `Accepted`）。
  > **这是 B2-A 交付当时的验收条件，已满足，属历史记录。**
  > PR #128 合并后，该状态已于 2026-09-19 按其自身迁移契约翻转为
  > **`Accepted`** ——**不要照这一行把它改回 `Proposed`**。
- authority taxonomy 保留 ADR-023 §1.2 三类输入，并单独给出 deployment
  constants 的读取边界；文中不再出现"所有 authoritative input 都来自
  owning committed DB rows"这类压平表述。
- receipt 的 producer / commit / crash 三段契约齐备，且明确写出
  "新文件 + 旧 receipt ⇒ fail closed"。
- planned schema 覆盖两张表，并以 §8.3 的三项测试取代 "at least"。
- `git diff --check` 无输出。

### S04-B2-B

- **receipt 绑定性**：一条测试证明 loader 在 cache 文件被改动、而 receipt
  未更新时**拒绝**（mismatch ⇒ fail closed），且**不**重新铸造 receipt。
- **crash 顺序**：一条测试模拟"文件已写、receipt 未提交"，断言后续
  reconciliation fail closed 而非自动信任。
- **producer 唯一性**：一条测试/守卫证明只有 transport sync 路径写 receipt。
- **全量渲染**：一条测试证明 desired snapshot 来自 DB 全量渲染，而非在既有
  配置上增量修改（铁律 1）。
- **generation 语义**：append-only；A→B→A 得到新 revision（10=A, 11=B, 12=A），
  revision 10 永不复用。
- **无凭据泄漏**：`test_secret_leak.py` 覆盖 receipt、generation、投影与候选
  配置的 `repr()` 与日志路径；断言 receipt 表不含 URL/token。
- **迁移幂等**：两个新迁移在已存在对象时可重复执行（铁律 7）。
- **边界证明**：一条测试断言本阶段结束时 `FORWARDER_PROVIDER=mihomo`
  **仍然**被 `build_registry()` 拒绝。

### S04-C

- **零 I/O 构造**：`FORWARDER_PROVIDER=mihomo` 下 `build_registry()` 不发起
  任何网络请求。
- **fail-closed 选择**：控制端点/secret 缺失或为占位值时
  `validate_runtime_safety()` 抛错，而不是构造出半配置的 provider。
- `make test-unit` 在无网络、无 Docker 下继续通过。

### 人工检查（三个阶段共同）

- PR 描述必须写明本阶段**不做**什么。
- 生产激活是独立的人工闸门，需要 User 明确批准，**不因本任务任一阶段合并
  而自动获得**。
