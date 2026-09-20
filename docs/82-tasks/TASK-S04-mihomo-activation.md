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

### S04-B2-B 的执行切分（**当前顺序见下方粗体；2026-09-19 的「4 个 checkpoint」已是历史**）

> ## ⚠️ 当前唯一顺序（2026-09-20，ADR-037 扩展后）
>
> ```
> B2-B1 → B2-B2a → B2-B2 → B2-B2c → B2-B2d → B2-B3 → B2-B4
> ```
>
> **下面那段「4 个 checkpoint」是 2026-09-19 的历史记录**，它的拆分理由仍然有效，
> 但**数量与顺序已被 ADR-037 取代**。精确边界见「执行切分与唯一安全依赖顺序」节。

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
> 不把任何 B2-B 要求推迟到 S04-C。** ~~下面四个 checkpoint 的并集~~
> **（2026-09-20：ADR-037 把 4 个扩为 6 个，并新增 writer + S04-C 闸门。）**
> **全部 checkpoint 的并集等于**原 B2-B 的交付项 1–8 与验收标准全集。

#### 依赖关系（单向，不得回环）

```
B2-B1  receipt / secret resolver / manifest / allocator / DNS gate
   │        ← 不依赖任何其它 checkpoint
   ▼
B2-B2a contract substrate（**前置：ADR-037 已合并**）
   │        ← DTO + manifest 键 + MANIFEST_VERSION "1"→"2"；不生产 generation
   ▼
B2-B2  concrete DB desired loader + preparation transaction
   │        ← PR #163，**需 rebase**；loader 必须**填充** egress_proxies
   │        ← 第一个能生产 generation 的 checkpoint，manifest 此时已完整
   ▼
B2-B2c render / finalization（dialer-proxy + 明文在 render 边界）
   ▼
B2-B2d Xray → Mihomo 交接（ADR-037 §1a）
   │        ← 没有这一段，链路 B 不可达
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
| **concrete loader 读哪些 DB 权威行** | **见下面「三类 DB authority 的精确分类」** —— 本行原来只给了一个平铺的表名清单，**那是不够的，也误导了两轮实现**（2026-09-20 裁决已更正） | ADR-025 §4 +  ADR-023 §1.1 |
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

#### 三类 DB authority 的精确分类（**2026-09-20 架构裁决，B2-B2 照此实现**）

> **为什么要有这一节。** 上面那一行原文是一个平铺的表名清单
> （「……`EgressBinding`；……`EgressTransportAssignment`……」），它**只说了读哪些表，
> 没说每张表的哪些列真正进入 projection**。ADR-025 §4 的判据是
> **“every effective input that can affect the repo-owned Mihomo projection”**——
> 判据是「**是否影响 Mihomo 文档**」，不是「是否被 SELECT 过」。
>
> 连续两轮实现都卡在这里：第一轮查了表但把结果 `del` 掉，第二轮为了「消费掉」
> 而发明了 `subscription-{subscription_id}` proxy-group。**两次都是规格的错，不是
> 执行者的错**——清单给了表名却没给语义，任何人都只能猜。
>
> **我写那一行时的具体失误**：把 ADR-025 §4 的 manifest bullet 逐条映射到「仓库里
> 名字最像的模型」，**没有沿「这张表的变化会不会改变 Mihomo 文档」这条轴走一遍**。
> 这与 S07 两次漏项、以及我在 #157/#162 上各犯一次的，是同一个失误类型。

##### 先确定链路：**ADR-037 §1 裁定为链路 B**

```
客户端
  → Xray（VPS，per-user 路由）
  → Mihomo listener（127.0.0.1:{mihomo_listen_port}，每出口一个）
  → transport node（订阅节点，ACTIVE assignment 选定）
  → 住宅 ISP egress（socks5 + 凭据）
  → 目标网站
