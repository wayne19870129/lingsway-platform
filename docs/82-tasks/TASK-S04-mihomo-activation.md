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
>
> **2026-09-20 进度：B2-B1 已完成并合并（PR #156）。下一个是 B2-B2，
> 它没有任何待解除的阻塞项**——deployment 常量的落点已由 **ADR-035** 裁定，
> 结论逐条写死在下面。

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
   │        ← 只依赖 B2-B1（#156 已合并）。deployment 常量落点见 ADR-035
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

#### deployment-owned 常量的落点 —— **ADR-035 已裁定，照抄即可**

> **2026-09-20：本节原来是一个 ⛔ 阻塞项（"必须先补一条 ADR"）。
> `ADR-035-mihomo-deployment-constant-placement.md` 已给出结论，
> 阻塞解除。B2-B2 / B2-B3 / B2-B4 不再等待任何人再确认一次。**
>
> 下面是结论本身，**不要重新推导，也不要重新讨论候选方案**。
> 完整论证与被否决的替代方案见 ADR-035 §2–§5。

##### 已定的 authority class（ADR-025 §3，本节不重开）

| 项 | 已定的结论 |
|---|---|
| authority class | **deployment，不是 DB**；**永不迁入数据库** |
| 读取边界 | **validated `Settings` / repo-owned deployment constants** |
| 进 manifest 的方式 | 已验证的值**逐字（verbatim）**进 manifest，因而被 fingerprint 覆盖 |
| `api-secret-revision` 的来源 | **fresh 的 purpose-bound `Secret.revision`**，**不是**另一个配置来源 |

> **"版本化 DB 表"不是合法候选**，走它必须先 supersede ADR-025。
> 本节初稿曾把它列为并列候选（2026-09-19 经审查更正）——**不要再提出来**。

##### 七个 key 各自的落点（**这就是全部答案**）

`_validate_deployment_constants()` 白名单 7 个 key，**4 个有默认值、3 个没有**：

| key | 落点 | 取值 |
|---|---|---|
| `mode` | 函数内默认值 | `"rule"` |
| `mixed-port` | 函数内默认值 | `7890` |
| `allow-lan` | 函数内默认值 | `False` |
| `log-level` | 函数内默认值 | `"warning"` |
| **`external-controller`** | **validated `Settings`** | 新字段 `mihomo_external_controller: str = ""`（ADR-035 §3） |
| **`api-secret-ref`** | **repo-owned constant** | `MIHOMO_CONTROLLER_SECRET_REF = "mihomo/api-secret"`，放在 `credential_resolver.py` 里 `MIHOMO_CONTROLLER_SECRET_PURPOSE` 旁边（ADR-035 §4） |
| `api-secret-revision` | resolver 读出的 `Secret.revision` | 不来自任何常量或 `Settings` |

##### `external-controller` 的两处校验（**职责不重叠，都不许省**）

| 位置 | 只做什么 |
|---|---|
| `Settings.validate_runtime_safety()` | **只查"有没有"**：`forwarder_provider == "mihomo"` 时，`mihomo_external_controller.strip()` 不得为空 |
| `mihomo_projection.py` 的 `_validate_controller_address()` | **格式的唯一权威**（IP 字面量 / 端口范围 / loopback / unspecified / IPv6 方括号），**已经存在，不要改** |

**禁止**：`core/config.py` 不得 import `providers/forwarder/mihomo_projection.py`
（反向依赖），**也不得**把格式规则在 `config.py` 里抄一遍（双事实来源）。

##### 因此 B2-B2 的允许文件比原清单多两个

**新增 `backend/app/core/config.py` 与 `backend/tests/unit/test_registry.py`。**
这两个文件原本只在 S04-C 清单里，现在两份清单**都有**——按下面的边界切开，
不是从 S04-C 拿走：

| | B2-B2 做 | S04-C 做 |
|---|---|---|
| `core/config.py` | 加 `mihomo_external_controller` 字段 + `forwarder_provider == "mihomo"` 闸门下的**非空**校验 | `build_registry()` 放行、lifecycle/factory、S04-C 自己新引入字段的校验 |
| `test_registry.py` | 上面那条校验的单测（空值 fail-closed / 非空通过） | 放行后的注册表行为 |
| `.env.example` | **不碰** | 补 `MIHOMO_EXTERNAL_CONTROLLER=`（空值），见 S04-C 段 |

