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

### S04-B2-B 的执行切分（**2026-09-19 重构：4 个 checkpoint**）

> **为什么重构：连续两次同一组 finding 未能完成。** 按 `docs/85` §6.1 的熔断
> 规则，同一 finding 连续 2 次修复失败后**不得原样再试**。
>
> **根因不是 Codex 不会写，是一次性要求的量与耦合度。** Round 1 的指令卡有
> 8 条,同时要求 receipt 绑定、repr 脱敏、SQL secret resolver、manifest
> builder、generation authority、concrete DB loader、preparation transaction、
> recovery、DNS gate、runtime readback、exact verifier，外加 **8 个测试文件
> ~25 个新测试** 与 2 条 MySQL 集成测试。**那不是一张指令卡，那是整个 B2-B。**
> 其中 loader / preparation / recovery / freshness 四项**互相依赖**，任何一项
> 没定下来，其余三项都无法收敛——这正是两次都停在同一处的原因。
>
> **本次只改执行粒度，不改架构结论，不降低任何最终验收要求，
> 不把任何 B2-B 要求推迟到 S04-C。** 下面四个 checkpoint 的并集
> **等于**原 B2-B 的交付项 1–8 与验收标准全集。

#### 依赖关系（单向，不得回环）

```
B2-B1  receipt / secret resolver / manifest / allocator / DNS gate
   │        ← 不依赖任何其它 checkpoint
   ▼
B2-B2  concrete DB desired loader + preparation transaction
   │        ← ⛔ 被 ADR-035 阻塞（deployment-owned 常量的落点，见下）
   ▼
B2-B3  freshness 复核 / recovery / runtime exact readback
   │
   ▼
B2-B4  完整 integration / guard / migration / concurrency 验收
```

**不得为了拆分制造任何过渡性不安全设计**：不得出现第二个 desired-state
authority、第二个 writer、临时 runtime fallback、或"先用 runtime 兜底"。
**每个 checkpoint 结束时，已接线的部分必须已经是 fail-closed 的终态**；
未接线的部分保持 B1 的注入式边界（`reconcile_mihomo_job()` 的四个参数），
**不得留下半接线状态**。

---

#### ⛔ 必须先补一条 ADR：deployment-owned 常量的**具体落点**

> **2026-09-19 更正（ChatGPT Round 1 审查 · Major 1/2，均已核实成立）。**
> 本节初稿把「新建版本化 deployment-constant DB 表」列为三个并列候选之一，
> 并说"两条 ADR 都提到它却都没定位它"。**两句都是错的：**
>
> **ADR-025 §3 的 authority taxonomy 已经裁定了 authority class**——
> deployment-owned validated constants 的 authority 是
> **`deployment, not DB`**，读取边界是
> **validated `Settings` / repo-owned deployment constants**，
> 并且紧跟着明写：**"Deployment constants are never migrated into the
> database"**。
>
> **所以"版本化 DB 表"不是一个合法候选。** 本 checkpoint 重构声明
> "只改执行粒度、不改架构结论"，而 DB authority 方案需要**先 supersede
> ADR-025**——那已经不是"架构不变"。**不要再把它当普通选项提出来。**
>
> **我错在哪**：只读了 ADR-025 §4（manifest 覆盖范围）就下结论，
> 没读 §3 那张 taxonomy 表——**而那张表正是答案所在**。
> 一找到能自圆其说的解释就停止搜索，是这次的实际失误。

**B2-B2 仍然被阻塞，但缺口比初稿窄得多，而且性质不同。**

##### 已经定了的（不要再讨论）

| 项 | 已定的结论 | 依据 |
|---|---|---|
| authority class | **deployment，不是 DB**；**永不迁入数据库** | ADR-025 §3 |
| 读取边界 | **validated `Settings` / repo-owned deployment constants** | ADR-025 §3 |
| 进 manifest 的方式 | 已验证的值**逐字（verbatim）**进 manifest，因而被 fingerprint 覆盖 | ADR-025 §3/§4 |
| **`api-secret-revision` 的来源** | **fresh 的 purpose-bound `Secret.revision`**，**不是**另一个配置来源 | ADR-025 §4/§8 |

##### 真正未定的：deployment-owned class **内部**的落点