```

| 段 | 载体 | 依据 |
|---|---|---|
| 客户端接入 VPS | **Xray**，per-user 路由 | `provisioning_state.py:398-403` |
| Xray → Mihomo | Xray outbound → `127.0.0.1:{mihomo_listen_port}` | `models/egress.py:194`（`unique=True`，每出口一个） |
| Mihomo listener | 每住宅出口一个 `mixed` listener | `ARCHITECTURE.md` §8「listener 数 == 出口数」 |
| **Mihomo → transport node** | ACTIVE `EgressTransportAssignment` 选定的 materialized proxy | ADR-037 §2 |
| **transport → 住宅 egress** | 以 transport node 为 `dialer-proxy` 拨通住宅 socks5 | ADR-037 §2.4 |
| 最终出口 | 住宅 ISP IP | `ARCHITECTURE.md` §8「listener 出口 IP == 数据库记录 IP」 |

> **被否决的链路 A**（Mihomo 直连住宅、不经 transport）：会让 transport
> materialization 全套机制（receipt / freshness / cache identity）与
> `EgressTransportAssignment` **同时变成死配置**。论证见 ADR-037 §1。

> **一条必须记住的约束**：`mihomo_listen_port` **每出口唯一**，所以
> 「哪个客户走哪个住宅出口」由 **Xray 选 listener 端口**决定，**不由 Mihomo 决定**。
> **Mihomo 不做 per-subscription 路由** —— 这就是 `subscription-{id}` proxy-group
> 必须撤掉的根本原因。

##### 分类表（**逐行照做**）

| 表 / 列 | 分类 | 怎么做 |
|---|---|---|
| `RouteGroup`（ACTIVE） | **effective** | → proxy-group 的 `name` |
| `RouteEgressBinding`（enabled） | **effective** | → proxy-group 的 `proxies` 成员 |
| `TrafficRule`（enabled） | **effective** | → `rules`；末尾必须是 `MATCH,BLOCK`（铁律 2） |
| `EgressGroup`（ACTIVE） | **validation-only** | 只用于筛 `EgressEndpoint.group_id`；自身不进 manifest |
| `EgressEndpoint.code` / `.protocol` / `.host` / `.port` | **effective** | → `ForwarderEgressProxyDTO` 的同名字段 |
| `EgressEndpoint.mihomo_listen_port` | **effective** | → `listener_specs` 的 `port` |
| `EgressEndpoint.credential_secret_ref` | **effective** | precedence 的**回落**来源（ADR-037 §3.2） |
| `EgressEndpoint` 其余列（`location`/`isp`/`ip_type`/`notes`/`capacity`/`current_count`/`purpose`/时间戳） | **非输入** | 一律不读进 snapshot |
| `EgressBinding.egress_id` | **validation-only** | ACTIVE binding 的 `egress_id` 必须落在 in-scope 出口集合内，否则 fail closed |
| `EgressBinding.subscription_id` | **非输入** | **禁止**进入 Mihomo 文档（见「必须撤掉的东西」） |
| `EgressBinding.credential_secret_ref` | **effective** | precedence 的**首选**来源（ADR-037 §3.2） |
| `Secret`（egress credential，仅 `secret_ref` + `revision`） | **effective** | → `credential_secret_ref` / `credential_revision`。**loader 不解密** |
| `TransportProviderRecord`（enabled，被 RouteBinding 引用） | **effective** | → receipt 查找 + `transport_references` |
| `TransportEndpointRecord.name` / `.host` / `.port` | **effective** | → `transport_node_identity` 三元组；并用于定位 materialized proxy |
| `TransportEndpointRecord` 其余列（`region`/`latency_ms`/`status`/`raw_metadata_json`/时间戳） | **非输入** | 变化不得影响 fingerprint |
| `EgressTransportAssignment`，`state == ACTIVE` | **effective** | → `transport_proxy_name` + `transport_node_identity`（ADR-037 §2） |
| `EgressTransportAssignment`，`state in {DRAINING, RELEASED}` | **非输入 / 历史状态** | 不阻断、不进 snapshot/manifest、不参与校验 |
| `MihomoTransportMaterialization`（receipt） | **effective** | 六字段绑定元组 → `transport_materializations` + `transport_references` |
| `Secret`（controller，仅 `secret_ref` + `revision`） | **effective** | → `deployment_constants`。**loader 不解密** |

##### Q1 裁决 —— ACTIVE `EgressTransportAssignment.transport_node_id` A→B：**manifest 必须变（YES）**

> **⚠️ 本节在 2026-09-20 被推翻过一次，留下完整记录。**
>
> **旧结论（已作废）**：NO ——「这张表没有 writer/reader/ADR，语义从未定义，
> 故不是 effective input」。
>
> **前半句是事实，结论是回避。**「语义从未定义」是**需要一条 ADR 去解决的问题**，
> 不是把它判成 unsupported 的理由。ADR-023 §1.1 的 unsupported 分类是给**确实
> 无法表达**的字段用的；这条链路**可以表达**，只是需要有人定下来。
>
> **最终结论：YES。裁定依据 `ADR-037` §2，由 User 于 2026-09-20 决定链路 B。**

**ACTIVE assignment 的语义**（ADR-037 §2.1）：

> 该 `egress_id` 的出站流量，**必须经由 `transport_node_id` 指向的 transport 节点
> 中继出去**。

| 列 | 职责 |
|---|---|
| `egress_id` | 被约束的住宅出口 |
| `transport_provider_id` | 所属订阅 provider，**必须与 node 的 `provider_id` 一致**，否则 fail closed |
| `transport_node_id` | 选定的中继节点 |

**每个 in-scope egress 至多一条 ACTIVE assignment。**
**没有 ACTIVE assignment 的 egress 允许存在**，其 proxy 不带 dialer（直连住宅）——
这是合法过渡态，**不是错误**。

**assignment → materialized proxy 的定位规则（写死，不要重新设计）：**

```
transport_node_id
  → TransportEndpointRecord(provider_id, name, host, port)
  → 在该 provider 的 receipt-verified cache 内容里，
    按 (name, host, port) 三项全等 定位唯一一个 materialized proxy
```

三项全等而非只看 name：`transport_sync.py:42-75` 把这三列直接写自
`provider.list_endpoints()`，与 cache 同源；要求三项一致是**为了不一致时立刻发现**，
而不是静默接上一个错节点。

**fail-closed 清单：**

| 情况 | 错误码 |
|---|---|
| node 行不存在 | `MIHOMO_TRANSPORT_ASSIGNMENT_NODE_MISSING` |
| `transport_provider_id != endpoint.provider_id` | `MIHOMO_TRANSPORT_ASSIGNMENT_PROVIDER_MISMATCH` |
| `egress_id` 不在 in-scope 出口集合内 | `MIHOMO_TRANSPORT_ASSIGNMENT_EGRESS_UNKNOWN` |
| cache 内按三元组找不到唯一匹配（0 或 >1） | `MIHOMO_TRANSPORT_PROXY_UNRESOLVED` |
| 同一 egress 有 >1 条 ACTIVE | `MIHOMO_TRANSPORT_ASSIGNMENT_AMBIGUOUS` |

**任一不满足 ⇒ 整份候选失败**，不得产出「某个出口悄悄直连」的部分配置（铁律 1）。

##### Q2 裁决 —— ACTIVE `EgressBinding.credential_secret_ref` A→B：**manifest 必须变（YES）**

> **⚠️ 本节同样被推翻过一次。**
>
> **旧结论（已作废）**：NO ——「凭据当前流向 Xray，Mihomo 侧无人读它」。
> 链路逐跳可查：`provisioning_state.py:388-394` → `:398-403`（`XrayOutboundDTO`）
> → `xray_file.py:130-131` → `xray_composition.py:385-389`。
>
> **实测没错，用它回答这个问题错了。**「今天的代码把它送去哪」是**实现现状**；
> 「Mihomo projection 是否必须覆盖它」是**架构问题**。方向反了。
>
> **最终结论：YES。裁定依据 `ADR-037` §3。**

`ARCHITECTURE.md` §8 要求 Mihomo 亲自拨通住宅出口，而住宅出口是**需认证的
socks5**（`xray_composition.py:383` 的协议白名单）。当前 proxy 条目
`{name,type,server,port}` **没有凭据字段，永远过不了那行验收**。

**优先级（= ADR-019 §7，不新立规则）：**

```
active EgressBinding.credential_secret_ref  （非空时）
  否则
EgressEndpoint.credential_secret_ref
```

- 只考虑 `released_at IS NULL` 的 active binding；
- `NULL` / 空串 = 无 override，**允许**回落；
- **whitespace-only / 指向不存在 Secret / revision 非法的非空 override 是
  malformed，必须 fail closed，禁止静默回落**；
- endpoint-level ref 为空或 malformed 同样 fail closed。

**与 Xray 共用 precedence 是刻意的**：否则同一出口会出现「Xray 用 A、Mihomo 用 B」
的分裂。

**`Secret.revision` 属于 manifest identity**：exact ref = precedence 选出的那一个；
**purpose 不绑定**（复用 `SqlAlchemyCredentialResolver` 现有的 purpose-unbound 语义，
与 Xray 读同一行，**本次不改它**）；fresh-read 用 `_current_secret_statement()` 的
current/locking read，且与 snapshot 同一锁 span。

##### provider-neutral contract（**唯一结构，不得替换**）

`backend/app/providers/base.py` 新增：

```python
@dataclass(frozen=True, slots=True)
class ForwarderEgressProxyDTO:
    name: str
    protocol: str
    host: str
    port: int
    credential_secret_ref: str = field(repr=False)
    credential_revision: int = 0
    transport_proxy_name: str | None = None
    transport_node_identity: tuple[str, str, int] | None = None
