# ADR-023: Mihomo 全量配置、单写者与激活边界

- 状态: Accepted
- 日期: 2026-09-17
- 范围: Mihomo forwarder 的后续真实 registry wiring 前置架构合同
- 取代/补充: ADR-009、ADR-014、ADR-020、ADR-022 中关于 desired state、全量渲染、单写者、运行时证据的通用原则；仅对 Mihomo projection 生效

## 决策摘要

Lingsway 应用拥有 Mihomo 的完整、repo-owned 配置 projection。未来真实 wiring 只能通过一个进程内 `MihomoProjectionWriter`（其外部接口可以由 `MihomoForwarderProvider` 实现，但写者边界不可下沉到 runtime）执行。该写者是 Mihomo 全局 full-config 的唯一 writer，并在同一命名锁下完成 fresh DB snapshot、render、validate、backup、install、reload、health/projection verification 以及 durable finalization。

DB committed desired state 是唯一 desired-state authority。transport cache 只能提供已由 DB desired state 引用且经过 freshness 校验的物化 transport input；它不是独立意图来源。Mihomo `runtime.current()`、磁盘当前文件、运行时 fingerprint 和 API 返回的 current/config 都只能作为观察、baseline、rollback 或 verification evidence，严禁反向成为 desired state source。

采用 commit-first 的 durable state machine：先把 DB desired state B 与 pending Mihomo intent 一起提交，再对 B 进行全量渲染和运行时激活，健康及 exact projection verification 成功后再提交 finalization。只有 finalization commit 成功且锁释放前的结果可证明时，业务才可报告 APPLIED/success。任何不确定性都进入 DEGRADED / manual-intervention 的 fail-closed 状态并阻止后续 Mihomo writers。

## 1. Render ownership / boundary

### 1.1 唯一 authoritative renderer

`MihomoProjectionWriter` 拥有完整 Mihomo document 的 authoritative render。它必须从一个操作范围内、带明确 revision 的 `DesiredForwarderState` 生成整个 repo-owned document，而不是拼接旧文件或只渲染某个 listener。

`MihomoForwarderProvider.render()` 是 provider contract 的适配入口；真实实现不得自行读取 Session、ORM、文件、`runtime.current()` 或 transport cache，也不得保留 resolver/Session。调用方在 projection lock 内完成 fresh read 和输入组装，再把不可变 snapshot 传入 render。任何 standalone `ops/forwarder/render_mihomo_config.py` 只能是显式调用同一 composer 的运维入口，不能成为第二个生产 writer；它不得拥有一套不同的 composition 规则。

Renderer 必须覆盖 Mihomo 配置中所有由本仓库拥有的字段，包括 listeners/inbounds、proxy/provider 定义、proxy-groups、rules、DNS/策略中由 DB 或固定部署常量控制的部分。不能表达或验证完整 ownership 的字段必须在 wiring 前明确归类为 deployment constant、transport materialization input 或 unsupported；不得用“保留未知字段”掩盖 ownership 缺口。未被 fresh desired snapshot 引用的旧 proxy/listener/rule 必须从候选中消失。

### 1.2 三类输入的权限

| 输入 | 权限 | 规则 |
| --- | --- | --- |
| DB committed desired state | 唯一意图来源 | 由业务 transaction 提交；包含 Mihomo projection 所需的全部逻辑字段及 monotonic revision/operation identity；render 只读此 snapshot |
| transport cache | 受限 materialized input | 只可用于 DB 已引用的 transport record；必须校验 cache key、source identity、content hash/version 与 freshness deadline，并把这些值纳入 snapshot fingerprint；cache 缺失、过期、无法证明对应关系时拒绝 render，不能静默使用旧 cache |
| Mihomo runtime.current()/config/API/fingerprint | 只读 evidence | 可用于 pre-change backup、rollback 的 exact A、drift/identity check、post-apply observation 和 verification；永远不能填补 DB 字段、生成 desired rows 或决定下一次 desired config |

明确禁止：任何实现从 `runtime.current()`、磁盘 config、API current/config、上一次 candidate 或 fingerprint 反向创建/修订 desired state。运行时状态不是 desired-state source；runtime drift 只能产生 DEGRADED/manual 信号或触发按 DB desired state 的重新收敛。

## 2. Global full-config writer contract

### 2.1 Single writer and serialization

全局 Mihomo config 只有一个 writer boundary：`MihomoProjectionWriter`。禁止 registry、request handler、scheduler tick、ops script、health checker 或另一个 provider instance 直接调用 install/reload/restore。

