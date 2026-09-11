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

### 关键发现：真实 provider 至少分三类，不是简单的"外部 vs 本机"二分

**这一节是第一版盘点的修正版**——第一版把"四个非 mock/noop 实现"分成
"A 类外部 API / B 类本机文件进程"两类，Work 的独立审查指出这个二分法
本身不准确，逐条核实后确认审查是对的，改正如下。这里的"四个"具体指四个
**已经有实现代码的文件**：`egress/webshare.py`、`transport/subscription.py`、
`gateway/xray_file.py`、`forwarder/mihomo.py`；`accounting` 分类的
"marzban"是第五个、但只是 `Settings` 里配置校验承认的一个取值名字，
`accounting/` 目录下**没有任何对应的实现文件**（见下面"Marzban 现状"一节）
——把它和前面四个已实现文件并列称呼容易让人误以为它也有代码，这里先
明确区分开，避免歧义。

**A 类：外部网络 API 客户端，需要真实凭据、可能产生真实外部副作用/
账单：**
- `egress/webshare.py`——`WebshareProvider`（Webshare API，Token 认证）
- `accounting` 分类——**没有任何实现文件**（见下面"Marzban 现状"）

**C 类：需要真实外部网络访问，但访问对象不是"平台账户 API"而是获取
配置数据；本地也会有写副作用（缓存文件）：**
- `transport/subscription.py`——`SubscriptionTransportProvider`

第一版盘点把这个类归进了"本机、无外部凭据"的 B 类，**这是一个核实后确认
的错误**：`sync_nodes()` 会对 `self._url`（订阅链接）发起真实的
`httpx.Client.get()` 请求——这个 URL 本身通常带有不能公开的私有 token
（`test_subscription_adapter_refreshes_without_exposing_url` 这个测试
名字本身就在提示这一点：URL 需要保密）；拿到响应后还会通过
`write_provider_cache()` 在本地写一份缓存文件。所以它既不是"纯本机
无凭据"，也不是"平台账户 API"那种典型 A 类——单独列成 C 类，接入前至少
要分别考虑三件事：①订阅链接获取（外部只读 HTTP，URL 本身是敏感信息，
需要按凭据对待）；②本地缓存文件写入；③本地激活/生效（改变哪个 Mihomo
实例读这份缓存），这三件事的风险和实现状态可能不一样，不能一次性打包
评估。

**B 类：本机文件/进程管理器，不涉及第三方账户/账单；构造这两个 provider
对象本身不发生真实 IO，但它们的运行期方法（`install`/`reload`/
`xray_test`/`health` 等）会真实访问本机文件系统、跑 subprocess、探测
socket，甚至（Mihomo）发起本地 HTTP 请求带一个真实的 API secret；这两点
第一版都没分清楚（第一版先说"只被 registry 的 mock 门挡住、没有其它
技术障碍"，第二版改口说"构造本身就和 registry 的无 IO 承诺冲突"，两个
说法都不准确，细节和改正见下面"构造 vs. 组装 vs. 运行期"一节）：**
- `gateway/xray_file.py`——`XrayFileProvider`（九步安全重载）
- `forwarder/mihomo.py`——`MihomoForwarderProvider`（文件+热重载+回滚）

修正后的结论没有变得比第一版更简单：B 类确实不涉及第三方账户/账单，比
A 类的"真实外部账户副作用"风险类别不同；但"不涉及外部账户"不等于"没有
真实副作用""接入工作量小"，也不等于"构造这些 provider 对象会自动破坏
`build_registry()` 的无 IO 承诺"——三件事分开评估，见下一节。

### 逐项盘点表

