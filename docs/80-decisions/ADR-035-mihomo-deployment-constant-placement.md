# ADR-035: Mihomo deployment-owned 常量的落点

- 状态: **已接受（Accepted）**
- 日期: 2026-09-20
- 决策范围: **仅** `_validate_deployment_constants()` 白名单里两个没有默认值的
  deployment-owned key —— `external-controller` 与 `api-secret-ref` —— 各自
  落在哪里。
- 前置: **ADR-025 §3**（authority taxonomy）。本 ADR 在它已画好的
  `deployment, not DB` 类**内部**做选择，**不重新讨论 authority class**。
- 实施 TASK: `docs/82-tasks/TASK-S04-mihomo-activation.md`（S04-B2-B2）

> **生效时点**：本 ADR 随其 PR 合并进 `main` 即生效。**没有额外闸门**：
> S04-B2-B2 不需要等任何人再确认一次，按下面写死的结论直接实现即可。

## 1. ADR-025 §3 已经裁定、本 ADR 不得重开的部分

| 项 | 已定的结论 | 出处 |
|---|---|---|
| authority class | **`deployment, not DB`** | ADR-025 §3 表格第 2 行 |
| 读取边界 | **validated `Settings` / repo-owned deployment constants** | 同上 |
| 是否可以迁进数据库 | **不可以**——原文：“Deployment constants are never migrated into the database” | ADR-025 §3 |
| 进 manifest 的方式 | 已验证的值**逐字（verbatim）**进 canonical manifest，因而被 fingerprint 覆盖 | ADR-025 §3 / §4 |
| `api-secret-revision` 的来源 | **fresh 的 purpose-bound `Secret.revision`**，不是第三个配置来源 | ADR-025 §4 / §8 |

**“版本化 deployment-constant DB 表”不是本 ADR 的候选。** 要走那条路必须
先 supersede ADR-025，那是另一件事，本 ADR 不做，也不暗中留口子。

## 2. 判别标准（两个字段结论不同，不是前后不一致）

`deployment, not DB` 这一类里有两种性质完全不同的值，ADR-025 §3 把它们并列
为读取边界（`Settings` / repo-owned constants）但没说哪个用哪个。判据只有
一条，本 ADR 定下来，以后同类问题照此判：

> **两个都正确的部署，这个值会不会不一样？**
> **会 → validated `Settings`。不会 → repo-owned constant。**

理由：`Settings` 的成本是**多一个可以配错的轴**，它只有在“确实需要按部署
取不同值”时才换得回来；repo-owned constant 的成本是**改它要发版**，它只有
在“所有部署都该是同一个值”时才不构成限制。把不变的值放进 `Settings`，等于
凭空造一个误配置入口却换不到任何能力。

## 3. 决定 A —— `external-controller` → validated `Settings`

**落点**：`backend/app/core/config.py` 的 `Settings` 新增字段

```
mihomo_external_controller: str = ""
```

- **默认空字符串，表示“未配置”。** 不写任何示例地址做默认值。
- 环境变量名按现有 `from_env()` 的规则自动为 `MIHOMO_EXTERNAL_CONTROLLER`
  （字段名大写），**不需要为它写特例分支**——它是普通 `str`，落在
  `from_env()` 已有的 `else` 分支上。

**为什么是 `Settings`**：`host:port` 是**逐部署不同**的值，按 §2 的判据直接
落到 `Settings`。写成 repo-owned constant 等于把某一个生产地址写进 git，
而且 S04-C 必然要再搬一次——正是“明知会被推翻的临时落点”。

**仓库里已有的同类先例（两条，都在同一个函数里）**：

| 先例 | 闸门 | 校验位置 |
|---|---|---|
| `marzban_base_url`（`core/config.py:27`） | `accounting_provider == "marzban" or gateway_provider == "xray_file"` | `validate_runtime_safety()` |
| `transport_cache_root`（`core/config.py:62`） | `transport_provider_mode == "subscription"` | `validate_runtime_safety()` |

两条都是“**按 provider/mode 选中才校验**”的 fail-closed 形状，本字段照抄：

```
forwarder_provider == "mihomo"  →  mihomo_external_controller 不得为空白
```

### 3.1 两处校验，职责不重叠，谁都不能因为对方存在而放松

这一点必须写死，否则会长出第二个事实来源：