```

`DesiredForwarderState` 新增 `egress_proxies: tuple[ForwarderEgressProxyDTO, ...] = ()`，
**按 `name` 升序稳定排序**（loader 保证；渲染与 manifest 不得再次重排）。

**与既有字段的四条关系（全部必须成立）：**

| 关系 | 规则 |
|---|---|
| `proxies` | **语义收窄**为 transport materialization 产物；repo-owned 出口**一律**走 `egress_proxies` |
| `listener_specs` | 每个 `.proxy` 必须能在 `egress_proxies` 里找到同名项，否则 `MIHOMO_LISTENER_PROXY_UNBOUND` |
| `transport_references` / `transport_materializations` | 不变；`transport_proxy_name` 必须落在某个 receipt 覆盖的 cache 内容里，否则 `MIHOMO_TRANSPORT_PROXY_UNRESOLVED` |
| 跨来源重名 | **已有机制，不新增**：`_validate_sections()` 的 `MIHOMO_DUPLICATE_PROXY_IDENTITY` 运行在合并后的 `effective_desired` 上（`mihomo_projection.py:435` + `:220`）。只需把 `egress_proxies` 并入同一条校验路径 |

##### manifest 表示

`build_projection_source_manifest()` 新增键 `"egress_proxies"`，按 `name` 升序，
每项八字段：`name / protocol / host / port / credential_secret_ref /
credential_revision / transport_proxy_name / transport_node_identity`。

**只有 opaque ref、整数 revision、节点名与三元组进 manifest。明文永不进。**

> **⚠️ 一个真实失效面**：**明文改了而 `Secret.revision` 没改 ⇒ fingerprint 不变
> ⇒ Mihomo 继续用旧凭据**。凭据轮换路径**必须**递增 `Secret.revision`。

##### Mihomo document 表达（**唯一编码：`dialer-proxy`**）

```yaml
proxies:
  - name: <transport proxy name>        # materialized，形状由订阅源决定
    ...
  - name:         <EgressEndpoint.code> # repo-owned
    type:         socks5
    server:       <EgressEndpoint.host>
    port:         <EgressEndpoint.port>
    username:     <resolved plaintext>
    password:     <resolved plaintext>
    dialer-proxy: <transport proxy name>   # 无 ACTIVE assignment 时整个键省略
listeners:
  - {name: listener-<code>, type: mixed, listen: 127.0.0.1,
     port: <mihomo_listen_port>, proxy: <code>}
```

`protocol` 为 `socks` 时同样渲染成 `socks5`；除 `{socks, socks5}` 外
`MIHOMO_EGRESS_PROTOCOL_UNSUPPORTED` fail closed。

**禁止**：proxy-group 技巧、`proxy-providers` 链、把 transport 节点塞进 listener 的
`proxy`、或任何以命名约定隐含链路的写法。

> **⛔ 实现期必须确认一次**：`dialer-proxy` 必须被
> `metacubex/mihomo:v1.19.27`（`compose.transport.yml` 钉住的版本）的 candidate
> validation 接受。**ADR-037 未在该镜像上实测过这一条。** 若发现不支持，
> **停止并报告 User**（ADR-036 §2），**不得自行改用其它编码**。

##### plaintext 边界

```
ref → 【loader：只取 ref + revision，不解密】→ egress_proxies → manifest / generation
    → 【render / finalization 边界】SqlAlchemyCredentialResolver（复用，不新建）
    → transient CredentialDTO → render() → CandidateConfig → 受权限保护的 runtime 文件
```

resolve 与 snapshot 必须在**同一个 Mihomo projection 命名锁 span 内**；
resolve 时 `Secret.revision` 与 snapshot 的 `credential_revision` 不一致 ⇒
**候选作废、fail closed、重新 fresh-read 重试**，绝不用新明文配旧 fingerprint。

**明文绝不允许出现**于：loader、`DesiredForwarderState`、任何 desired DB row、
manifest、generation row、transport receipt、日志/异常/metrics、
runtime-independent snapshot、PR 与文档正文。

##### `EgressEndpoint` 的 in-scope status（**原规格漏了，必须补**）

**当前 PR #163 用的是 `status == "AVAILABLE"`，这是错的，且在真实数据上必然失败。**

实测：`provisioning_state.py:93` 在把出口分配给客户时会把
`endpoint.status` 置为 **`"ASSIGNED"`**。所以只取 `AVAILABLE` 会把**每一个正在服务
客户的出口**排除在 Mihomo projection 之外——直接违反
`ARCHITECTURE.md` §8 的「Mihomo listener 数 == 数据库出口数」，
并且紧接着会让 `EgressBinding` 的关系校验对**每一条真实 ACTIVE binding**
抛 `MIHOMO_EGRESS_BINDING_INVALID`。

> **定死：Mihomo projection 的 in-scope 出口集合是
> `EgressEndpoint.status IN ("AVAILABLE", "ASSIGNED", "DEGRADED")`。**

依据是仓库里唯一的 forwarder listener 先例
`provisioning_state.py:187` 的 `current_forwarder_state()`，它用的正是这三个状态。
（`drift_check.py:18` 的 `("AVAILABLE","ASSIGNED")` 是 **drift 检查**的口径，不是
listener 口径；`DEGRADED` 的出口仍然挂着客户，**摘掉它的 listener 会静默断掉那个客户**，
所以取更保守的三状态集合。）

##### 必须撤掉的东西（PR #163 当前实现）

| 要撤掉的 | 为什么 |
|---|---|
| `subscription-{subscription_id}` proxy-group | ① 没有任何 TASK/ADR 授权这个表示形状；② 它把 **`subscription_id`（客户标识）写进 Mihomo 配置文件**，而 **Mihomo 不做 per-subscription 路由** —— 那是 Xray 按 `gateway_principal` 选 listener 端口做的（ADR-037 §1）；③ 这些 group **没有任何 rule 引用**，属死配置，违反 ADR-023 §1.1「未被 fresh desired snapshot 引用的旧 proxy/listener/rule 必须从候选中消失」 |
| `EgressEndpoint.status == "AVAILABLE"` 单状态过滤 | 见上一节，真实数据上必然失败 |
| 把 assignment 与 endpoint 查出来后 `del` 掉 / 仅做关系校验就放行 | 按 Q1 它们现在是 **effective input**，必须产出 `transport_proxy_name` + `transport_node_identity` |

##### 执行切分与**唯一安全依赖顺序**（2026-09-20 第三次修订）

> **⚠️ 上一版的顺序是错的，会造出一个已知违反 Accepted ADR 的中间 `main`。**
>
> 旧顺序是 `B2-B2（#163）→ B2-B2c`，且明写「B2-B2 不含 egress_proxies /
> credential」。但 **ADR-037 一旦合并，assignment 与 credential 就是
> effective canonical-manifest inputs**，而 **#163 的
> `prepare_mihomo_reconciliation()` 是 production-wired 的 generation 生产者**。
> 按旧顺序合 #163，`main` 上就会有一个**明知遗漏 effective inputs 的
> fingerprint 生产者**。
>
> **这不是纸面问题。** `allocate_generation()` 在
> `manifest_version` 与 `desired_fingerprint` 都相同时**复用**上一代
> （`mihomo_generation.py:104-109`）。所以在那个中间状态下：
> **assignment 或 credential 改了 → fingerprint 不变 → 复用旧 generation**，
> 而且这些 row 会**持久留在库里**。
> 违反 ADR-025 §4 与 `docs/85`「每个 checkpoint 结束时已接线部分必须是
> fail-closed 终态，不得留过渡性不安全设计」。