| Provider 分类 | 真实实现文件 | 完整度 | 配置入口 | 凭据来源 | 现有测试覆盖 | 目前是否被任何代码路径实例化 |
|---|---|---|---|---|---|---|
| egress | `egress/webshare.py`（`WebshareProvider`） | **部分**：`list_endpoints()`/`capacity()`/`create_tenant()`/`update_tenant_quota()`/`replace_endpoint()` 都会发真实请求，但响应体全部被丢弃，返回写死的空/零值 DTO（`list_endpoints()` 恒返回 `[]`，`capacity()` 恒返回全零）；`get_tenant_usage()`/`get_credentials()` 直接 `raise NotImplementedError`。写操作有明确的路径白名单守卫（`_guard`），永久拒绝任何 purchase/renew/payment/billing 路径。 | `egress_provider` 只接受 `"mock"` | **没有 `webshare_api_key`（或同类）配置项**——`Settings` 里完全没有为 `WebshareProvider.__init__` 的 `api_key` 参数留位置 | `test_webshare_guard.py`、`test_webshare_procurement.py`：覆盖写操作白名单、限速/429 退避、procurement 只读适配器；**没有测试覆盖真实响应体到 DTO 的映射**（因为这部分还没写） | 否——只在测试文件里手动构造，`registry.py`/其余应用代码里从未 `import WebshareProvider` |
| accounting | **无**（`accounting/` 目录只有 `mock.py`） | 不适用——**这个 provider 类别完全没有真实实现**，连一个空壳文件都没有 | `accounting_provider` 只接受 `"mock"`；但 `Settings` 里已经有一整组 `marzban_*` 字段（`marzban_base_url`/`marzban_admin_username`/`marzban_admin_password`/`marzban_default_protocol`/`marzban_default_inbounds_json`/`marzban_verify_tls`），`validate_runtime_safety()` 甚至已经在校验"生产环境选了 marzban 但配置还是默认值就报错"——**配置层已经为一个不存在的实现做好了校验，这是意外发现，见下方** | 同上，字段已存在但没有对应实现读取它们 | 无——没有实现就没有测试 | 否——不存在这个类 |
| gateway | `gateway/xray_file.py`（`XrayFileProvider`） | **部分**（第一版"较完整"的判断已修正）：九步安全重载序列、校验失败/重载失败/健康检查失败的回滚路径都已实现，没有发现 `NotImplementedError`/占位注释；但**独立审查核实出一个真实的自我校验缺口**：`render()` 产出的候选配置不含 `inbounds` 字段，在真实环境（当前配置有 inbound 客户端）下会被自己的 `_preservation_errors()` 判定为"移除了现有 inbound 客户端"而拒绝——见下方"具体实现缺口"一节，这个问题不影响现有测试是因为测试固件两边都没有 `inbounds` | `gateway_provider` 只接受 `"mock"` | 不涉及第三方账户；构造 `XrayFileProvider` 本身只是保存一个 `XrayRuntime` 对象引用，不发生真实 IO（见下方"构造 vs. 组装 vs. 运行期"一节），但要接进 `build_registry()` 仍需要先决定 `Settings` 怎么提供 `config_path`/`backup_dir` 等真实路径值 | `test_safe_reload.py`：9 个测试，覆盖校验失败提前拦截、重载失败完整回滚、缺失路由/用户/出站的各种拒绝场景、Dockerfile 资产完整性；**没有测试覆盖"当前配置存在真实 inbound 客户端"这个场景**，这正是上面缺口没被测试暴露的原因 | 否——`registry.py` 从未 import，只有测试文件直接构造 |
| forwarder | `forwarder/mihomo.py`（`MihomoForwarderProvider`） | **部分**（第一版"较完整"的判断已修正）：文件安装+热重载+失败回滚，没有发现 `NotImplementedError`/占位注释；但**独立审查核实出一个真实的功能缺口**：`health()` 硬编码恒返回健康，`apply()` 从未调用它做重载后校验，成功判定完全依赖"`install()`/`reload()` 没抛异常"——见下方"具体实现缺口"一节 | `forwarder_provider` 只接受 `"mock"` | 不涉及第三方账户；构造 `MihomoForwarderProvider` 本身只是保存 `render_document`/`MihomoRuntime` 引用，不发生真实 IO；但其本机实现 `LocalMihomoRuntime` 的字段里有一个真实凭据 `api_secret`（本机 Mihomo API 的 Bearer token），且现有的 `local_runtime_from_env()` 工厂函数在组装期就会读 env、解析路径、校验这个凭据非空（见下方"构造 vs. 组装 vs. 运行期"一节）——这是范围在本机 API 而非第三方账户的真实凭据 | `test_db_adapters.py::test_mihomo_render_failure_restores_exact_pre_operation_state`——**测试描述已改正**：这个测试实际模拟的是 render/install 之后的**重载（reload）失败**并验证精确回滚，不是 render 本身失败（第一版盘点这里的描述不准确，已修正）；**没有测试覆盖 `health()`/重载后校验缺失这个问题** | 否——`registry.py` 从未 import；`MihomoForwarderProvider` 类定义本身是唯一"看到这个类名"的应用代码位置 |
| transport | `transport/subscription.py`（`SubscriptionTransportProvider`） | 未发现 `NotImplementedError`/占位注释（未逐行审计到函数级） | `transport_provider_mode` 只接受 `"mock"` | **需要外部网络访问且涉及敏感信息**：`sync_nodes()` 对订阅 URL（通常带私有 token）发起真实 `httpx` GET 请求，并可选写本地缓存文件——见上面"C 类"说明，第一版把它错误归类为"不需要外部网络凭据" | `test_transport_provider.py`：覆盖订阅解析（Clash YAML / base64 URI 列表）、凭据不泄漏到 metadata、mock transport 下的 `sync_nodes()` 刷新流程 | 否——`registry.py` 从未 import |
| payment / notify / email / captcha / storage | 无 | 不适用——这五类目前都只有 mock/noop 实现，没有任何真实实现文件或占位文件 | 各自只接受 `"mock"`/`"noop"` | 不适用 | 不适用 | 不适用 |