| 位置 | 校验什么 | 为什么在这儿 |
|---|---|---|
| `Settings.validate_runtime_safety()` | **只查“有没有”**：`forwarder_provider == "mihomo"` 时 `mihomo_external_controller.strip()` 不得为空 | 启动即失败，而不是等到第一次 reconcile |
| `mihomo_projection.py` 的 `_validate_controller_address()` | **格式的唯一权威**：IP 字面量 / 端口范围 / loopback / unspecified / IPv6 方括号 | 它是值进 `deployment_constants`、进 manifest、被 fingerprint 覆盖的那道边界 |

**`core/config.py` 不得 import `providers/forwarder/mihomo_projection.py`**
去复用格式校验——那是反向依赖（`config` 被 `registry` 读，`registry` 才造
provider）。**也不得在 `config.py` 里把格式规则抄一遍**：同一套规则写两处，
下次只改一处就是本仓库已经吃过几次亏的双事实来源。

因此：**空值在启动时被挡住；格式错误在 projection 边界被挡住并抛
`MIHOMO_CONTROLLER_ADDRESS_INVALID`。** 两道都不许省。

### 3.2 字段和它的校验必须同一个 PR 落地

ADR-025 §3 的措辞是 **validated** `Settings`——一个没有校验的 `Settings`
字段不满足那条读取边界，而它的值是**逐字进 manifest 并被 fingerprint 覆盖**
的。所以不允许“B2-B2 加字段、S04-C 再加校验”的拆法：中间那段时间里，
一个未经校验的部署常量正在参与 fingerprint。

**B2-B2 期间这条校验事实上不会触发**（`build_registry()` 还不接受
`mihomo`），这是**允许的**，与 B2-B1 里“resolver 已交付但尚无生产调用点”
同一性质、同一理由：正确性由本 checkpoint 的单测完整覆盖。

**已实测**：`backend/tests/unit/test_mihomo_generation.py:25` 的
`build_registry(Settings(forwarder_provider="mihomo"))` **不会**因此变红——
`validate_runtime_safety()` 只在 `Settings.from_env()` 里被调用，直接构造
`Settings(...)` 不走它。

## 4. 决定 B —— `api-secret-ref` → repo-owned constant

**落点**：`backend/app/infra/credential_resolver.py`，紧挨着已经在那里的
`MIHOMO_CONTROLLER_SECRET_PURPOSE`：

```
MIHOMO_CONTROLLER_SECRET_REF = "mihomo/api-secret"
```

**值就是 `"mihomo/api-secret"`**——这是当前测试与集成用例里事实上已经统一
使用的字符串（`test_mihomo_projection.py`、`test_db_adapters.py`、
`test_mihomo_reconciliation.py`、两个 guard 测试都是它），定死它带来零改动。

**为什么不是 `Settings`**，四条，按分量排：

1. **它是复合查找键的一半。** `reveal_secret_snapshot_for_purpose()` 用
   `(secret_ref, purpose)` 两列联合定位一行（`core/secrets.py:185-190`）。
   `purpose` 已经是 repo-owned constant。把 `ref` 挪进 `Settings` 会让一对
   必须配套的值分属两个所有权域：部署方可以配出一个与 purpose 对不上的
   ref，而**任何配置校验都查不出来**（要查就得连数据库，`Settings` 校验
   不连库）。失败会推迟到第一次 reconcile 才暴露。
2. **按 §2 的判据它不变。** 每个部署都只有一行 Mihomo controller secret，
   由 purpose 唯一确定；没有任何场景需要两个部署用不同的 ref。
3. **换不到能力，只多一个误配置轴。** 见 §2。
4. **它不是密钥。** `secret_ref` 是**不透明引用**，明文只存在于
   `secrets.ciphertext` 里。把引用写进仓库不泄露任何东西——这一点由 §6 的
   边界表保证，不是靠“看起来不像密钥”。

**它仍然要进 `deployment_constants` 映射**（而不是在更深处写死），因为
ADR-025 §4 要求 manifest 覆盖 “controller-secret opaque reference”：走这条
路，常量一旦被改，fingerprint 就会变，generation 因此必须新开一代。

**这一条不需要新增任何允许文件**——`credential_resolver.py` 早已在 S04-B2-B
的清单里。

## 5. 明确否决的替代方案

