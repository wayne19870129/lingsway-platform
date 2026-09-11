# TASK-T16 — 把真实 provider 接入 registry(目前全平台恒为 mock/noop)

风险等级：high（涉及真实凭据与外部副作用，且是当前所有业务逻辑的信任前提）

## 目标

在实现 T5G(scheduler 出口漂移检测)时发现：`backend/app/providers/registry.py`
的 `build_registry()` 目前**无论环境变量怎么设置，都只能装配 mock/noop
实现**——除了 `mock` 之外的任何取值都会直接 `raise ProviderConfigurationError`
（例如 `if settings.egress_provider != "mock": raise _unsupported(...)`，
`accounting_provider`、`gateway_provider`、`forwarder_provider`、
`payment_provider`、`notify_provider`、`email_provider`、`captcha_provider`、
`storage_provider`、`transport_provider_mode` 全部同样只接受各自的
mock/noop 取值）。

这意味着：`backend/app/providers/egress/webshare.py`、
`providers/gateway/xray_file.py` 等已经写了部分实现的真实 provider，
**目前无法通过任何环境变量组合被实际选中**。`place_order` 里调用的
`build_registry(get_settings()).egress.capacity()`，无论部署环境的
`EGRESS_PROVIDER` 填的是什么，实际跑的永远是 `MockEgressProvider`。
换句话说：当前代码库如果原样部署，不会真的调用 Webshare、Marzban、
Xray、Mihomo 等任何外部系统——所有"看起来接好的"业务流程都是在跟 mock
数据交互。这比 T5G 本身的范围大得多，值得单独立项。

## 约束

1. 每接入一个真实 provider,必须先确认对应实现是否已经完整(参照
   `docs/70-external-facts.md`记录的已实测行为),不完整的部分(例如
   `providers/egress/webshare.py` 的 `list_endpoints()`、`capacity()`、
   `get_tenant_usage()`、`get_credentials()` 当前都是占位实现,注释写着
   "response mapping arrives with T5 migration"、`NotImplementedError`)
   要先补完,不能让 registry 选中一个还会在运行时报
   `NotImplementedError` 的 provider。
2. 真实 provider 的接入必须逐个来,每接入一个就要有对应的集成测试
   （真实凭据只能来自 `/etc/lingsway/*.conf` 或 CI 的
   `STAGING_*`/`PROD_*` secret，不得写进代码或测试固件）。
3. 不得为了让 registry 通过而放宽或跳过 `webshare.py` 现有的写操作
   硬拦截、限速、429 退避逻辑。
4. 本任务只负责"让真实实现可以被选中并被验证跑通"，不负责把默认部署
   配置从 mock 切换成真实值——`.env.example` 的默认值、`deploy/` 的
   `inventory.example.yml` 默认给哪个 provider，由部署时的运维决定。
5. 这是一个高风险任务：一旦某个真实 provider 被错误接入并在默认配置下
   被意外选中，可能对接真实 Webshare/Marzban 账号产生真实副作用。接入
   顺序建议从只读能力开始（`list_endpoints`、`capacity`、`get_usage`），
   有写副作用的能力（`create_tenant`、`replace_endpoint`）放在后面并
   要求先过一遍 `backend/tests/guards/test_webshare_guard.py` 同等级别
   的护栏测试。

## 允许修改的文件

- `backend/app/providers/registry.py`
- `backend/app/providers/egress/webshare.py`（补完占位实现）
- `backend/app/providers/**` 下其余真实 provider 实现（按接入顺序）
- `backend/app/core/config.py`（如需新增 provider 相关配置项）
- `backend/tests/**`
- `docs/70-external-facts.md`、`docs/80-decisions/`（记录接入过程中发现的
  实现落差或需要的新决策）

## 验收标准

- `build_registry()` 在对应环境变量设为真实 provider 名称时，不再抛
  `ProviderConfigurationError`，且返回的实现不含遗留的
  `NotImplementedError` 占位方法。
- 每个新接入的真实 provider 至少有一条集成测试，覆盖其只读路径
  （`list_endpoints`/`capacity`/`get_usage` 等）在真实或已授权的测试账号
  上确实返回数据，而不是仍然依赖 mock。
- 全 mock/noop 的组合（`make test-unit`）继续在无网络、无 Docker 环境
  下通过——真实 provider 是新增选项，不是替换默认值。
- 明确记录：接入完成前，`docs/10-deploy-new-server.md` 里任何暗示
  "生产环境会真的调用 Webshare/Marzban" 的描述都需要标注为
  "取决于本任务是否已完成"，避免误导实际部署。

## 阶段一：现状盘点与接口契约（2026-09-11，只读分析，未改动业务代码）

按用户要求，本任务不会一次性整体执行；这一节先只做只读盘点，逐项核对
`registry.py` 与每个真实 provider 目录的实现完整度、配置入口、凭据来源、
外部 API 响应映射状态、现有 guard/integration 测试覆盖，产出接下来分阶段
接入的具体顺序建议。**没有启用任何真实 provider、没有调用任何写接口、
没有接触真实凭据、没有部署。**