### `XrayFileProvider` / `MihomoForwarderProvider` 的真实契约：构造 vs. 组装 vs. 运行期

**这一节是第二次修正版**——上一版说这两个 provider"只被 `registry.py`
的 mock 门挡住，没有其它技术障碍"被 Work 审查指出不成立，改正后写成了
"这两个 provider 的构造直接和 `build_registry()`'无网络/无文件系统/无
shell'的承诺冲突"；这一版结论又被 Work 的独立审查指出**把"构造"
（construction）和"运行期方法调用"（operation-time）这两件不同的事混为
一谈**——逐行核实源码后确认这条审查意见是对的，再次改正。要把这件事
说准确，必须分成三层，而不是笼统地说"构造需要 XX，所以和 registry 的
承诺冲突"：

**第一层：构造输入（construction inputs）——只是对象和参数，本身不是 IO**

- `XrayFileProvider.__init__(runtime, *, disable_user=..., alert=...,
  audit=...)`（`xray_file.py:48-59`）：函数体只有五行属性赋值
  （`self._runtime = runtime` 等），没有任何文件系统/网络/subprocess
  调用。
- `MihomoForwarderProvider.__init__(render_document, runtime)`
  （`mihomo.py:102-104`）：同样只有两行属性赋值。
- `LocalXrayRuntime`（`xray_file.py:250-256`）和 `LocalMihomoRuntime`
  （`mihomo.py:44-50`）都是 `@dataclass(slots=True)`，构造时只是把
  `config_path`/`backup_dir`/`api_url`/`api_secret` 等值赋给字段，
  **dataclass 字段赋值本身不访问文件系统、不发网络请求、不 fork
  subprocess**。

也就是说，只要调用方能拿到这些构造参数（路径字符串、URL、secret 值），
单纯"构造出一个 `XrayFileProvider`/`MihomoForwarderProvider` 实例"这件事
本身是纯 Python 对象组装，**和 `build_registry()` 的"无网络/Docker/
文件系统/shell"承诺并不冲突**。上一版"构造直接与该承诺冲突"的说法不准确，
已经改正。

**第二层：组装期行为（assembly-time behavior）——只有一个具体例子，且
只存在于 Mihomo 侧**

- `local_runtime_from_env()`（`mihomo.py:141-155`）会：①读取
  `MIHOMO_CONFIG_PATH`/`MIHOMO_BACKUP_DIR`/`MIHOMO_API_URL`/
  `MIHOMO_API_SECRET`/`MIHOMO_RUNTIME_CONFIG_PATH` 等环境变量；②对路径
  调用 `.resolve()`——这是真实的文件系统调用（会解析符号链接，做
  `realpath`/`readlink` 系统调用，即使目标文件不存在也会访问文件系统）；
  ③校验 `MIHOMO_API_SECRET` 非空，为空则 `raise MihomoRuntimeError`。
  这是一个真实存在的"组装期读 env + 文件系统路径解析 + 配置校验"的例子，
  但它是一个独立的工厂函数，不是 `LocalMihomoRuntime.__init__` 本身的
  行为——调用方完全可以绕过 `local_runtime_from_env()`，直接用已经解析好
  的值构造 `LocalMihomoRuntime(...)`，这样就不会触发这层组装期文件系统
  访问。
- Xray 侧**目前没有对应的 `local_runtime_from_env()` 或任何等价工厂
  函数**——`xray_file.py` 里没有 `from_env`/`local_runtime_from_env`
  这类构造入口，`LocalXrayRuntime` 只能通过直接传参数构造。

**第三层：运行期副作用（operation-time side effects）——只有真正调用这些
方法时才发生，和"构造出实例"是两回事**

- `LocalXrayRuntime`：`backup()`/`current()`（读文件）、`xray_test()`
  （`subprocess.run` 真的跑一次 `xray run -test`）、`install()`（写文件）、
  `reload()`（`subprocess.run` 执行 `systemctl reload xray`）、`health()`
  （`socket.create_connection` 探测本机 8443 端口）、`restore()`（读写
  文件）——这些方法调用才是真实的文件系统/subprocess/socket 访问，不是
  `LocalXrayRuntime(...)` 这行构造代码本身。
- `LocalMihomoRuntime`：`backup()`/`install()`/`restore()`（文件读写）、
  `reload()`（对 `api_url` 发起带 `Authorization: Bearer {api_secret}`
  的真实 HTTP PUT 请求）——同样只在方法被调用时才发生。