**不变式（**本 TASK 的硬约束，任何重排都必须满足**）：**

> **任何「已合并且能生产 generation」的 checkpoint，其 canonical manifest
> 必须已经覆盖 ADR-037 定义的全部 effective inputs。**

**唯一安全顺序：**

```
B2-B1  ✅ 已合并（#156）
   ▼
B2-B2a contract substrate（**新增，前置：ADR-037 已合并**）
   │      ForwarderEgressProxyDTO + DesiredForwarderState.egress_proxies
   │      + manifest 新增 egress_proxies 键 + MANIFEST_VERSION "1" → "2"
   │      ← 纯类型与序列化契约。不含 loader、不含 render、不碰 Xray
   │      ← 它**不**生产 generation，所以不触发上面的不变式
   ▼
B2-B2  concrete DB desired loader + preparation transaction   ← PR #163，**需 rebase**
   │      ⚠️ **scope 相应变化**：loader 必须**填充** egress_proxies
   │         （assignment 解析 + credential ref/revision），**只取 ref，不解密**
   │      ← 这是第一个能生产 generation 的 checkpoint，
   │         合并时 manifest 已经完整 —— 不变式成立
   ▼
B2-B2c render / finalization
   │      dialer-proxy 渲染 + 凭据明文在 render 边界解析 + 协议白名单
   ▼
B2-B2d Xray → Mihomo 交接（ADR-037 §1a）
   │      Xray outbound 改指 127.0.0.1:{mihomo_listen_port}，该跳无凭据
   │      ← 没有这一段，链路 B 不可达
   ▼
B2-B3  freshness 复核 / recovery / runtime exact readback
   ▼
B2-B4  完整 integration / guard / migration / concurrency 验收
   ▼
⛔ writer checkpoint（ACTIVE assignment 的生产者，**尚无 owner**）
   ▼
S04-C  registry 放行 + 激活闸门（每 egress 恰好一条 ACTIVE assignment）
```

**为什么选「substrate 先行」而不是「把 #163 的 scope 一次扩到闭合」**
（审查给了这两条，必须二选一且唯一化）：

后者要在一张卡里同时完成 DTO 设计、manifest 契约、loader 填充、render、
凭据边界 —— **正是连续两轮失败的那种范围膨胀**。
前者把**契约**（纯类型 + 序列化，无 I/O、无 DB、无 render）单独切出来，
剩下的每一段都只依赖已经存在的契约，**每一段都能独立跑通验收并提交**。

> **#163 的 allowed files 不需要新增任何文件。** 它要填的
> `egress_proxies` 用的是 B2-B2a 已经建好的类型，读的是它已经在读的那些表。
> **变的是验收标准，不是文件清单。**

##### B2-B2a —— contract substrate（**新增 checkpoint**）

**⛔ 前置：`ADR-037` 已合并。**

**目标**：只落地 provider-neutral 契约与 manifest 序列化，**不接任何生产路径**。

**精确 allowed files：**

```
backend/app/providers/base.py            # ForwarderEgressProxyDTO + DesiredForwarderState.egress_proxies
backend/app/infra/mihomo_generation.py   # manifest 新增 egress_proxies 键 + MANIFEST_VERSION → "2"
backend/tests/unit/test_mihomo_generation.py
backend/tests/guards/test_secret_leak.py
```

**必须照抄的契约**：ADR-037 §4（DTO 字段、**loader 排序职责**与四条关系）、
§5（manifest 键与序列化）。

> **排序职责只有一个归属**：`egress_proxies` 按 `name` 升序**由 loader 保证**，
> **manifest 与 render 都不得再次重排**（ADR-037 §4）。B2-B2a 只验证
> 「不重排」，排序本身由 B2-B2 的 loader 测试覆盖。

**`MANIFEST_VERSION` 必须由 `"1"` 升到 `"2"`（写死，不要跳过）：**

`allocate_generation()` 用 `manifest_version` + `fingerprint` 决定是否复用
（`mihomo_generation.py:104-109`）。manifest 形状变了而版本号不变，会让
**同一个 `"1"` 同时指代两种不同的 manifest 语义**，既有 row 会被按新契约解读。
升版之后，旧 row 明确属于「ADR-037 之前的形状」，**永远不会被新契约复用**。

**必须新增的测试：**

| 测试名 | 关键断言 |
|---|---|
| `test_manifest_includes_egress_proxies_without_reordering` | 传入**已按 `name` 升序**的 `egress_proxies` ⇒ manifest **原样保持输入顺序，不再次排序**；八个字段完整进入 manifest。⚠️ **2026-09-20 更正**：原名 `..._sorted_by_name`、原断言「乱序传入 ⇒ manifest 自己排序」，**与 ADR-037 §4「由 loader 保证排序，渲染与 manifest 都不得再次重排」直接冲突**。排序职责在 loader（见 B2-B2 的测试），不在 manifest |
| `test_manifest_version_is_bumped_for_egress_proxies_shape` | `MANIFEST_VERSION == "2"`；且用 v1 的 generation row 与 v2 manifest 比对时**不复用**，分配新 revision |
| `test_egress_proxy_dto_repr_redacts_credential_ref` | `repr(ForwarderEgressProxyDTO(...))` **不含** `credential_secret_ref` 的值 |
| `test_desired_forwarder_state_accepts_egress_proxies_and_freezes` | `egress_proxies` 通过 `__post_init__` 的 `freeze()`；`tuple[str,str,int]` 的 identity 三元组可被接受 |