**为什么字段和它的校验必须同一个 PR**：ADR-025 §3 的措辞是 **validated**
`Settings`，而这个值**逐字进 manifest 并被 fingerprint 覆盖**；拆成
"B2-B2 加字段、S04-C 加校验"会留出一段时间，让一个未经校验的部署常量参与
fingerprint。理由见 ADR-035 §3.2。

**B2-B2 期间这条校验不会真的触发**（`build_registry()` 还不接受 `mihomo`），
**这是允许的**——与 B2-B1"resolver 已交付但尚无生产调用点"同一性质：
正确性由本 checkpoint 的单测完整覆盖。

**已实测，不会误伤**：`backend/tests/unit/test_mihomo_generation.py:25` 的
`build_registry(Settings(forwarder_provider="mihomo"))` 不受影响——
`validate_runtime_safety()` 只在 `Settings.from_env()` 里调用，直接构造
`Settings(...)` 不走它。

##### 这条红线没有放宽

B2-B 结束时必须仍有测试证明 `FORWARDER_PROVIDER=mihomo` **被
`build_registry()` 拒绝**。ADR-035 加的是一个地址字段，**不是一个开关**。

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

**前置条件**：无。**已于 2026-09-19 由 PR #156 完成并合并。**

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

**允许修改的文件**：
```
backend/app/infra/mihomo_materialization.py
backend/app/infra/mihomo_generation.py
backend/app/infra/credential_resolver.py
backend/app/models/ops.py
backend/app/models/__init__.py
backend/app/providers/transport/subscription.py
backend/app/providers/transport/resolver.py              # 2026-09-19 补入，依据见下
backend/app/providers/forwarder/mihomo_projection.py     # 仅 DNS schema gate
backend/app/workers/transport_sync.py
infrastructure/alembic/versions/0025_mihomo_projection_generations.py
infrastructure/alembic/versions/0026_mihomo_transport_materializations.py
backend/tests/unit/test_mihomo_materialization.py
backend/tests/unit/test_mihomo_generation.py
backend/tests/unit/test_transport_provider.py
backend/tests/unit/test_transport_resolver.py            # 2026-09-19 补入，依据见下
backend/tests/unit/test_mihomo_projection.py
backend/tests/guards/test_mihomo_reconciliation_safety.py
backend/tests/guards/test_secret_leak.py
backend/tests/integration/test_models.py
```

##### 2026-09-19 闭包补入：`resolver.py` 与它的单测（source revision 的生产边界）

**触发**：PR #156（head `9c2067383f1d5f832f2a663d04c0dbb0b740ebb4`）实现的
receipt 会**谎报 bytes 的来源 revision**。实测当前调用链：

| 步 | 代码 | 发生了什么 |
|---|---|---|
| 3 | `resolver.py:93` | 用 purpose-bound `SecretSnapshot` 解析订阅 URL，**revision = N** |
| 4 | `subscription.py:270` | 用那个 URL 发起网络 fetch |
| 5 | `subscription.py:284` | cache 写盘成功 |
| 6 | `transport_sync.py:103` | **fetch 之后重新查当前 `Secret`** |
| 7 | `transport_sync.py:122` | `receipt.source_revision = secret.revision` ← **可能已是 N+1** |

**只要 secret 在第 3 步与第 6 步之间轮换，receipt 就会给"用 revision N 取回的
bytes"盖上 revision N+1 的章。** 这违反 ADR-025 §3a：`source_revision` 必须是
**实际产生这批 materialized bytes 的那个 revision**，不能在网络 I/O 之后重新
查询当前值猜出来。

**这不是新架构问题，正确边界由 ADR-025 §3a 加现有 resolver 唯一推出**——
理由是 resolver **已经**持有并校验着那个值：