**修正后的结论**：不能说"这两个 provider 的构造需要真实文件系统/shell
访问，所以和 `build_registry()` 的承诺冲突"；准确的说法是——如果
`build_registry()` 只是把已经拿到的路径/URL/secret 值传给
`XrayFileProvider(LocalXrayRuntime(...))`/
`MihomoForwarderProvider(..., LocalMihomoRuntime(...))` 的构造函数，
这件事本身不会产生网络/文件系统/subprocess 访问，不违反
`build_registry()` 现有的文档字符串承诺；真正需要单独评估、且目前唯一
已知会在组装期做真实文件系统访问的，是 `local_runtime_from_env()`
这一个具体的工厂函数（如果阶段二决定继续用它来供给
`LocalMihomoRuntime`）。**本阶段不预先替阶段二决定**"必须用惰性构造""
必须把 runtime 挪到 registry 外部用依赖注入容器完成"之类的架构方案——
上一版这么写是在没有区分这三层之前就跳到了结论；现在看，一旦分清"构造
输入 vs. 组装期行为 vs. 运行期副作用"，`registry.py` 完全有可能在保持
"构造阶段不做真实 IO"这条更精确的边界的前提下，直接实例化这两个
provider（只要它拿到的是已经解析好的配置值，而不是自己在组装期调用
`local_runtime_from_env()` 这类会做文件系统/校验的工厂）；阶段二具体
怎么设计 `Settings` 传参、要不要保留/替换 `local_runtime_from_env()`
这类工厂，留给阶段二自己决定。

需要新增哪些 `Settings` 字段来传递
`config_path`/`backup_dir`/`api_url`/`api_secret` 等路径和凭据，以及
这些新字段（尤其 `api_secret`）要按什么规则管理（参照 `AGENTS.md` 的
凭据持有范围规则，不能直接写进 `.env`/代码），仍然是阶段二需要单独设计
的问题，这里只记录问题存在，不做决定。

### `XrayFileProvider` / `MihomoForwarderProvider` 的具体实现缺口（独立审查第二轮，已核实）

第一版和第一次修正都只停留在"构造依赖"层面，没有发现下面这两个更具体
的实现缺口。Work 的第二条独立审查评论指出后，逐行读源码核实，确认都是
真实存在的问题，不是构造参数缺失那种"接上配置就能用"的问题：

- **`XrayFileProvider.render()` 生成的候选配置完全没有 `inbounds` 字段**
  （`xray_file.py:61-81`，`content` 只有 `routing`/`outbounds` 两个 key）。
  而它自己的 `_preservation_errors()` 校验（`apply()` 在真实重载前后都会
  调用，`xray_file.py:113-117`）会用 `_inbound_clients()` 去读
  `current.get("inbounds")` 和 `candidate.get("inbounds")` 分别算出两组
  客户端集合再取差集；`_inbound_clients()` 对不是 list 的 `inbounds`
  直接返回空集（`xray_file.py:206-207`）。也就是说，只要真实运行环境里
  当前配置（`self._runtime.current()`）包含任何 inbound 客户端，
  `render()` 产出的候选配置就会被自己的校验判定为"移除了现有 inbound
  客户端"（`errors.append("candidate removes existing inbound clients")`），
  从而在 `validate()` 阶段直接拒绝，或者在 `apply()` 里通过了 `validate()`
  之后的重载后二次校验时触发完整回滚。现有测试（`test_safe_reload.py`）
  之所以没有暴露这个问题，是因为测试固件里 `current()` 返回的基线配置本身
  也没有 `inbounds` 字段（两边都是空集，差集为空，测试通不出这个缺口）。
  这是一个真实的、会在有真实 inbound 客户端的环境下必现的自我校验失败，
  必须先修，不能指望"先接进 registry 再说"。

  **修复方向的一处更正（独立审查第三轮，已核实）**：上一版这里写的其中
  一个候选修复方向——"`render()` 补上从**现有配置**读取并保留
  `inbounds`"——**核实后确认违反 `AGENTS.md` 铁律第 1 条**（"配置渲染
  一律从数据库全量生成，禁止增量拼接"）：如果 `render()` 为了不删除
  `self._runtime.current()` 里已有的 inbound 客户端，转而去读当前运行时
  文件、把里面的内容原样搬进候选配置，运行时文件就变成了和数据库并列的
  第二数据源，这正是铁律禁止的"增量拼接"。真正合规的方向只有一个：
  **候选配置必须完全由数据库表达的期望态一次性生成，不得读取或拼接当前
  运行时文件的任何内容**。而 `backend/app/providers/base.py` 里
  `DesiredRoutingState`（`render()` 的输入类型）目前**只有
  `user_routes: Mapping[str, str]` 和 `outbound_tags: tuple[str, ...]`
  两个字段，完全没有 `inbounds`/`clients` 的位置**——也就是说，这不是
  "`render()` 忘了读一下 inbounds"这么简单，而是当前的期望态 DTO 本身
  就没有能力表达 inbounds/clients 应该是什么样子。阶段二要解决这个问题，
  第一步必须是回答"数据库模型和查询能不能完整表达 Xray 需要的
  inbounds/clients/routing/outbounds"，答不出来就要先补这个契约，而不是
  假设"从当前文件复制缺失部分"是一个可用的临时方案。