所有会改变或验证该 global projection 的操作都必须取得同一稳定命名锁，例如 `lingsway:mihomo:full-config`。锁覆盖 fresh read、snapshot fingerprint、render、candidate validation、backup、install、reload、health/projection verification、DB finalization、rollback/compensation 以及 lock-release outcome classification。锁获取失败超时必须不写文件、不 reload、不 commit success，并返回 retryable/manual-safe failure。

进程内必须只存在一个 application-scoped writer/runtime owner；多 worker/进程仍必须由同一外部可见命名锁串行化。锁不是 freshness 的替代品：每次取得锁后都必须重新读取当前 DB desired state，不能复用请求开始时的旧 ORM snapshot。

### 2.2 Freshness and stale-render prevention

在锁内开启操作 transaction，读取 committed desired state、相关 active bindings、transport materialization metadata 和 deployment constants 的当前版本，并计算 `snapshot_revision` 与 deterministic `desired_fingerprint`。render 只使用这个 snapshot。

render/apply 前不得释放锁。若 render 或 I/O 期间发现 desired revision、引用关系、transport cache hash/version 或 deployment settings 已变化，候选立即作废；不得 install/reload 该候选。必须 rollback/close 当前 attempt、重新在同一锁下 fresh-read，再由新 operation identity 重试。

finalization commit 前必须再次以锁定/current read 确认仍在 finalize 同一个 operation identity，DB desired revision 与候选 fingerprint 相符，且没有更高 revision。任何 mismatch 都是 stale candidate，不能报告 APPLIED。

## 3. Transaction and commit ordering

唯一正常顺序如下：

1. 在普通业务 transaction 中准备 prospective B；此时 B 未提交，不得修改 Mihomo runtime。
2. 取得 Mihomo projection lock，并用 fresh DB read 建立 active projection 的 A/B 边界；将 B、必要的 identifier-only pending Mihomo intent 和 operation identity 一起写入 DB。
3. 提交第一 durable commit。提交成功后，B 成为 authoritative desired state；Subscription/Order 等 customer-success 状态仍保持 PROVISIONING/PENDING，不能报告成功。
4. 仍持锁，重新读取已提交 B，生成完整 candidate，进行 schema/semantic validation 和 exact ownership checks。
5. 保存 exact previous runtime baseline A（原始 bytes/config identity，不是重新 render 的 A），原子 install candidate，执行 allowlisted Mihomo reload/apply。
6. 对 post-apply runtime 做 health verification，并验证 repo-owned projection 与 candidate fingerprint/operation identity 一致。仅 HTTP reload 成功、文件写成功或 fingerprint 相同都不足以视为健康。
7. verification 全部成功后，提交第二 durable finalization：pending intent 标记完成，记录 candidate fingerprint、DB revision、verification evidence，并将业务状态转为 APPLIED/ACTIVE。
8. 成功提交的 acknowledgement 可证明后释放锁；再执行非阻断通知。锁释放未确认时不得伪称 clean success，须报告 committed-with-warning 并阻止不安全的并发 writer，直到恢复确认。

DB commit 不得发生在 runtime apply 前作为“成功”提交，也不得在 apply 后把 DB B 回滚为 A 以掩盖 crash/ack uncertainty。第二 commit 的异常或 acknowledgement loss 必须由 fresh independent DB read 分类为 LANDED、ABSENT 或 UNKNOWN：UNKNOWN 进入 writer-blocking DEGRADED/manual state，绝不猜测成功或失败。

## 4. Activation, health, rollback and compensation

### 4.1 Activation and APPLIED

`APPLIED` 只允许在以下条件全部成立时产生：

- 第一 durable commit 已确认，且当前 fresh DB desired state 仍是该 operation 的 B；
- candidate 是从该 fresh B 全量 deterministic render 的结果，并通过完整 validation；
- exact A 已保存；
- install、allowlisted reload/apply 均成功；
- post-apply Mihomo health endpoint/API 可达且报告健康；
- repo-owned projection readback/observation 与 candidate fingerprint、operation identity 和 DB revision 一致；
- 第二 durable finalization commit 成功且结果可由 fresh read 证明；
- 未发生 lock-release uncertainty。

`install`、`reload` 或 health 任一阶段失败都不得返回 APPLIED。健康无法查询、readback 不一致、revision 过期、锁状态未知均为 UNKNOWN/DEGRADED，不是健康或成功。

### 4.2 Exact rollback baseline

只有 exact previous runtime baseline A 可以作为 runtime rollback 目标。A 必须是本次 operation 在锁内捕获的原始 previous document/identity；不能通过 DB 重渲染“猜回 A”，不能从 runtime 在失败后再次读取的可能已变更状态构造 A。