```python
# resolver.py:99  —— 新建时把 revision 存进自己的表
self._providers[descriptor.record_id] = (descriptor, snapshot.revision, provider)

# resolver.py:82-84 —— 复用时校验它没漂移
if snapshot.revision != old_revision:
    raise TransportResolutionError("TRANSPORT_SECRET_REVISION_DRIFT")
```

**resolver 已经是"这个 provider 绑定在哪个 revision 上"的权威**，
缺的只是**把这个已知值传下去**——第 95-98 行构造
`SubscriptionTransportProvider(code, snapshot.value, cache_path=...)` 时
**没有把 `snapshot.revision` 一起给它**，所以
`materialization_proof()`（`subscription.py:289`）只能返回二元组
`(cache_identity, content_hash)`，worker 只好回头自己去查。

**因此补入两个文件**：

| 文件 | 依据 |
|---|---|
| `backend/app/providers/transport/resolver.py` | **只有它知道**实际用于构造/复用 provider 的 purpose-bound `SecretSnapshot.revision`。不把它传下去，worker 就只能在 fetch 后重查当前值——那正是本缺陷 |
| `backend/tests/unit/test_transport_resolver.py` | 该文件是 resolver 的既有单测所在，绑定行为必须在这里被断言 |

##### source-revision 生产契约（**写死，不要重新设计**）

1. **`SubscriptionTransportResolver.resolve()` 是 transport source revision
   的唯一生产边界。** 别处不得产生或改写它。
2. **新建 provider 时**，把用于取 URL 的那个 `SecretSnapshot.revision`
   **一并绑定给 provider**。
3. **复用 provider 时**，继续使用 resolver 已校验过的**同一个 bound
   revision**（复用路径已有 `TRANSPORT_SECRET_REVISION_DRIFT` 校验，
   **不要改它的语义**）。
4. **`SubscriptionTransportProvider.materialization_proof()` 必须同时携带**：
   `cache identity`、`content hash`、**bound source revision**。
5. **`refresh_provider_inventory()` 直接消费 proof 里的 bound revision。**
6. **worker 不得在 fetch 之后重新查询 `Secret.revision` 来生成 receipt 的
   source revision。** 现有 `transport_sync.py:103` 那次查询必须去掉。
7. **secret 在 resolve 之后轮换时**：
   - 本次 receipt **仍诚实记录这次 fetch 实际用的那个旧 revision**；
   - 后续 loader / current-authority 比对会发现 revision stale 并 **fail closed**；
   - 下一次 `resolve()` 会按**现有** revision-drift 规则拒绝旧 provider，
     直到受控 replacement / restart。
   **这三条合起来才是正确行为：诚实记录 + 下游 fail closed，
   而不是在 receipt 里把它"修正"成当前值。**
8. **不得让 secret 的 DB transaction / row lock 穿过网络 I/O。**

##### proof 发布时序（同一边界，现状有缺陷）

实测 `subscription.py:282-287`：

```python
self._last_content_hash = hashlib.sha256(response.content).hexdigest()   # 写盘前
self._last_cache_identity = str(self._cache_path)                        # 写盘前
write_provider_cache(self._cache_path, response.content)                 # 可能抛错
# Publish the new snapshot only after parsing and cache persistence succeed.
```

**`write_provider_cache()` 抛错时，两个 proof 字段已经被新值覆盖**——
上一份 last-known-good proof 就此丢失。**而紧跟其后的注释恰恰说的是相反的事**
（"只在解析与落盘都成功之后才发布"），它当前只对 `_endpoints` / `_capacity`
成立，**对 proof 不成立**。

**要求**：

- **hash 可以在写盘前计算**（必须对**传给 `write_provider_cache()` 的同一份
  bytes** 计算，这一条不变）；
- **但对外可消费的 proof 只能在 `write_provider_cache()` 成功返回之后发布**；
- **cache write 抛错时**：不得发布新 proof，**已有的 last-known-good proof
  不得被这次失败的结果覆盖**。

##### `test_transport_resolver.py` 必须断言的性质

（测试名自定，但下面四条性质缺一不可）

1. provider / materialization proof 拿到的，**正是 resolver 实际读取的那个
   `SecretSnapshot.revision`**；