- **`MihomoForwarderProvider.health()` 是硬编码的
  `return HealthReport(True, {"provider": "mihomo"})`**（`mihomo.py:137-138`），
  不读取 `self._runtime` 的任何真实状态；`apply()`（`mihomo.py:120-135`）
  的成功判定完全依赖"`install()`/`reload()` 没有抛异常"，从未调用
  `health()` 做重载后的真实健康验证——对比 `XrayFileProvider.apply()`
  会在重载后调用 `self._runtime.health()` 并把结果纳入回滚判断
  （`xray_file.py:112-118`），`MihomoForwarderProvider` 完全没有等价的
  重载后校验路径。这意味着即使 Mihomo 进程重载后配置没有生效或已经
  异常退出，只要 `install()`/`reload()` 调用本身没抛异常，`apply()`
  仍然会返回 `ApplyResult(True, ...)`，把这次操作视为成功。这是接入前
  必须先补的一个真实功能缺口，不是文档层面的描述问题。

### Marzban 现状：配置层承认一个 registry 无法构造的实现，不是"死代码"

第一版盘点在这里的结论是"这段校验代码永远不会被真正走到"，**这个结论
的执行顺序搞反了，Work 审查指出后核实确认是错的，改正如下**。

`Settings.validate_runtime_safety()` 里有这一段：

```python
if self.app_env == "production" and self.accounting_provider == "marzban":
    if "example.invalid" in self.marzban_base_url:
        raise ValueError("Production MARZBAN_BASE_URL must be configured")
    if "CHANGE_ME" in {self.marzban_admin_username, self.marzban_admin_password}:
        raise ValueError("Production Marzban credentials must be configured")
```

`Settings.from_env()` 的最后一步就是调用 `self.validate_runtime_safety()`
再返回——也就是说，只要有人真的把 `ACCOUNTING_PROVIDER=marzban`、
`APP_ENV=production` 这两个环境变量设置好（不管配置的凭据是不是默认值），
调用 `get_settings()`/`Settings.from_env()` 这一步本身就会先执行到这段
校验：默认凭据会在这里被真的挡下来、抛出 `ValueError`；如果凭据不是
默认值，这段校验会通过，但**再往后调用 `build_registry(settings)` 时，
`accounting_provider == "marzban"` 会被 `_unsupported()` 拒绝**——校验
代码是真实可达、真实会执行的，不是"死代码"。

真正的问题是：**配置校验层承认"marzban 是一个合法的
`accounting_provider` 取值"这件事，但 registry 组装层完全没有对应的实现
可以构造**——这是配置层和 registry 层对"合法值"的定义不同步，不是"一段
永远执行不到的校验"。这个不同步本身仍然值得记录和修：如果有人在生产
环境按上面这个字段名配置了非默认的 marzban 凭据（以为这样就能用），
`validate_runtime_safety()` 会放行，但下一步 `build_registry()` 仍然会
直接报错——对配置的人来说，"两层校验说法不一致"本身就是一种误导。
`accounting/` 目录下也确实没有任何名叫 Marzban 的类，这个结论没有变：
接入 Marzban accounting provider 是从零开始写。

### 建议的分阶段接入顺序（本阶段只给建议，不实施）