**明确不做**：不写 loader；不渲染 `dialer-proxy`；不解析任何凭据；不碰 Xray；
不碰 `mihomo_projection.py`（那是 B2-B2c）。

##### 精确 allowed files

**B2-B2（PR #163，**文件清单不扩展**，仍是这五个 —— 但验收标准按上面的新顺序
变了：loader 必须**填充** `egress_proxies`）：**

```
backend/app/infra/mihomo_reconciliation.py
backend/app/infra/credential_resolver.py
backend/app/core/config.py
backend/tests/unit/test_mihomo_reconciliation.py
backend/tests/unit/test_registry.py
```

> **B2-B2 一个字都不许动 `providers/base.py`（属 B2-B2a）与
> `mihomo_projection.py`（属 B2-B2c）。** 它**使用** B2-B2a 已建好的
> `ForwarderEgressProxyDTO`，但不修改它的定义。

**B2-B2c —— render / finalization（⛔ 前置：**B2-B2a 与 B2-B2 都已合并**）：**

```
backend/app/providers/forwarder/mihomo_projection.py   # dialer-proxy 渲染 + 协议白名单 + 并入既有重名校验
backend/app/providers/forwarder/mihomo.py              # render/finalize 边界接 CredentialResolver
backend/tests/unit/test_mihomo_projection.py
backend/tests/guards/test_secret_leak.py
```

> **`providers/base.py` 与 `mihomo_generation.py` 已在 B2-B2a 落地，
> `mihomo_reconciliation.py` 的 loader 填充已在 B2-B2 落地** ——
> B2-B2c **不再包含这三者**，它只做渲染与明文边界。

**不需要新的 resolver 文件** —— 复用 `credential_resolver.py` 已有的
`SqlAlchemyCredentialResolver`（ADR-037 §6）。

##### B2-B2 必须新增的测试（**精确名称与关键断言**）

放 `backend/tests/unit/test_mihomo_reconciliation.py`：

| 测试名 | 关键断言 |
|---|---|
| `test_loader_rejects_assignment_with_missing_transport_endpoint` | ACTIVE assignment 的 `transport_node_id` 指向不存在的行 ⇒ 抛 `MIHOMO_TRANSPORT_ASSIGNMENT_NODE_MISSING`；**且不产出任何 snapshot** |
| `test_loader_rejects_assignment_provider_mismatch` | `assignment.transport_provider_id != endpoint.provider_id` ⇒ 抛 `MIHOMO_TRANSPORT_ASSIGNMENT_PROVIDER_MISMATCH` |
| `test_released_transport_assignment_does_not_block_projection` | `state=RELEASED` / `DRAINING` ⇒ 不抛错、不进 manifest、**不**对其做 provider/node 校验；同一 egress 的 snapshot 与「完全没有该行」时**逐字节相同** |
| `test_egress_binding_egress_id_mismatch_fails_closed` | ACTIVE binding 指向 in-scope 集合外的 egress ⇒ `MIHOMO_EGRESS_BINDING_INVALID` |
| `test_synthetic_subscription_proxy_group_is_not_used` | 渲染结果的 `proxy_groups` 里**不存在**任何 `name` 以 `subscription-` 开头的项；且 `proxy_groups` / `proxies` / `listener_specs` / manifest 的**任何键值**都不含 `subscription_id` 的字符串形式 |
| `test_assigned_and_degraded_endpoints_are_in_scope` | 三个出口分别 `AVAILABLE` / `ASSIGNED` / `DEGRADED` ⇒ 三者都出现在 `listener_specs`；`len(listener_specs) == 出口数` |
| `test_transport_endpoint_record_non_identity_columns_are_not_inputs` | 只改 `region` / `latency_ms` / `status` / `raw_metadata_json` ⇒ manifest fingerprint **不变**（`name`/`host`/`port` 是 identity，其余不是） |
| `test_prepare_rolls_back_generation_when_intent_enqueue_fails` | 在 `allocate_generation()` 已 flush 之后强制 `enqueue_mihomo_reconciliation()` 抛错 ⇒ **测试自身不得调用 `db.rollback()`**；断言 Session 已无活动 preparation 事务（或等价地：紧接着的 `db.commit()` 无法持久化那个 generation），并断言**没有**新的 `MihomoProjectionGeneration`、**没有**新的 `MIHOMO_RECONCILE` Job |
| `test_loader_populates_egress_proxies_with_opaque_credential_identity` | **（顺序修订后新增）** ① **数据源故意乱序**（种入的 egress 行顺序不等于 `name` 升序）⇒ loader 产出的 `DesiredForwarderState.egress_proxies` **必须按 `name` 升序** —— 排序职责在 loader（ADR-037 §4）；② 每项带正确的 `credential_secret_ref`（按 ADR-019 §7 precedence 选出）与正整数 `credential_revision`；③ **全过程 `reveal_secret` / `decrypt_secret` 不得被调用**（monkeypatch 成调用即失败） |
| `test_assignment_node_change_changes_manifest_fingerprint` | **（顺序修订后前移到 B2-B2）** 只把 ACTIVE assignment 的 `transport_node_id` 由 node-A 改为同 provider 的合法 node-B ⇒ `transport_proxy_name` 与 `transport_node_identity` 变，`manifest_fingerprint()` **不同**，`allocate_generation()` 返回**新 revision** |
| `test_binding_credential_ref_change_changes_manifest_fingerprint` | **（顺序修订后前移到 B2-B2）** 只把 ACTIVE `EgressBinding.credential_secret_ref` 由 secret-A 改为 secret-B ⇒ fingerprint **不同** + 新 revision。**另加一例**：同 ref 的 `Secret.revision` N→N+1 ⇒ 同样不同 + 新 revision |
| `test_active_binding_credential_override_is_preserved_as_opaque_identity` | **（顺序修订后前移到 B2-B2）** override 非空 ⇒ `credential_secret_ref` **等于 override 的 ref**；`NULL`/空串 ⇒ **等于 endpoint 的 ref**；非空但 malformed ⇒ **fail closed 且不回落** |