- 在第一 durable commit 之前失败：不应有 runtime mutation；rollback DB transaction，确认没有 pending B，然后结束为 FAILED/NO_SIDE_EFFECT。
- 在 B 提交后、candidate activation 期间失败：先用 exact A restore + reload + health/readback verification。若 A 恢复并验证成功，B 仍是 DB authority，不能把 DB 偷改回 A；operation 保持 retryable PENDING，后续 writer 被 pending intent 阻塞。
- A restore、reload 或 rollback verification 任一失败：状态为 DEGRADED/UNKNOWN，禁止任何后续 writer，要求人工确认或受控 recovery；不得报告已回滚、不得继续下一个 saga step。

### 4.3 Downstream saga failure after Mihomo activation

Mihomo activation 之后的 accounting/gateway/subscription/notification 等后续步骤失败时，补偿按 durable boundary 分两类：

1. 若失败发生在第一 durable commit 之前，Mihomo 不得已激活；若实现发现有 partial runtime mutation，补偿 exact A，并验证 A；DB transaction rollback，外部资源按各自 ADR compensation。
2. 若 Mihomo 已按本合同完成 activation，第一 commit 已确认 B，则 B 已是 authoritative。后续失败**不得**把 Mihomo 恢复到 A，也不得把 runtime current 反向写回 DB。补偿只处理失败步骤自己拥有的副作用（例如 accounting user 按其 ADR disable、未完成 token/notification 标记失败），保留 Mihomo B 和 pending intent，进入 retryable PROVISIONING/DEGRADED；恢复 worker 必须从 fresh DB B 重新 render/apply/verify，不能从 A 推导 B。

仅当业务明确执行一个已授权的取消/回滚 operation，且该 operation 将新的 desired state C 先 durable-commit，才可把 Mihomo 变更为 C；取消不是隐式的 A rollback。这样不会因后续步骤的单点失败把 DB desired state 与 runtime 撕裂。

## 5. Crash and failure semantics

| Crash/failure point | Authority and required state |
| --- | --- |
| Before first DB commit | Fresh DB is A; no committed B. Discard uncommitted work; no success. |
| After B+pending commit, before install | Fresh DB B is authority; runtime A/unknown. Keep pending; recover B under lock. |
| During install/reload | DB B is authority; runtime outcome unknown. Fail closed, retain pending, verify/converge B before another writer. |
| After runtime B, before health/readback | B is authority; APPLIED is not proven. Verify B or enter DEGRADED; never claim success. |
| After health/readback, before finalization commit | B is authority; retry finalization after fresh evidence. Do not restore A merely because the process crashed. |
| During/after finalization commit | Fresh DB observer classifies LANDED/ABSENT/UNKNOWN. UNKNOWN is writer-blocking manual state. |
| During exact A rollback | If restore/reload/health evidence is incomplete, runtime is UNKNOWN and the writer remains locked out until controlled recovery. |
| After finalization, before lock release | B/APPLIED is authority; verify lock release. If release is unverified, committed-with-warning and no concurrent writer assumption. |

A process restart always starts with fresh committed DB desired state and pending intent. It never bootstraps desired state from runtime config, backup file, or cache. Any install/reload/health/rollback/commit failure is classified as known only when the required evidence is observed; otherwise runtime or commit outcome is UNKNOWN and fail-closed.

## 6. Fail-closed and implementation gates

Before real `FORWARDER_PROVIDER=mihomo` wiring, the implementation must provide:

- one writer instance and one named lock path used by every writer;
- a fresh, immutable desired-state snapshot with revision/fingerprint and transport-cache freshness evidence;
- complete full-config render ownership, including explicit treatment of every repo-owned Mihomo section;
- exact A backup and verified restore path;
- post-reload health plus exact repo-owned projection verification;
- durable pending/finalization evidence and crash recovery;
- tests for two concurrent writers, stale snapshot, stale cache, install/reload/health failure, rollback failure, downstream saga failure, second-commit ACK loss, process crash at every table row, and fail-closed writer blocking.

No implementation agent may invent a second source of truth, a different lock span, an apply-before-commit success path, a silent runtime-to-DB reconciliation, or an automatic A rollback after post-commit downstream failure. This ADR closes `MIHOMO_RENDER_BOUNDARY_BLOCKER` and the downstream-compensation and global-writer serialization/freshness/commit-order blockers. Real registry wiring, schema changes if durable evidence is not representable by existing records, deployment, credentials, reloads, and production approval remain separate work.