1. **阶段二（已按独立审查意见调整）：先加固并完成一个本机 provider 的
   接口契约测试，再以显式 opt-in 方式接入 registry；首选候选是
   `gateway/xray_file.py`（`XrayFileProvider`），不是直接接入 registry**
   ——上一版建议是"B 类选一个直接接入 registry"，前提是"没有其它技术
   障碍"；上面"具体实现缺口"一节已经确认这个前提不成立：
   `XrayFileProvider` 在有真实 inbound 客户端时会被自己的校验拒绝，
   `MihomoForwarderProvider` 的 `health()`/`apply()` 没有真正的重载后
   校验。在这些缺口修好、并且有对应的契约测试之前，把它们接进
   `build_registry()` 只会让 registry 组装出一个会在真实环境下自我拒绝
   或误报成功的实例，比继续保持 mock 更危险。

   **为什么首选 Xray 而不是 Mihomo：** 两者都需要先补契约测试，但缺口的
   性质不同。Xray 已经有 `health()` 的真实实现（`socket.create_connection`
   探测本机端口 + `marzban_health()` 回调）、`apply()` 也已经在重载后
   调用 `health()` 并纳入回滚判断（`xray_file.py:112-118`）——它欠缺的
   是"重载前生成的候选配置本身要不要保留 `inbounds`"这一条具体的
   preservation 规则，属于**在一个已经存在的健康校验框架里补一条遗漏的
   校验规则**。Mihomo 则连"重载后到底健不健康"这个最基础的校验都没有
   （`health()` 硬编码恒真、`apply()` 从不调用它），属于**框架本身缺失**
   ——在框架都不存在之前去讨论"候选配置要不要保留什么字段"没有意义。
   因此 Xray 的缺口更收敛、更接近"补一条测试和一段逻辑"，Mihomo 需要先
   把 post-reload health-gate 这个更基础的框架补上，工作量和不确定性都
   更大。所以阶段二**首选 `XrayFileProvider`**；Mihomo 的
   post-reload 健康校验缺口本身记为待办，但不在阶段二第一步的范围内。

   **阶段二第一步必须遵守 `AGENTS.md` 铁律第 1 条（"配置渲染一律从数据库
   全量生成，禁止增量拼接"），这一点在上一版还没写清楚（独立审查第三轮
   指出后核实确认）：** 上一版把 preservation 契约写成"不能删除既有
   inbound 客户端/routing rule/outbound"，这个措辞暗示"拿当前运行时
   文件当基准、候选配置不得比它少东西"，等于把运行时文件当成了跟数据库
   并列的第二数据源，一旦按这个方向实现 `render()`，就会违反铁律第 1 条；
   而且这个措辞本身也有问题：如果数据库期望态里某个用户路由/inbound
   客户端已经被合法删除，"无条件不能比当前配置少东西"这条规则会永远
   挡住这次合法删除，配置永远无法瘦身。

   阶段二的具体步骤（这里只记录 handoff 内容，本 PR 不实现、不改动
   `xray_file.py`、`base.py` 或任何测试文件）：
   ①**先盘点数据库模型/查询和 `DesiredRoutingState` 能不能完整表达
   Xray 需要的全部内容**——目前 `DesiredRoutingState`
   （`backend/app/providers/base.py`）只有 `user_routes` 和
   `outbound_tags` 两个字段，没有 `inbounds`/`clients` 的位置。如果
   数据库/领域模型已经能查出"当前应该存在哪些 inbound、哪些 client"，
   阶段二第一步是把这些信息补进期望态 DTO，让 `render()` 完全基于这个
   一次性生成的完整期望态产出候选配置（不读、不拼接当前运行时文件）；
   如果数据库/领域模型本身还没有这些概念，第一步就是先记录并设计这个
   缺口和边界，不能假设"从当前文件复制缺失部分"是一个可行的临时方案——
   这一步本身可能已经超出"补一条 preservation 测试"的工作量，需要
   阶段二自己重新评估范围。
   ②**preservation/校验语义要重新定义为"候选及重载后配置与数据库期望态
   一致"，不是"不丢失当前运行时文件里的任何历史对象"**——校验应该保护
   `AGENTS.md` 的安全不变量（`private BLOCK` 规则、`tcp,udp BLOCK` 兜底
   规则等），并确认生效配置确实等于数据库期望态生成的候选配置，而不是
   笼统要求"候选不能比当前配置少任何 routing rule/outbound/inbound
   客户端"；同时要明确定义合法删除/撤销该怎么发生（例如某个客户端在
   数据库里被撤销后，下一次 render 出的候选配置里就应该不再包含它，
   这不应该被 preservation 校验拦下）。候选配置仍然需要能通过
   `xray_test()`（`xray run -test`）边界校验，重载失败或校验失败时仍
   需完整回滚到重载前状态。
   ③契约和数据源设计稳定之后，再以**显式 opt-in**（而不是让它替换 `registry.py`
   现有的默认拒绝分支）的方式接入 registry——具体是新增一个独立的环境
   变量开关还是别的机制，留给阶段二自己设计。
   ④在①②③完成之前，`build_registry()` 不接入 `XrayFileProvider`，
   `forwarder/mihomo.py` 也保持现状不接入。
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

## 阶段二 A：数据库能否作为 Xray desired state 的唯一 source of truth
（2026-09-11，只读设计/契约分析，未改动业务代码；同日第二次修订：独立
审查指出并核实了四处事实错误 + 一处措辞问题，已全部修正，见下方各条
"已更正"标注）

按用户要求，阶段二先只回答一个问题："数据库能不能完整表达应用负责管理的
Xray desired state"，不做 registry wiring、不做 provider 实现、不碰
Xray reload。完整分析和证据记录在新 ADR：
**`docs/80-decisions/ADR-014-xray-desired-state-ownership.md`**——本节
只摘录结论，逐条证据和推导过程见该 ADR，不在这里重复。

### 结论 1：数据库现状 —— `INSUFFICIENT`

逐项缺口（详见 ADR-014"事实一~五"）：