> **⚠️ 这四条是 2026-09-20 第三次修订（审查 Major 3）前移到 B2-B2 的。**
> 原来它们在 B2-B2c。**必须前移**，因为 B2-B2 是**第一个能生产 generation 的
> checkpoint** —— 它合并时 manifest 就必须已经覆盖 ADR-037 的全部 effective
> inputs，否则 `main` 上会出现一个明知遗漏输入的 fingerprint 生产者。

> 上一轮审查要求的 `test_sql_desired_loader_consumes_all_authoritative_bindings`
> **不要再写** —— 用上表取代。它的原始意图（证明三张表真被消费）现在由
> `test_loader_rejects_assignment_*` 与 B2-B2c 的 fingerprint 测试共同覆盖。

##### B2-B2c 必须新增的测试（**精确名称与关键断言**）

> **2026-09-20 第三次修订**：原表里的三条 fingerprint / credential-identity 测试
> **已前移到 B2-B2**（理由见那一节）。本表只剩 render / finalization 相关的。

| 测试名 | 放哪 | 关键断言 |
|---|---|---|
| `test_manifest_excludes_egress_plaintext_credentials` | `test_secret_leak.py` | 种一个明文可辨识的 Secret；断言 `manifest` 的递归序列化、`DesiredForwarderState.__repr__()`、`ForwarderEgressProxyDTO.__repr__()`、generation row 的 `repr()` **均不含** username/password 子串；且 `credential_secret_ref` 不出现在 `repr()` 里（`repr=False`） |
| `test_finalization_resolves_egress_credential_at_provider_boundary` | `test_mihomo_projection.py` | 明文**只**在 render/finalize 边界出现：loader 阶段 monkeypatch 解密为「调用即失败」仍能构造出完整 snapshot；render 阶段注入 resolver 后，候选 document 的 proxy 条目含 `username`/`password`；且 resolve 时 revision 与 snapshot 不一致 ⇒ 候选作废（**不**用新明文配旧 fingerprint） |
| `test_mihomo_document_contains_egress_transport_binding` | `test_mihomo_projection.py` | 有 ACTIVE assignment 的出口 ⇒ 其 proxy 条目含 `dialer-proxy == <transport proxy name>`；**无** ACTIVE assignment 的出口 ⇒ 该条目**完全没有** `dialer-proxy` 键（不是空值）；且 `listeners[*].proxy` 与 `proxies[*].name` 一一对应 |
| `test_egress_proxy_name_collision_fails_closed` | `test_mihomo_projection.py` | repo-owned 出口 `code` 与某个 materialized proxy 同名 ⇒ 复用既有 `MIHOMO_DUPLICATE_PROXY_IDENTITY` fail closed |
| `test_unsupported_egress_protocol_fails_closed` | `test_mihomo_projection.py` | `EgressEndpoint.protocol` 非 `{socks, socks5}` ⇒ `MIHOMO_EGRESS_PROTOCOL_UNSUPPORTED` |
| `test_transport_proxy_unresolved_fails_closed` | `test_mihomo_reconciliation.py` | ACTIVE assignment 的 `(name, host, port)` 在 receipt-verified cache 内容里匹配 0 个或 >1 个 ⇒ `MIHOMO_TRANSPORT_PROXY_UNRESOLVED` |

##### B2-B2d —— Xray → Mihomo 交接（**ADR-037 §1a，2026-09-20 新增**）

> **为什么必须有这个 checkpoint。** ADR-037 §1 选定链路 B，但
> `provisioning_state.py:398-403` 当前构造的是
> `XrayOutboundDTO(host=endpoint.host, port=endpoint.port, ...)` ——
> **Xray 直拨住宅 egress，永远不会去 `127.0.0.1:{mihomo_listen_port}`。**
> 没有这个 checkpoint，**即使 B2-B2c 与 S04-C 全做完，链路 B 仍然不可达。**

**⛔ 前置：`ADR-037` 已合并 + B2-B2c 已合并。**

**裁决（ADR-037 §1a.2，照抄）：**

> `FORWARDER_PROVIDER=mihomo` 生效时，每个 active route 的 Xray outbound
> 指向 `127.0.0.1:{该 route 对应 EgressEndpoint.mihomo_listen_port}`，
> `protocol="socks"`，`credential_secret_ref=None`。
> **住宅 host/port/凭据不再出现在 Xray 这一跳。**
> **非 Mihomo 模式保持现状不变。**

**DTO 变更（ADR-037 §1a.4）：**

```python
credential_secret_ref: str | None = field(repr=False, default=None)
```

| 条件 | 要求 |
|---|---|
| loopback host + 某个 `mihomo_listen_port` | `credential_secret_ref` **必须** `None` |
| 其它任何 outbound | **必须**非空；`None` ⇒ `XRAY_OUTBOUND_CREDENTIAL_REQUIRED` fail closed |

> **默认值 `= None` 是刻意的**：让十几个只关心 tag/host/port 的既有构造点
> **不必逐个改**。只有真正涉及 loopback 语义的地方才动。

**精确 allowed files（已做四轴闭包，ADR-037 §1a.5）：**

```
backend/app/providers/base.py                            # credential_secret_ref -> str | None
backend/app/infra/provisioning_state.py                  # Mihomo 模式下构造 loopback outbound
backend/app/providers/gateway/xray_composition.py        # None 时渲染不带 users 的 socks outbound
backend/app/providers/gateway/xray_file.py               # 跳过 None，不放宽非 None 的解析失败
backend/tests/unit/test_desired_routing_snapshot.py
backend/tests/unit/test_xray_composition.py
backend/tests/guards/test_xray_writer_guard.py
```

**查过但明确不加**（防范围蔓延）：

- `ops/gateway/render_xray_routes.py` —— 它**确实**构造 `XrayOutboundDTO`，但
  `= None` 默认值使它**无需改动**即可继续编译与运行。**若实现时发现它必须改，
  停下来上报，不要自行扩清单。**
- `backend/tests/unit/test_xray_render.py` / `test_xray_provider_standalone_parity.py` /
  `test_credential_infra.py` / `test_domain.py` / `test_services_wiring.py` /
  `backend/tests/guards/test_safe_reload.py` /
  `backend/tests/integration/test_provisioning_phase_boundary.py` ——
  都构造 `XrayOutboundDTO`，但都**显式传**了 `credential_secret_ref`，
  默认值变更不影响它们。**同样：真要改就停下来上报。**