| 方案 | 否决理由 |
|---|---|
| 版本化 deployment-constant **DB 表** | 需先 supersede ADR-025 §3（“never migrated into the database”）。不是本 ADR 的范围 |
| 绕过 `Settings` 直接 `os.environ` 读 | 绕开 `from_env()` 的统一转换与 `validate_runtime_safety()`，等于第二条配置通道 |
| `external-controller` 写成 repo constant | 把某个生产地址写进 git，且 S04-C 必然再搬一次——明知会被推翻的临时落点 |
| `api-secret-ref` 放 `Settings` | 见 §4 四条 |
| 两个字段都推到 S04-C | B2-B2 的 loader 没有这两个值**根本构造不出** `DesiredForwarderState`；推迟等于让 B2-B2 无限期挂着 |
| 在 `config.py` 里复制一份地址格式校验 | 双事实来源，见 §3.1 |

## 6. 安全边界（不因本 ADR 产生任何新的明文路径）

**controller secret 明文**在本 ADR 之后仍然**只**存在于 `secrets.ciphertext`
与 `ControllerSecretSnapshot.value` 的进程内生命周期里。以下位置**一个都不
允许**出现明文：

- `Settings` / `.env` / 任何 plaintext config——本 ADR 只往 `Settings` 放
  一个**地址**，不放密钥；
- desired-state DB row；
- canonical manifest（ADR-025 §4 明列排除 plaintext controller secrets）；
- generation row（ADR-025 §3“Generation metadata is not desired-state
  authority”）；
- transport receipt；
- 日志与异常（`AGENTS.md`：已知凭据字段最多 first-4/`****`/last-4）；
- runtime-independent snapshot。

`api-secret-revision` 的来源**不变**：fresh 的 purpose-bound
`Secret.revision`，由 `SqlMihomoControllerSecretResolver` 读出，**不来自**
本 ADR 落的任何一个常量或 `Settings` 字段。

**一条部署侧义务，写在这里免得以后重新推导**：某个部署真要启用 Mihomo 时，
必须先存在一行 `secret_ref = "mihomo/api-secret"` 且
`purpose = "MIHOMO_CONTROLLER_API_SECRET"` 的 `Secret`。这是**运维动作，
不是代码**，且**本 ADR 不授权任何人去做它**——生产激活仍需 User 明确批准。

## 7. 对 TASK-S04 的后果（已同步写进 TASK）

- **S04-B2-B2 的允许文件新增两个**：`backend/app/core/config.py`、
  `backend/tests/unit/test_registry.py`。两者原本只在 S04-C 清单里，现在
  **两份清单都有**——这不是把它们从 S04-C 拿走，而是按下面的边界切开。
- **B2-B2 在这两个文件里只做一件事**：加 `mihomo_external_controller` 字段
  + `validate_runtime_safety()` 里 `forwarder_provider == "mihomo"` 闸门下的
  **非空校验**，以及它的单测。
- **S04-C 保留的仍然是**：`build_registry()` 接受 `FORWARDER_PROVIDER=mihomo`、
  lifecycle/factory 边界、`.env.example`、以及 S04-C 自己新引入的字段的校验。
- **`.env.example` 留在 S04-C**，理由与 §3.2 正好相反：它是纯文档，不参与
  fingerprint，也不影响 fail-closed 行为；而**具体要补的那一行已经写死在
  TASK 的 S04-C 段里**，不靠谁记得。
- **B2-B 的既有红线不变**：B2-B 结束时必须仍有测试证明
  `FORWARDER_PROVIDER=mihomo` **被 `build_registry()` 拒绝**。本 ADR 没有
  放宽这一条——它加的是一个地址字段，不是一个开关。

## 8. 重新评估条件

- ADR-025 §3 的 authority taxonomy 被 supersede；
- 出现**确实需要按部署不同**的 `api-secret-ref`（例如一个部署里存在多行
  Mihomo controller secret 并存），此时 §2 的判据会把它翻到 `Settings`；
- `_validate_deployment_constants()` 的白名单新增一个没有默认值的 key——
  按 §2 判一次落点，不需要新 ADR，除非它落不进 `deployment, not DB` 这一类。

## 9. 本 ADR 不授权什么

不授权生产激活、不授权部署、不授权 `build_registry()` 放行 `mihomo`、
不授权任何人创建或轮换 `MIHOMO_CONTROLLER_API_SECRET`。