- **inbound/client（UUID、密码、email 等认证材料）没有发现任何生产
  renderer/provider 路径生成或写入它**（措辞已更正：第一版说"全仓库
  搜索 `clients` 零匹配"不准确，`xray_file.py` 的 `_inbound_clients()`
  确实有引用，只是读取/校验，不是生成/写入）；
  `infrastructure/marzban/xray_config.base.json` 里
  `inbounds[0].settings.clients` 写死为空数组，没有任何渲染路径填充它。
  **部署拓扑证据（ADR-014 事实一、二）已经证明 Marzban 容器直接挂载
  本仓库渲染出的同一份 `xray_config.json`，不存在"两个互相独立、看不见
  彼此的 Xray 实例"这种拓扑**；真正仍未确定的问题收窄为"Marzban 进程
  启动后是否/如何动态把自己面板新增的用户写回这份共享文件"——这一点
  属于 Marzban 自身运行时行为，本仓库代码看不到，继续标记
  `UNVERIFIED / DECISION REQUIRED`，不猜测。
- **（已更正）Xray 路由匹配用的 accounting username 已经有明确的持久化
  来源**：`backend/app/api/admin.py::admin_confirm_payment` 把
  `request.username`（`f"sub-{order.id}"`，对 `order.id` 确定性推导）
  写入 `Subscription.accounting_user_id`（`unique` 列）并 commit；
  `GatewayRouteBinding` 通过 `subscription_id` 外键就能关联到这个已经
  持久化的值，**不需要新增字段**。第一版这里"username 没有持久化在任何
  表里""`GatewayRouteBinding` 需要新增字段"的结论是错的，已在 ADR-014
  "事实三"改正，本文档同步删除这个缺口。`GatewayRouteBinding.
  gateway_principal` 本身仍然不是 Xray client 身份（是 Webshare 出口
  租户 ID，审计用途）——这一点第一版是对的，不变。
- **outbound 的连接细节（host/port/protocol/凭据）数据库层面是有的**
  （`EgressEndpoint`/`EgressBinding`/`Secret`，`ops/gateway/
  render_xray_routes.py::render_config()` 已经证明这条数据流跑得通，
  而且这个脚本已经是 `deploy/lib/40_stack_up.sh` 里明确调用的部署步骤，
  不是"推测的独立脚本"），但**目前只被这个部署脚本使用，没有经过
  `GatewayProvider` Protocol/provider 抽象层**——`XrayFileProvider.
  render()`（provider 抽象层的实现）完全没有用到这些信息，只产出没有
  连接细节的空壳 outbound。
- **Reality `privateKey` 和 `shortIds` 都没有持久化位置**（已扩充：
  第一版只记录了 privateKey，`shortIds` 同样从空模板生成、从不写回，
  一并记录）：`_reality_settings()` 每次从静态模板文件生成新的
  privateKey/shortIds，理论上每次渲染都可能轮换、破坏存量客户连接
  （ADR-014"事实五"，这是静态阅读代码即可确认的缺陷，不是 UNVERIFIED）。
- **（已更正）`TrafficRule` 不是死代码**：`ops/forwarder/
  render_mihomo_config.py` 确实 import 并查询 `TrafficRule`（enabled
  过滤），`_render_traffic_rules()` 把它们渲染成 Mihomo 的 `rules`
  字段，`build_config()` 使用这个结果——这是 Mihomo/forwarder 的真实
  动态流量策略数据源。第一版"零代码引用/dead code/建议废弃"的结论是错
  的（当时没有读 `ops/forwarder/render_mihomo_config.py`），已在
  ADR-014"事实四"改正。**`TrafficRule` 和 Xray 的
  `DesiredRoutingState.user_routes` 仍然无关**——这两点可以同时成立：
  它是 Mihomo 侧真实在用的数据源，但不是 Xray 侧的数据源，不应假设为
  路由数据源用于本阶段的 Xray 期望态设计。

### 结论 2：`DesiredRoutingState` 现状 —— `INSUFFICIENT`

`backend/app/providers/base.py::DesiredRoutingState` 只有
`user_routes: Mapping[str, str]` 和 `outbound_tags: tuple[str, ...]`
两个字段（这一条结论不受本轮事实修正影响）：

- 没有 `inbounds`/`clients` 的位置（是否需要补，取决于上面结论 1 里
  那个收窄后的 `UNVERIFIED` 问题——Marzban 是否/如何动态管理这部分）。
- **`outbound_tags` 只是字符串 tag 名字，不能表达 host/port/protocol/
  凭据**——而 `ops/gateway/render_xray_routes.py` 已经证明这些字段是
  生成真实可用 outbound 的必需项。这个缺口比"缺 inbounds"更基础：即使
  inbound 问题最终确认不属于本应用职责，`DesiredRoutingState` 现状仍然
  不足以生成一个连得上真实 socks 出口的 outbound 定义。