> **一条硬约束**：`backend/tests/unit/test_gateway_reconciliation_contract.py:39`
> 断言源码里存在字面量 `"full_desired_routing_snapshot(db)"` ——
> **不要重命名这个函数。**

**必须新增的测试与关键断言：**

| 测试名 | 放哪 | 关键断言 |
|---|---|---|
| `test_mihomo_mode_xray_outbound_targets_loopback_listener` | `test_desired_routing_snapshot.py` | Mihomo 模式下 outbound 的 `host == "127.0.0.1"` **且** `port == 该 route 对应 EgressEndpoint.mihomo_listen_port`；**断言它不等于** `endpoint.host` / `endpoint.port` |
| `test_mihomo_mode_xray_outbound_carries_no_residential_credential` | `test_desired_routing_snapshot.py` | 该 outbound 的 `credential_secret_ref is None`；且整个解析路径上 `resolver.resolve` **未被调用**（monkeypatch 成调用即失败） |
| `test_non_mihomo_mode_xray_outbound_keeps_direct_residential_dial` | `test_desired_routing_snapshot.py` | 非 Mihomo 模式下 `host/port == endpoint.host/port` 且 `credential_secret_ref` 非空 —— **两种模式的边界必须被证明，不能只测新路径** |
| `test_non_loopback_outbound_without_credential_fails_closed` | `test_xray_composition.py` | 非 loopback outbound 且 `credential_secret_ref is None` ⇒ `XRAY_OUTBOUND_CREDENTIAL_REQUIRED` |
| `test_loopback_outbound_renders_without_users_block` | `test_xray_composition.py` | 渲染出的 socks outbound **没有** `settings.servers[].users` 键（不是空列表）；非 loopback outbound 的 `users` 行为**逐字节不变** |

**明确不做**：不放行 registry（`FORWARDER_PROVIDER=mihomo` 结束时**仍被拒绝**）；
不做 S04-C 的激活闸门；不部署。

##### S04-C 的激活闸门（**ADR-037 §2.1a，新增，fail closed**）

> **在 `build_registry()` 放行 `FORWARDER_PROVIDER=mihomo` 之前，必须机械验证：
> 每个 in-scope egress（`status IN (AVAILABLE, ASSIGNED, DEGRADED)`）
> 有且仅有一条 `state == ACTIVE` 的 `EgressTransportAssignment`。**
>
> 缺失或多于一条 ⇒ **拒绝激活**。

**为什么必须有**：ADR-037 §2.1 允许「未激活阶段某些 egress 没有 assignment，
proxy 不带 `dialer-proxy`」。**这个过渡态如果一路带进生产，激活后就会有出口
绕过 transport 直连住宅 —— 实际退回被否决的链路 A，而且没有任何机制会报错。**

**必须新增的测试：**

| 测试名 | 关键断言 |
|---|---|
| `test_mihomo_activation_rejects_missing_active_transport_assignment` | 某个 in-scope egress 没有 ACTIVE assignment ⇒ 激活被拒（抛错），**且不构造任何 provider** |
| `test_mihomo_activation_rejects_multiple_active_transport_assignments` | 同一 egress 有 2 条 ACTIVE ⇒ 激活被拒 |
| `test_mihomo_activation_accepts_exactly_one_assignment_per_egress` | 每个 in-scope egress 恰好 1 条 ACTIVE ⇒ 闸门通过（**这只是闸门通过，不等于本 TASK 授权生产激活**） |

##### ⛔ ACTIVE assignment 的 writer —— **尚无 owner，已命名的未决项**

**当前仓库里没有任何代码写 `egress_transport_assignments`**（实测：除 model 与
迁移外零引用）。ADR-037 §2.1b **刻意不定义 writer**，因为「哪个 egress 配哪个
transport node」涉及容量、健康度、地域、重平衡——**那是一套分配策略，不能在
这里顺手发明**。

**但这不是「以后再看」**：上面的激活闸门把它变成了硬约束 ——

> **在 writer 存在并真正产出 ACTIVE assignment 之前，Mihomo 生产激活被机械阻断。**
> 不存在「先激活、assignment 以后补」的路径。

**依赖**：`writer checkpoint`（规格待定） → `S04-C 激活`。
**禁止**：人工改库、隐藏 seed、或「表以后自然会有数据」。

**按 ADR-036 §2**：真要做这个 writer 时，**停止自行推进、报告 User**，
由 User 决定是否叫 Claude 写那条规格。


##### preparation rollback（**纯实现问题，保留，B2-B2 必修**）

> `prepare_mihomo_reconciliation()` 是 preparation transaction 的 owner，
> **清理责任不得推给 caller。**

- 把锁内的 `load_current → manifest → allocate/reuse → enqueue → commit` 整段包起来：
  **任何在「确认成功 commit」之前抛出的异常，必须先在 Mihomo 命名锁内执行
  `db.rollback()`，然后 re-raise**；
- 除既有契约已要求的翻译外，**不得吞掉或改写原异常**；
- 保留成功路径的断言：generation 与 identifier-only intent **同一次 commit** 持久化；
- 对应测试见上表的 `test_prepare_rolls_back_generation_when_intent_enqueue_fails`
  —— **测试自身不得手工 `db.rollback()` 来伪造通过**。

##### 已记录的 S04-C 前置缺口

1. ~~Mihomo 拨住宅出口需要 socks5 凭据~~ **✅ 已由 `ADR-037` §3 裁定。
   实施分三段，不是「全在 B2-B2c」**：
   **`B2-B2a`** 建契约（`ForwarderEgressProxyDTO.credential_secret_ref` /
   `credential_revision` + manifest 键）→
   **`B2-B2`** loader 填充这两个字段并锁住 fingerprint 不变式（**不解密**）→
   **`B2-B2c`** render/finalization 解析明文并写进候选 document。
2. ~~`EgressTransportAssignment` 的 projection 语义从未定义~~
   **✅ 已由 `ADR-037` §2 裁定。同样分三段**：
   **`B2-B2a`** 建 `transport_proxy_name` / `transport_node_identity` 字段与
   manifest 表示 → **`B2-B2`** loader 解析 ACTIVE assignment、
   定位 materialized proxy、锁住 node A→B ⇒ fingerprint 变 →
   **`B2-B2c`** 渲染 `dialer-proxy`。
3. **`dialer-proxy` 在钉住的 `metacubex/mihomo:v1.19.27` 上未实测。**
   B2-B2c 的 candidate validation 必须确认它被接受；**不被接受则停止并报告 User**
   （ADR-036 §2），**不得自行改用其它编码**。