2. **同一 provider 被复用时 revision 不漂移**；
3. secret revision 变化后，**现有 drift 规则仍然 fail closed**；
4. **source revision 不是由 worker 在 fetch 之后查 `Secret.revision` 得来的**
   —— 断言那条查询不再决定 receipt 的 `source_revision`。

另需一条**写失败**测试（放 `test_transport_provider.py`）：
`write_provider_cache()` 抛错时不发布新 proof，且上一份 proof 未被覆盖。

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

##### 其余 B2-B1 修订要求（2026-09-19 审查确认，**不新增允许文件**）

**这些都落在已列出的文件里，不扩大 checkpoint。**

**1. receipt 必须是 current read** —— `backend/app/infra/mihomo_materialization.py`

receipt 查询必须带 `.with_for_update()` 与
`.execution_options(populate_existing=True)`，否则 Session 的 identity map
会把旧值当成当前值返回。测试必须覆盖这个具体场景：

- Session A 已经缓存了一份旧 receipt；
- Session B 更新并提交了 receipt；
- **Session A 的 verifier 再读时必须看到新值**，不是 identity-map 里的旧值。

**2. 两个迁移的时间列精度** —— `0025` / `0026`

`created_at`、`updated_at`、`freshness_deadline` 必须与 ORM 的
repository-standard precise timestamp 一致（MySQL 下即 `DATETIME(fsp=6)`
的等价写法）。**不改迁移编号、不改任何历史 revision**，只修这两个尚未合并的
新迁移本身。

**3. 验收测试要断到"性质"，不是只对上名字**

上一轮出现过"测试名存在、但没测到它该测的性质"。**逐条写死：**

| 测试 | 必须真正断言的 |
|---|---|
| `test_manifest_datetime_and_order_are_canonical` | 必须**真的构造 semantic datetime**；证明**等价时区 → 同一个 UTC canonical 表示**；证明 **mapping key 的插入顺序不影响 fingerprint** |
| `test_sql_controller_secret_resolver_requires_exact_purpose_and_revision` | 必须**捕获传给 helper 的 purpose**，断言它**恰好等于** `MIHOMO_CONTROLLER_API_SECRET`；断言返回的 revision 与 helper snapshot 的 revision **完全一致** |
| receipt 的 mismatch / missing / expired / source-revision 四类测试 | **每一条都要比较 verifier 调用前后的 receipt**：行数与字段内容**都不得被 verifier 改写** |
| `test_mihomo_projection.py` | 必须包含 TASK 已要求的**负向 DNS 用例**：`{"enable": "true"}` 必须 fail closed |
| `test_secret_leak.py` | 必须**实际放入** materialized YAML sentinel **与** controller-secret sentinel；覆盖 receipt / generation / verified materialization 的 `repr()`；覆盖相关**错误文本与日志**；断言 sentinel、URL、token **一个都不出现** |

**明确不做**：不写 concrete DB loader；不写 preparation/recovery；不改
`mihomo_reconciliation.py`；不改 `mihomo.py`；不改 registry；不做 S04-C；
**不碰 `scheduler` / `main.py` / `core/config.py`**——本次补入的只有
`resolver.py` 与它的单测，**不得借机扩大到调用它的上游**。

---

#### B2-B2 —— concrete DB desired loader + preparation transaction

**前置条件：B2-B1 已合并（PR #156，已满足）。** deployment constants 的落点
由 **ADR-035** 裁定，结论已写死在上面「deployment-owned 常量的落点」一节。
**没有别的等待项。**

**目标**：`SqlMihomoDesiredSnapshotLoader` 从 DB 全量重建 `DesiredForwarderState`
（铁律 1），`prepare_mihomo_reconciliation()` 在锁内把 generation 与
identifier-only `MIHOMO_RECONCILE` intent 在**同一事务**提交。

**必须完成的 production wiring**：loader 与 resolver **真正接到**
`reconcile_mihomo_job()` 的参数上（替换注入式 fake）；preparation 成为
generation + intent 的**唯一**生产者。