### 结论 3：Phase 2B 需要的改动类型

| 类型 | 是否需要 | 说明 |
|---|---|---|
| DTO change（`DesiredRoutingState`/新增 DTO） | **需要** | 至少要能表达 outbound 连接细节（host/port/protocol/凭据引用）+ 路由匹配键（`Subscription.accounting_user_id`，已确认有持久化来源，不是新缺口）；inbound/client 是否需要取决于 ADR-014 收窄后的 `UNVERIFIED` 问题的答案 |
| DB model/schema change | **可能需要**，具体列留给 Phase 2B（**已删除"`GatewayRouteBinding` 补 username 字段"这一条候选**——事实三核实后确认不需要） | 候选：Reality `privateKey`/`shortIds` 的持久化位置（新列或 `Secret` 表条目）；如果确认 Marzban 不会自己动态管理 client，需要新表存 client 认证材料 |
| query/adapter change | **需要** | 期望态查询逻辑需要参照 `render_xray_routes.py::_active_routes()` 已经跑通的 JOIN 逻辑，而不是从零设计；`SqlAlchemyProvisioningState.desired_routing_state()` 现有实现需要重新评估是否要挪到这条新数据流里 |
| Xray renderer change | **需要** | `XrayFileProvider.render()` 需要能产出完整 outbound（不只是 tag），且要先解决 inbounds 是否属于它职责范围这个前提问题 |
| validation/preservation change | **需要** | 按 ADR-014 的"合法删除语义"重新定义：校验候选与"本次从数据库计算出的期望态"一致 + 安全不变量成立，不是"候选是不是运行时当前配置的超集" |

### Phase 2B 最小范围（严格收敛，不是"实现完整真实 Xray Provider"）

**补齐数据库 → desired-state 的最小契约 + Xray renderer/校验护栏**，
具体拆解为（Phase 2B 自己的 PR 里再细化，这里只定边界；本轮修正后步骤
数量不变，内容按上面结论 1/3 的更正同步调整）：

1. 先解决 ADR-014 收窄后的 `UNVERIFIED / DECISION REQUIRED`（Marzban
   是否/如何动态管理共享 `xray_config.json` 里的 client；Marzban 对应
   accounting 还是 transport）——这两点任一没有明确答案，后面的 DTO/
   schema 设计都是在猜。这一步本身可能需要跟运维/产品确认真实部署拓扑
   或 Marzban 自身行为，不是纯代码分析能回答的。（route-match 用户名
   持久化问题已在本轮解决，不再是阻塞项。）
2. 在①有答案之后，扩展期望态 DTO，让它至少能表达完整 outbound（参照
   `render_xray_routes.py` 已验证的字段形状）和路由匹配键
   （`Subscription.accounting_user_id`）；按①的答案决定要不要加
   inbound/client 的位置。
3. 为 Reality `privateKey`/`shortIds` 和（如果需要）client 认证材料
   设计持久化位置，走 `Secret` 表的加密存储机制。
4. 重写 preservation/校验逻辑为"候选与本次期望态一致 + 安全不变量"，
   替换掉现在"候选不能比运行时当前配置少任何东西"的旧语义。
5. 为①~④分别补齐契约测试，覆盖合法删除、Reality 材料稳定性、
   outbound 凭据正确性等场景。
6. **不在 Phase 2B 做**：`registry.py` wiring、生产环境启用、Xray
   reload——这些留给 Phase 2C。

### Phase 2C（本阶段不讨论细节，只记录顺序）

只有 Phase 2B 完成并独立审查通过之后，才讨论**显式 opt-in 的 Xray
registry wiring**。2A/2B/2C 不合并成一个阶段。

### 本阶段（2A）验收

- 新增 `docs/80-decisions/ADR-014-xray-desired-state-ownership.md`，
  修改本文档和 `docs/10-deploy-new-server.md`（仅修正与本轮新事实直接
  冲突的段落），`backend/app/**`/`infrastructure/**`/`frontend/**` 等
  代码目录零改动。
- 没有调用任何真实 provider 的网络请求，没有读取生产 Xray 配置、生产
  `.env`、`/etc/lingsway/*.conf`，没有使用任何真实 secret/token。
- 没有 reload 或调用 Xray/Mihomo，没有修改生产数据库。
- 数据库现状：`INSUFFICIENT`；`DesiredRoutingState`：`INSUFFICIENT`
  ——详细缺口清单见上方结论 1/2 和 ADR-014（route-match 用户名持久化
  问题已解决，不再计入缺口）。
- 下一步（Phase 2B）需要用户先确认 ADR-014 里收窄后的两个 `UNVERIFIED`
  问题的真实答案（可能需要跟运维或产品确认部署拓扑/Marzban 行为），再
  决定具体的 DTO/schema 改动范围。