`_validate_deployment_constants()` 的白名单有 7 个 key。**有默认值的只有 4 个**
（`mode`="rule"、`mixed-port`=7890、`allow-lan`=False、`log-level`="warning"）。
**三个没有默认值、必须由调用方提供**：

| key | 校验位置 | 现状 |
|---|---|---|
| `external-controller` | `_validate_controller_address()`，`None` 直接抛 `MIHOMO_CONTROLLER_ADDRESS_INVALID` | **无落点** |
| `api-secret-ref` | `_controller_identity()`，要求非空、无前后空白的安全字符串 | **无落点** |
| `api-secret-revision` | `_controller_identity()`，要求正整数（`bool` 不算） | 来源**已定**（purpose-bound `Secret.revision`），但要**经由哪个 deployment-owned 字段拿到 ref** 仍未定 |

> **初稿写的"7 个 key 里 6 个有默认值、只有 `external-controller` 没有"是错的。**
> 实际是 **4 个有默认值、3 个没有**；`api-secret-ref` 与 `api-secret-revision`
> 同样由 `_controller_identity()` 强制要求。**缺口不止 controller 地址。**

**所以未决问题只有一个，而且限定在 ADR-025 已画好的框内：**

> **`external-controller` 与 `api-secret-ref` 这两个 deployment-owned 必填值，
> 分别落在 validated `Settings` 还是 repo-owned fixed constants？**

两边都合法（ADR-025 §3 把两者并列为读取边界），但**代价不同**：

1. **validated `Settings`** —— 仓库已有同类先例（`core/config.py:62` 的
   `transport_cache_root`，并在 `validate_runtime_safety()` 里校验），
   **是最贴合 ADR-025 §3 措辞的一条**。代价：`core/config.py` 属 **S04-C**
   的允许清单，B2-B2 用它会把 S04-C 的范围提前拉进来，与"三阶段不得合并成
   一个 PR"冲突——**这是 TASK 层面的范围问题，不是架构问题**。
2. **repo-owned fixed constants** —— 不碰 `core/config.py`，B2-B2 可以独立完成。
   代价：`external-controller` 是环境相关的 `host:port`，写死不适合生产；
   B2-B 不做生产激活所以**本阶段**可行，但等于承认一个 S04-C 必须再改一次的
   临时落点，**必须明确记录而不是默认**。

> **需要的 ADR：`ADR-035 — Mihomo deployment-owned 常量的落点`。**
> 范围**仅限**在 ADR-025 §3 已裁定的 deployment-owned class 内部决定：
> `external-controller` 与 `api-secret-ref` 各自落在 validated `Settings`
> 还是 repo-owned fixed constants；若选 `Settings`，同时裁定 B2-B2 是否
> 破例动 `core/config.py`，还是等到 S04-C。
>
> **ADR-035 不得重新讨论 authority class，也不得引入 DB 落点**——
> 那需要先 supersede ADR-025，属另一件事。
>
> **在 ADR-035 被接受之前，B2-B2 / B2-B3 / B2-B4 不得开工。**
> **B2-B1 完全不受此阻塞**（它不构造 desired state，只消费传入的）。

---

#### 已由 ADR + 现有代码唯一确定的实现边界（**写死在这里，不要让执行者重新设计**）

这些是两次卡住时被反复重新推导的东西。**它们都有唯一答案，照抄即可：**