### 关键发现：真实 provider 分成两类,风险模型完全不同

盘点前的 TASK-T16 原文把所有"非 mock 实现"当成同一类风险来描述,但逐个
读代码之后发现,`backend/app/providers/**` 下实际存在的四个非 mock/noop
实现,分成两类性质完全不同的东西：

**A 类：外部网络 API 客户端**（需要真实凭据、可能产生真实外部副作用/
账单）：
- `egress/webshare.py`——`WebshareProvider`
- `accounting` 分类——**没有任何实现文件**（见下面"意外发现"）

**B 类：本机文件/进程管理器**（在同一台 VPS 上操作本地文件和本地
进程重载，不需要走外部网络 API，不涉及第三方账单）：
- `gateway/xray_file.py`——`XrayFileProvider`（九步安全重载）
- `forwarder/mihomo.py`——`MihomoForwarderProvider`（文件+热重载+回滚）
- `transport/subscription.py`——`SubscriptionTransportProvider`

这个区分很重要：B 类的"真实副作用"范围是本机配置文件和本机进程，不涉及
外部账号/账单，而且已经有相当扎实的护栏测试（见下表）；A 类才是原始
TASK-T16 描述的"真实凭据 + 外部副作用"那种高风险。**建议接入顺序应该
优先考虑 B 类，而不是原文暗示的"从 `list_endpoints`/`capacity` 这类
外部只读 API 开始"**——B 类的只读/幂等性质在护栏测试里已经证明过，A 类
的只读方法（`list_endpoints`/`capacity`）虽然确实是只读，但实现本身还
没补完（见下表），补完这部分工作量不比直接做 B 类小。

### 逐项盘点表

| Provider 分类 | 真实实现文件 | 完整度 | 配置入口 | 凭据来源 | 现有测试覆盖 | 目前是否被任何代码路径实例化 |
|---|---|---|---|---|---|---|
| egress | `egress/webshare.py`（`WebshareProvider`） | **部分**：`list_endpoints()`/`capacity()`/`create_tenant()`/`update_tenant_quota()`/`replace_endpoint()` 都会发真实请求，但响应体全部被丢弃，返回写死的空/零值 DTO（`list_endpoints()` 恒返回 `[]`，`capacity()` 恒返回全零）；`get_tenant_usage()`/`get_credentials()` 直接 `raise NotImplementedError`。写操作有明确的路径白名单守卫（`_guard`），永久拒绝任何 purchase/renew/payment/billing 路径。 | `egress_provider` 只接受 `"mock"` | **没有 `webshare_api_key`（或同类）配置项**——`Settings` 里完全没有为 `WebshareProvider.__init__` 的 `api_key` 参数留位置 | `test_webshare_guard.py`、`test_webshare_procurement.py`：覆盖写操作白名单、限速/429 退避、procurement 只读适配器；**没有测试覆盖真实响应体到 DTO 的映射**（因为这部分还没写） | 否——只在测试文件里手动构造，`registry.py`/其余应用代码里从未 `import WebshareProvider` |
| accounting | **无**（`accounting/` 目录只有 `mock.py`） | 不适用——**这个 provider 类别完全没有真实实现**，连一个空壳文件都没有 | `accounting_provider` 只接受 `"mock"`；但 `Settings` 里已经有一整组 `marzban_*` 字段（`marzban_base_url`/`marzban_admin_username`/`marzban_admin_password`/`marzban_default_protocol`/`marzban_default_inbounds_json`/`marzban_verify_tls`），`validate_runtime_safety()` 甚至已经在校验"生产环境选了 marzban 但配置还是默认值就报错"——**配置层已经为一个不存在的实现做好了校验，这是意外发现，见下方** | 同上，字段已存在但没有对应实现读取它们 | 无——没有实现就没有测试 | 否——不存在这个类 |
| gateway | `gateway/xray_file.py`（`XrayFileProvider`） | **较完整**：九步安全重载序列、校验失败/重载失败/健康检查失败的完整回滚路径都已实现，没有发现 `NotImplementedError`/占位注释 | `gateway_provider` 只接受 `"mock"` | 不需要外部网络凭据——操作的是本机 Xray 配置文件和本机进程（`LocalXrayRuntime`） | `test_safe_reload.py`：9 个测试，覆盖校验失败提前拦截、重载失败完整回滚、缺失路由/用户/出站的各种拒绝场景、Dockerfile 资产完整性 | 否——`registry.py` 从未 import，只有测试文件直接构造 |
| forwarder | `forwarder/mihomo.py`（`MihomoForwarderProvider`） | **较完整**：文件安装+热重载+失败回滚，没有发现 `NotImplementedError`/占位注释 | `forwarder_provider` 只接受 `"mock"` | 不需要外部网络凭据——本机 Mihomo 配置文件和本机进程（`LocalMihomoRuntime`） | `test_db_adapters.py::test_mihomo_render_failure_restores_exact_pre_operation_state`：覆盖渲染失败时精确回滚到操作前状态 | 否——`registry.py` 从未 import；`MihomoForwarderProvider` 类定义本身是唯一"看到这个类名"的应用代码位置 |
| transport | `transport/subscription.py`（`SubscriptionTransportProvider`） | 未发现 `NotImplementedError`/占位注释（未逐行审计到函数级） | `transport_provider_mode` 只接受 `"mock"` | 不需要外部网络凭据 | `test_transport_provider.py`：有专门的单元测试文件 | 否——`registry.py` 从未 import |
| payment / notify / email / captcha / storage | 无 | 不适用——这五类目前都只有 mock/noop 实现，没有任何真实实现文件或占位文件 | 各自只接受 `"mock"`/`"noop"` | 不适用 | 不适用 | 不适用 |