4. **Xray → Mihomo 交接未实现。** 已由 `ADR-037` §1a 裁定，实施在 **B2-B2d**。
5. **每个 in-scope egress 恰好一条 ACTIVE assignment 的激活闸门。**
   已由 `ADR-037` §2.1a 裁定，实施在 **S04-C**（见上面「S04-C 的激活闸门」节）。
6. **⛔ ACTIVE assignment 的 writer 仍无 owner。** 见上面那一节 ——
   **它是 S04-C 激活的硬前置**，不是可选项。

**除第 6 条外，当前没有其它已知的 S04-C 架构前置缺口。**

---

#### B2-B2 —— concrete DB desired loader + preparation transaction

**⛔ 前置条件：B2-B1 已合并（PR #156，已满足）**，**且 `B2-B2a` 已合并**
（2026-09-20 新增，ADR-037 §5a 的顺序不变式要求）。

> **#163 必须 rebase 到 B2-B2a 之后**，并且它的 loader **必须填充 `egress_proxies`** ——
> 因为 B2-B2 是第一个能生产 generation 的 checkpoint，合并时 manifest 就必须
> 已覆盖 ADR-037 的全部 effective inputs。

deployment constants 的落点由 **ADR-035** 裁定，结论已写死在上面
「deployment-owned 常量的落点」一节。

**目标**：`SqlMihomoDesiredSnapshotLoader` 从 DB 全量重建 `DesiredForwarderState`
（铁律 1），`prepare_mihomo_reconciliation()` 在锁内把 generation 与
identifier-only `MIHOMO_RECONCILE` intent 在**同一事务**提交。

**必须完成的 production wiring**：loader 与 resolver **真正接到**
`reconcile_mihomo_job()` 的参数上（替换注入式 fake）；preparation 成为
generation + intent 的**唯一**生产者。

> **⛔ 开工前先读上面「三类 DB authority 的精确分类」整节（2026-09-20 裁决，
> 依据 `ADR-037`）。** 它裁定了最终链路、逐列分类、Q1/Q2 的 YES、
> provider-neutral contract、`EgressEndpoint` 的 in-scope status，
> 以及 **B2-B2 与 B2-B2c 的精确切分与各自的 allowed files**。
> **不要从表名重新推导语义**——连续两轮实现正是栽在这里。
>
> **B2-B2 一个字都不许动 `providers/base.py` 与 `mihomo_projection.py`。**

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
与那里已有的 `test_xray_runtime_safety_requirements_fail_closed` 同形），
**外加上面裁决节「B2-B2 必须新增的测试」表里的 7 条**。

**明确不做**：不做 freshness 二次/三次复核；不做 recovery；不做 runtime readback；
不改 `mihomo_projection_lock.py` / `mihomo_blocker.py` 的公开签名。

---

#### B2-B3 —— freshness 三点复核 / recovery / runtime exact readback

**⛔ 前置条件**：**`B2-B2d` 已合并**（它隐含 B2-B2a → B2-B2 → B2-B2c → B2-B2d
整条单向链已完成）。2026-09-20 按 ADR-037 修正 —— 原文只写「B2-B2 已合并」。

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

**⛔ 前置条件**：**`B2-B3` 已合并，且其全部前置 checkpoint 均已完成** ——
即 `B2-B1 → B2-B2a → B2-B2 → B2-B2c → B2-B2d → B2-B3` 整条单向链。
2026-09-20 按 ADR-037 修正 —— 原文只写「B2-B1–B3 全部合并」。

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
backend/app/providers/base.py                           # 2026-09-20（ADR-037）：B2-B2a（ForwarderEgressProxyDTO + egress_proxies）+ B2-B2d（XrayOutboundDTO.credential_secret_ref -> str | None）
backend/app/infra/provisioning_state.py                  # 2026-09-20 补入（ADR-037 §1a）：仅 B2-B2d，Mihomo 模式下构造 loopback outbound
backend/app/providers/gateway/xray_composition.py        # 2026-09-20 补入（ADR-037 §1a）：仅 B2-B2d，None 时渲染不带 users 的 socks outbound
backend/app/providers/gateway/xray_file.py               # 2026-09-20 补入（ADR-037 §1a）：仅 B2-B2d，跳过 None
backend/tests/unit/test_desired_routing_snapshot.py      # 2026-09-20 补入（ADR-037 §1a）：仅 B2-B2d
backend/tests/unit/test_xray_composition.py              # 2026-09-20 补入（ADR-037 §1a）：仅 B2-B2d
backend/tests/guards/test_xray_writer_guard.py           # 2026-09-20 补入（ADR-037 §1a）：仅 B2-B2d
docs/80-decisions/ADR-025-mihomo-projection-generation-authority.md      # 仅状态改为 Accepted
docs/82-tasks/TASK-S04-mihomo-activation.md
docs/83-project-continuity.md
```

> **每个 checkpoint 的精确 allowed list 以上面「精确 allowed files」节为准**
> （`B2-B2a` / `B2-B2` / `B2-B2c` / `B2-B2d` 各不相同）。
> **下面这份总清单只是全部 checkpoint 的并集，本身不授权任何单个 checkpoint。**
> 判断某个文件能不能改，要同时满足两条：**在总并集里** ✅ **且在当前 checkpoint
> 自己的 exact list 里** ✅。

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
- `backend/app/providers/base.py` —— **这条只对 B2-B1 成立，2026-09-20 已被
  ADR-037 部分取代。** 原文：receipt producer 的改动写在
  `providers/transport/subscription.py` 与 `workers/transport_sync.py` 内部
  （"仅为计算 `content_hash`" / "仅为同事务提交 receipt"），没有证据表明需要改
  Protocol；要改它属于 `AGENTS.md` 铁律 5 的范围，停下来上报。
  **现状**：铁律 5 已经走完 —— `ADR-037` 就是那条 ADR。但**授权的不是 B2-B2c**：

  | checkpoint | 对 `providers/base.py` 的授权 |
  |---|---|
  | **B2-B2a** | ✅ **新增 `ForwarderEgressProxyDTO` 与 `DesiredForwarderState.egress_proxies`**（ADR-037 §4 写死的那些，不多不少） |
  | **B2-B2d** | ✅ **仅** `XrayOutboundDTO.credential_secret_ref: str \| None`（ADR-037 §1a.4），不碰 forwarder 侧任何字段 |
  | **B2-B1 / B2-B2 / B2-B2c** | ❌ **一个字都不许改**。B2-B2 与 B2-B2c **使用** B2-B2a 建好的类型，不修改其定义 |

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