| 问题 | 唯一答案 | 依据 |
|---|---|---|
| **concrete loader 读哪些 DB 权威行** | 启用中的 `RouteGroup` / `RouteBinding` / `RouteEgressBinding`；`TrafficRule`；`EgressGroup` / `EgressEndpoint` / `EgressBinding`；`TransportProviderRecord` / `TransportEndpointRecord` / `EgressTransportAssignment`；`MihomoTransportMaterialization`（receipt）；`Secret`（仅 ref + revision） | ADR-025 §4 的清单逐项映射到仓库现有模型 |
| **transport → receipt → cache 的确定性解析路径** | `cache_path = cache_root / f"{record_id}-{safe_code}-{sha256('record_id\|code\|kind\|implementation')[:16]}.yaml"`，即 `TransportProviderResolver._cache_path()` **是唯一权威**；`cache_identity` 必须由**同一个四元组**导出，**绝不**从 subscription URL/token 构造 | `providers/transport/resolver.py:58-64` |
| **receipt 的精确绑定元组** | `(owner_record_id, provider_code, source_revision, cache_identity, content_hash, freshness_deadline)` 六项**全部**精确一致才放行 | ADR-025 §3a；`models/ops.py:189-200`；`mihomo_projection.py:403-405` 已用同样四元组做过比对 |
| **generation 分配与 intent 入队的 transaction owner** | **`prepare_mihomo_reconciliation()` 唯一拥有**：在现有 `mihomo_projection_write(db)` 命名锁内，fresh-read → manifest → allocate/reuse → 调用现有 `enqueue_mihomo_reconciliation()`，**两者同一个 DB transaction 提交**；`enqueue_mihomo_reconciliation()` 的签名**不改**（它只 `db.add` + `flush`，不 commit，天然可被复用） | ADR-025 §5；`mihomo_reconciliation.py:154-195` |
| **preparation 与 `reconcile_mihomo_job()` 的关系** | **完全分离，无调用关系。** preparation 是**生产者**（写 generation + intent）；`reconcile_mihomo_job()` 是**消费者**（`_claim()` 取 job 后执行）。两者只通过 `Job.payload_json` 的 `snapshot_revision` 相连 | `mihomo_reconciliation.py:550-590` |
| **recovery 的唯一入口** | `recover_mihomo_projection()`，**唯一入口**，只从 fresh DB + committed receipts 重建；缺失/不匹配/过期一律 `MIHOMO_STALE_DESIRED_SNAPSHOT`；**绝不**从 runtime/current YAML/backup/cache 反向铸 desired state 或 receipt | ADR-023 §1.2 第三行（runtime 只读 evidence）+ ADR-025 §6 |
| **apply 前与 finalization 前的 freshness 复核复用哪个 helper** | **新增一个** `confirm_fresh_generation(db, *, expected_revision) -> None`，**三处共用**：现有第一次 loader read 后的校验（`mihomo_reconciliation.py:588-590` 已存在，改为调用它）、`provider.apply()` 之前、写 `SUCCEEDED`/finalization_evidence 之前。三处**必须是同一个函数**，不得各写一份 | ADR-023 §3；ADR-025 §5 |
| **exact readback verifier 的输入/输出边界** | 输入：`candidate` + `operation_id` + `snapshot_revision`（即现有 `ProjectionVerifier.verify()` 签名，**不改**）；输出：`ProjectionVerification`。runtime 读回只作为 **evidence**，**绝不**进入 loader 或 desired state；不一致时 fail-closed，交给现有 `MIHOMO_RUNTIME_UNKNOWN` 路径 | `mihomo_reconciliation.py:90-93`；ADR-023 §1.2 |
| **`policy` 字段怎么办** | 现有 `_validate_sections()` 对非空 `policy` 直接抛 `MIHOMO_POLICY_UNSUPPORTED`（`mihomo_projection.py:191-192`）。因此 loader **必须**产出空 `policy`，manifest 以"空"覆盖它。**不要为了对齐 ADR-025 §4 的字面清单去实现 policy** | 现有代码 + ADR-023 行 24（无法表达 ownership 的字段必须归类，不得掩盖） |

---

#### B2-B1 —— receipt 绑定 / secret resolver / manifest + allocator / DNS gate

**目标**：把"消费 receipt、解析 controller secret、从**给定的** `DesiredForwarderState`
算出 canonical manifest 与 generation、以及 DNS candidate 校验"这四件**互不依赖**
的事做完做透。**本 checkpoint 不构造 desired state**——它消费调用方传入的。

**前置条件**：无（ADR-025 已 Accepted，TASK 已合并）。**不被 ADR-035 阻塞。**

**必须完成的 production wiring**：
- receipt **producer** 侧完成接线：`sync_nodes()` 对**传给 `write_provider_cache()`
  的同一份 bytes** 在写盘前算 SHA-256；`workers/transport_sync.py` 在
  endpoint inventory + provider status 的**同一 DB transaction** 内 upsert receipt，
  全流程只有最后一个 `db.commit()`。
- **consumer 侧**交付 `verify_materialization()` 与 `load_transport_materialization()`，
  但**此时尚无生产调用点**（调用点在 B2-B2 的 loader）——这是**允许的**，
  因为它们的正确性由本 checkpoint 的测试完整覆盖，且 B2-B2 会立即接上。