**允许修改的文件**：`backend/app/infra/mihomo_reconciliation.py`、
`backend/app/infra/credential_resolver.py`、
`backend/tests/unit/test_mihomo_reconciliation.py`、
**`backend/app/core/config.py`**、**`backend/tests/unit/test_registry.py`**
（前三个原本就在总清单内；后两个是 ADR-035 的后果，已补进总清单，
**只做上面那张 B2-B2/S04-C 边界表里 B2-B2 那一列**）。

**必须新增的测试**：`test_prepare_generation_and_intent_commit_together`、
`test_sql_controller_secret_resolver_requires_exact_purpose_and_revision`、
loader 全量 DB 重建与稳定排序的覆盖、
`mihomo_external_controller` 为空时 `forwarder_provider="mihomo"`
的 `validate_runtime_safety()` fail-closed 覆盖（放 `test_registry.py`，
与那里已有的 `test_xray_runtime_safety_requirements_fail_closed` 同形）。

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
- **B2-B2 起新开 PR。** 当时的理由是"它被 ADR-035 阻塞，不能让 #156 无限期
  挂着"；**#156 已于 2026-09-19 合并，ADR-035 已于 2026-09-20 裁定，
  两个理由都已消失**，但"一个 checkpoint 一个 PR"这条仍然照办。

---

### S04-C — 注册表放行与生产选择（NOT STARTED）

`build_registry()` 接受 `FORWARDER_PROVIDER=mihomo`，
`.env.example` 补齐变量，lifecycle/factory 边界。**生产激活本身仍需 User
明确批准，不由本任务授权。**

> **2026-09-20 修订（ADR-035）：`mihomo_external_controller` 的 fail-closed
> 校验已划给 B2-B2，不再是 S04-C 的活。** 原文写的"`validate_runtime_safety()`
> 加入与 Marzban 同级的 fail-closed 校验"指的就是它。S04-C 在这个函数里仍
> 负责 **S04-C 自己新引入的字段**（如果有）的校验，**不重复实现 controller
> 地址那一条**。
>
> **`.env.example` 具体要补的那一行，写死在这里，免得以后重新推导**：
>
> ```
> # Mihomo external controller, host:port. Empty until Mihomo is selected.
> MIHOMO_EXTERNAL_CONTROLLER=
> ```
>
> 为什么它留在 S04-C 而字段的校验去了 B2-B2：`.env.example` 是纯文档，
> **不参与 fingerprint、不影响 fail-closed 行为**；而校验参与（ADR-035 §3.2）。

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
backend/app/core/config.py                          # 2026-09-20 补入（ADR-035）：仅 mihomo_external_controller 字段 + 非空校验
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
backend/tests/unit/test_registry.py                     # 2026-09-20 补入（ADR-035）：仅上面那条非空校验的单测
docs/80-decisions/ADR-025-mihomo-projection-generation-authority.md      # 仅状态改为 Accepted
docs/82-tasks/TASK-S04-mihomo-activation.md
docs/83-project-continuity.md
```

> **两份清单有两处刻意重叠（2026-09-20，ADR-035）。**
> `backend/app/core/config.py` 与 `backend/tests/unit/test_registry.py`
> **同时**出现在 S04-B2-B 和 S04-C 清单里。**这不是漏改，也不是把它们从
> S04-C 拿走**——B2-B2 只做 `mihomo_external_controller` 字段与它的非空校验
> （加上单测），S04-C 仍然独占 `build_registry()` 放行、lifecycle/factory、
> `.env.example`。精确切分见上面 B2-B2 那张边界表。
>
> **重叠意味着这两个文件上可能出现先后两次改动**，这是允许的；
> 不允许的是某一边越过边界表去做对方那一列。

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
- **deployment 常量 fail-closed（2026-09-20 补入，ADR-035）**：一条测试断言
  `forwarder_provider="mihomo"` 且 `mihomo_external_controller` 为空白时，
  `validate_runtime_safety()` 抛 `ValueError`；一条断言非空时通过。
  另断言 `api-secret-ref` 取自 `MIHOMO_CONTROLLER_SECRET_REF` 常量，
  **不来自** `Settings`。

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