### 意外发现：`accounting_provider=marzban` 是一个"配置存在但实现不存在"的悬空引用

这不是本次盘点原本要找的东西，但值得单独记录：`Settings.validate_runtime_safety()`
里有这一段：

```python
if self.app_env == "production" and self.accounting_provider == "marzban":
    if "example.invalid" in self.marzban_base_url:
        raise ValueError("Production MARZBAN_BASE_URL must be configured")
    if "CHANGE_ME" in {self.marzban_admin_username, self.marzban_admin_password}:
        raise ValueError("Production Marzban credentials must be configured")
```

这段代码假装"选 marzban 作为 accounting_provider"是一个真实存在的选项，
还煞有介事地校验它的凭据是不是默认值——但 `registry.py` 里
`accounting_provider` 唯一接受的值是 `"mock"`，选 `"marzban"` 在
`build_registry()` 这一步就会先被 `_unsupported()` 拒绝，**这段校验代码
永远不会被真正走到**（配置层的校验和实际的 provider 选择逻辑不同步）。
`accounting/` 目录下也确实没有任何名叫 Marzban 的类。这意味着 A 类
provider 里，accounting/Marzban 比 egress/Webshare 的差距更大——Webshare
好歹有一个写了大半的类，Marzban 是从零开始。

### 建议的分阶段接入顺序（本阶段只给建议，不实施）

1. **阶段二（建议先做，风险最低）：B 类里选一个先接入 registry**——
   `gateway/xray_file.py` 或 `forwarder/mihomo.py` 二选一，因为它们已经
   有扎实的护栏测试、不涉及外部凭据、`registry.py` 目前拒绝它们纯粹是
   因为"只接受 mock"这一条硬编码检查，没有其它技术障碍。这一步能验证
   "把一个真实实现接进 `ProviderRegistry` 需要改哪些地方"（`registry.py`
   的分支逻辑、`Settings` 是否需要新字段、`build_registry()` 的构造参数）
   ，且爆炸半径仅限本机配置文件，不触及任何外部账号。
2. **阶段三：补完 `egress/webshare.py` 的响应映射**——`list_endpoints()`/
   `capacity()` 的真实响应体到 DTO 的映射需要先补上（目前是发真实请求、
   丢弃真实响应、返回假数据），且需要找到 `api_key` 的合理配置/凭据来源
   （目前 `Settings` 里没有这个字段）。补完后只接入只读方法
   （`list_endpoints`/`capacity`），写方法（`create_tenant`/
   `update_tenant_quota`/`replace_endpoint`）单独放到更后面，且要求先补
   `get_tenant_usage`/`get_credentials` 的 `NotImplementedError`。
3. **阶段四（工作量最大，建议最后做）：从零实现 Marzban accounting
   provider**——目前没有任何代码可以复用，需要先写一个新类，参照
   `Settings` 里已经存在的 `marzban_*` 字段推测出的接口形状（base_url、
   admin 账号密码、默认 protocol、默认 inbounds、TLS 校验开关），且需要
   先解决"配置层校验和 registry 实际选择逻辑不同步"这个意外发现。
4. **payment/notify/email/captcha/storage 这五类不在本次盘点的优先级
   讨论范围内**——目前完全没有真实实现，是否需要真实实现取决于产品是否
   真的要接入对应外部服务（例如真实支付网关），这属于新的产品决策，不是
   "把已经写了一半的代码补完"，超出 TASK-T16 本身"接入已存在的真实
   实现"这个框架，需要单独立项讨论。

### 本阶段验收

- 只产出这份文档更新，`backend/app/**` 零改动。
- 没有调用任何真实 provider 的网络请求（本次盘点全部通过阅读源码完成，
  没有执行任何会触发真实 HTTP 请求的测试或脚本）。
- 没有接触任何真实凭据（`.env`、`/etc/lingsway/*.conf`、CI secrets 均未
  读取）。
- 没有修改 `registry.py`、`config.py` 或任何 provider 实现文件。
- 下一步（阶段二起）需要用户确认要不要按上面建议的顺序推进，以及是否
  同意"先做 B 类、后做 A 类"这个和原始任务描述不同的优先级调整。