- `SqlMihomoControllerSecretResolver` 交付并可被注入；**接线到
  `reconcile_mihomo_job()` 的 `resolver` 参数在 B2-B2 完成**。
- DNS candidate schema gate **立即生效**（它在 `_validate_sections()` 内，
  是既有渲染路径的一部分）。

**允许修改的文件**（均已在 B2-B 总清单内，无新增）：
```
backend/app/infra/mihomo_materialization.py
backend/app/infra/mihomo_generation.py
backend/app/infra/credential_resolver.py
backend/app/models/ops.py
backend/app/models/__init__.py
backend/app/providers/transport/subscription.py
backend/app/providers/forwarder/mihomo_projection.py     # 仅 DNS schema gate
backend/app/workers/transport_sync.py
infrastructure/alembic/versions/0025_mihomo_projection_generations.py
infrastructure/alembic/versions/0026_mihomo_transport_materializations.py
backend/tests/unit/test_mihomo_materialization.py
backend/tests/unit/test_mihomo_generation.py
backend/tests/unit/test_transport_provider.py
backend/tests/unit/test_mihomo_projection.py
backend/tests/guards/test_mihomo_reconciliation_safety.py
backend/tests/guards/test_secret_leak.py
backend/tests/integration/test_models.py
```

**必须新增/通过的测试**（名称照抄，不要改名）：
- `test_mihomo_materialization.py`：`test_receipt_hash_mismatch_fails_closed_without_remint`、
  `test_receipt_cache_identity_mismatch_fails_closed`、
  `test_missing_or_expired_receipt_fails_closed`、
  `test_source_revision_mismatch_fails_closed`、
  `test_verified_materialization_repr_redacts_raw_content`
  —— 每条都断言 receipt 行数与内容**未被 verifier 改写**。
- `test_mihomo_generation.py`：保留 `test_mihomo_forwarder_is_still_rejected`；新增
  `test_generation_reuses_only_exact_latest_manifest`、
  `test_generation_a_b_a_allocates_new_revision`（三个 revision 严格递增且第三 ≠ 第一）、
  `test_manifest_excludes_materialized_raw_content_and_plaintext_secret`、
  `test_manifest_datetime_and_order_are_canonical`
- `test_transport_provider.py`：`test_subscription_sync_hashes_exact_bytes_before_atomic_cache_write`
  （monkeypatch `write_provider_cache` 捕获 bytes，断言 proof hash = 该 bytes 的 SHA-256）
- `test_mihomo_projection.py`：DNS 类型失败用例，至少断言 `{"enable": "true"}` 被拒
- `test_mihomo_reconciliation_safety.py`：producer-uniqueness guard
  —— 除 `workers/transport_sync.py` 外任何路径不得创建/upsert `MihomoTransportMaterialization`
- `test_secret_leak.py`：sentinel 放进 materialized YAML 与 controller secret，
  断言 receipt / generation / verified materialization 的 `repr()`、错误文本、日志均不出现
- `test_models.py`：表计数从 38 更新为 40（新增两表）
- `SqlMihomoControllerSecretResolver` 的 purpose/revision 校验测试
  —— **固定放在 `backend/tests/unit/test_mihomo_generation.py`**（该文件已在
  允许清单内）。**测试名自定，但必须存在。**

> **2026-09-19 更正（Round 1 审查 · Minor）**：本条原文写「或新建」，
> 而 B2-B1 的精确允许清单**没有**授权任何新测试路径。按本仓库的铁律，
> 「或新建」会诱导执行者越出 allowed-file list。**已固定到上面那个已授权
> 的文件，不留开放式"新建"。** 同类表述以后一律不写。

**明确不做**：不写 concrete DB loader；不写 preparation/recovery；不改
`mihomo_reconciliation.py`；不改 `mihomo.py`；不改 registry；不做 S04-C。

---

#### B2-B2 —— concrete DB desired loader + preparation transaction

**⛔ 前置条件：ADR-035 已 Accepted**（deployment constants 来源）**且 B2-B1 已合并。**

**目标**：`SqlMihomoDesiredSnapshotLoader` 从 DB 全量重建 `DesiredForwarderState`
（铁律 1），`prepare_mihomo_reconciliation()` 在锁内把 generation 与
identifier-only `MIHOMO_RECONCILE` intent 在**同一事务**提交。

**必须完成的 production wiring**：loader 与 resolver **真正接到**
`reconcile_mihomo_job()` 的参数上（替换注入式 fake）；preparation 成为
generation + intent 的**唯一**生产者。

**允许修改的文件**：`backend/app/infra/mihomo_reconciliation.py`、
`backend/app/infra/credential_resolver.py`、
`backend/tests/unit/test_mihomo_reconciliation.py`（均已在总清单内）。

**必须新增的测试**：`test_prepare_generation_and_intent_commit_together`、
`test_sql_controller_secret_resolver_requires_exact_purpose_and_revision`、
loader 全量 DB 重建与稳定排序的覆盖。

**明确不做**：不做 freshness 二次/三次复核；不做 recovery；不做 runtime readback；
不改 `mihomo_projection_lock.py` / `mihomo_blocker.py` 的公开签名。

---

#### B2-B3 —— freshness 三点复核 / recovery / runtime exact readback

**前置条件**：B2-B2 已合并。

**目标**：`confirm_fresh_generation()` 一个 helper 用于三处（见上表）；
`recover_mihomo_projection()` 唯一入口；`MihomoExactProjectionVerifier` 接到
`verifier` 参数上；`MihomoRuntime` / `LocalMihomoRuntime` 增加只读 readback。

**必须完成的 production wiring**：三处 freshness 复核全部生效；verifier 真正接线。

**允许修改的文件**：`backend/app/infra/mihomo_reconciliation.py`、
`backend/app/providers/forwarder/mihomo.py`、
`backend/tests/unit/test_mihomo_reconciliation.py`（均已在总清单内）。

**必须新增的测试**：`test_stale_snapshot_before_apply_causes_zero_runtime_mutation`、
`test_desired_change_before_finalization_cannot_mark_succeeded`、
`test_recovery_uses_fresh_db_and_never_runtime_as_desired_source`。

**明确不做**：不放行 registry；不接真实 Mihomo；不部署。

---

#### B2-B4 —— 完整 integration / guard / migration / concurrency 验收

**前置条件**：B2-B1–B3 全部合并。

**目标**：在**真实 MySQL 8.4** 上证明前三个 checkpoint 的不变式。

**允许修改的文件**：`backend/tests/integration/test_mihomo_reconciliation_mysql.py`、
`backend/tests/integration/test_db_adapters.py`（均已在总清单内）。

**必须新增的测试**：`test_mihomo_migrations_are_idempotent`（用 Alembic
`MigrationContext + Operations` 对 0025/0026 的 `upgrade()` 调用两次）；
真实 MySQL 上的 generation+intent 同事务、同 manifest 重用 latest、
A→B→A 新 revision、**两个并发 writer 只有一个 runtime apply**。

**明确不做**：不做 S04-C；不部署。

---

#### PR #156 应停在哪里

**#156 落 B2-B1，到此为止。**

- #156 当前 head `143c1e58f52f03967ece66e7ff56e2f01cb41127` 已经包含 B2-B1 的
  大部分骨架（generation allocator、materialization verifier、两个 migration、
  两个 ORM model、`test_models.py` 更新）；
- Codex **本地未提交**的工作（receipt exact binding、repr 脱敏、SQL controller
  secret resolver、canonical manifest、DNS validation、runtime readback 雏形）
  **全部落在 B2-B1 范围内，唯一例外是 runtime readback 雏形（属 B2-B3）**。
  **不要求丢弃任何本地修改**：readback 那部分**保留在工作树、暂不提交**，
  等 B2-B3 再提交即可；其余按 B2-B1 的验收补齐后提交。
- **安全切分点**：B2-B1 结束时系统处于一致状态——receipt producer 已完整接线，
  consumer 与 resolver 已交付且被测试完整覆盖但尚无生产调用点，
  `reconcile_mihomo_job()` 仍走 B1 的注入式边界，**没有半接线、没有临时 fallback、
  `FORWARDER_PROVIDER=mihomo` 仍被拒绝**。
- **B2-B2 起新开 PR**，因为它被 ADR-035 阻塞，不能让 #156 无限期挂着。

---

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
