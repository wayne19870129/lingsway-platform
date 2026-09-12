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
| gateway | `gateway/xray_file.py`（`XrayFileProvider`） | **部分**（第一版"较完整"的判断已修正；**本轮再次更正**：此前"九步安全重载序列、校验失败/重载失败/健康检查失败的回滚路径都已实现"这句话过于笼统，与本 TASK 后面 Phase 2B0 小节已确认的事实冲突，改为精确描述——validation failure 会在 `install()` 之前停止，不产生任何磁盘/运行时改动；`install()`+第一次 `reload()` 成功之后，如果 post-reload health/preservation 检查**以返回值形式报告**不健康，会执行 `restore(backup)`+第二次 `reload()`+health 复核（这一条回滚路径确实已实现）；**但 `install()` 抛出异常、或第一次 `reload()` 抛出异常（`LocalXrayRuntime.reload()` 用 `subprocess.run(..., check=True)`，命令失败会直接抛异常）目前都没有 try/except 兜底，异常会绕过 `restore(backup)` 直接从 `apply()` 传播出去**——这两项已经在后续"阶段二 B0"小节记录为 Phase 2B 必须加固的安全缺口，没有发现 `NotImplementedError`/占位注释）；此外**独立审查核实出另一个真实的自我校验缺口**：`render()` 产出的候选配置不含 `inbounds` 字段，在真实环境（当前配置有 inbound 客户端）下会被自己的 `_preservation_errors()` 判定为"移除了现有 inbound 客户端"而拒绝——见下方"具体实现缺口"一节，这个问题不影响现有测试是因为测试固件两边都没有 `inbounds` | `gateway_provider` 只接受 `"mock"` | 不涉及第三方账户；构造 `XrayFileProvider` 本身只是保存一个 `XrayRuntime` 对象引用，不发生真实 IO（见下方"构造 vs. 组装 vs. 运行期"一节），但要接进 `build_registry()` 仍需要先决定 `Settings` 怎么提供 `config_path`/`backup_dir` 等真实路径值 | `test_safe_reload.py`：9 个测试，覆盖校验失败提前拦截、**post-reload health 检查以返回值形式报告不健康时的完整 rollback sequence**（`restore`+第二次 `reload`+health 复核）、缺失路由/用户/出站的各种拒绝场景、Dockerfile 资产完整性；**本轮更正**：这里此前笼统写"重载失败完整回滚"，但现有测试并不覆盖"`install()` 抛异常"或"第一次 `reload()` 抛异常"这两种场景——**当前没有这两类测试**，rollback `restore()` 自身抛异常、以及 rollback 第二次 `reload()`/健康复核失败这两类也仍待补齐；完整的四类未来护栏测试要求见下方"阶段二 B0"小节，此处不重复定义一套不同的规则；另外**没有测试覆盖"当前配置存在真实 inbound 客户端"这个场景**，这正是上面缺口没被测试暴露的原因 | 否——`registry.py` 从未 import，只有测试文件直接构造 |
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
审查指出并核实了四处事实错误 + 一处措辞问题，已全部修正；同日第三次
修订：独立审查又指出并核实了三处 Major——铁律第 1 条与静态模板边界不清、
`docs/10` 对 Reality 来源的描述仍不准确、`GatewayRouteBinding.
gateway_principal` 设计意图判断错误 + `Subscription.accounting_user_id`
持久化时机晚于渲染发生——已全部修正；同日第四次修订：独立审查指出 A2
把 Reality `dest`/`serverNames` 留成"暂不强制归类"这个中间状态本身和
A2 自己的通用规则矛盾，已收敛为明确结论（运维部署配置，唯一来源为
环境变量/`Settings`，不再允许从模板读取），完整证据见 ADR-014 的"A2"节
和"事实三"节）

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
- **（已更正，第三次修订又补充了两处遗漏）Xray 路由匹配用的 accounting
  username 有一个持久化来源，但持久化时机和字段一致性都还有缺口**：
  `backend/app/api/admin.py::admin_confirm_payment` 把
  `request.username`（`f"sub-{order.id}"`，对 `order.id` 确定性推导）
  写入 `Subscription.accounting_user_id`（`unique` 列）并 commit——这个
  canonical source 确实存在，第一版"username 没有持久化在任何表里"的
  结论是错的，`GatewayRouteBinding` 也确实不需要新增字段来存它。但
  **本轮独立审查又指出并核实了两处遗漏**：①`admin_confirm_payment` 是
  在整个九步开通编排（`confirm_payment_and_provision`）全部跑完之后才
  写入 `accounting_user_id`，而 `desired_routing_state()`（生成路由
  期望态的地方）是在编排的第 7 步 `APPLY_GATEWAY` 执行的——**渲染发生
  的时刻，这一列可能还是 `NULL`**，Phase 2B 不能假设它已经就绪；
  ②`GatewayRouteBinding.gateway_principal` 的**模型 docstring**（"Maps
  one accounting user principal..."）和**集成测试**
  （`ensure_gateway_route_binding("marzban-user-1", dto)`）都期望这里
  存的是 accounting principal，但**当前生产 writer**
  （`desired_routing_state()`）实际写入的是 `tenant.tenant_id`（Webshare
  出口租户 ID）——第一版把"当前 writer 的行为"当成"这个字段的设计意图"
  来描述是错的，这是一处需要 Phase 2B 收敛的**实现不一致**，不是一个
  已经想清楚的审计字段设计。详见 ADR-014"事实三"完整证据和候选 A/B/C。
- **outbound 的连接细节（host/port/protocol/凭据）数据库层面是有的**
  （`EgressEndpoint`/`EgressBinding`/`Secret`，`ops/gateway/
  render_xray_routes.py::render_config()` 已经证明这条数据流跑得通，
  而且这个脚本已经是 `deploy/lib/40_stack_up.sh` 里明确调用的部署步骤，
  不是"推测的独立脚本"），但**目前只被这个部署脚本使用，没有经过
  `GatewayProvider` Protocol/provider 抽象层**——`XrayFileProvider.
  render()`（provider 抽象层的实现）完全没有用到这些信息，只产出没有
  连接细节的空壳 outbound。
- **Reality `privateKey` 和 `shortIds` 都没有持久化位置，且按
  `AGENTS.md` 铁律第 1 条的严格解读，这是当前不合规的现状，需要 Phase
  2B 挪进数据库/`Secret`**（已扩充两次：第一版只记录了 privateKey；第二
  次修订补上 shortIds；第三次修订新增 ADR-014"A2"节区分"可变 desired
  state"/"代码级固定安全不变量"/"静态模板"三类概念）：`_reality_settings()`
  每次从静态模板文件生成新的 privateKey/shortIds，理论上每次渲染都可能
  轮换、破坏存量客户连接（ADR-014"事实五"，这是静态阅读代码即可确认的
  缺陷，不是 UNVERIFIED）。
- **（本轮第四次修订新增）Reality `dest`/`serverNames` 的归属已收敛为
  第四类：运维部署配置，唯一来源应为环境变量/`Settings`，不再允许从
  模板文件读取**——第三次修订曾把这两个字段用"暂不强制归类"搁置，独立
  审查指出这和 A2 自己"凡是会因部署变化而影响候选配置的内容都必须有
  canonical source"的通用规则自相矛盾。核实判据：`dest`/`serverNames`
  是运维手动选定的 Reality 伪装身份参数（伪装成哪个域名/SNI），不随
  客户下单/退订变化，性质上和 `protocol`/`listen`/`port`、以及本仓库
  已有的 `SITE_DOMAIN`/`API_DOMAIN`（ADR-006）一致——都是"运维经环境
  变量配置、不进数据库"的先例；和 `privateKey`/`shortIds`（系统生成、
  需要追踪轮换）的区别是它们不是系统生成的、不需要轮换/审计机制。当前
  实现"模板优先、env 兜底"两个来源并存本身是一个真实风险（模板和 env
  值不一致时会悄悄用错值），Phase 2B 需要把来源收窄为只读环境变量，
  移除模板读取路径。
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
| DTO change（`DesiredRoutingState`/新增 DTO） | **需要** | 至少要能表达 outbound 连接细节（host/port/protocol/凭据引用）+ 路由匹配键；inbound/client 是否需要取决于 ADR-014 收窄后的 `UNVERIFIED` 问题的答案 |
| DB model/schema change | **可能需要**，具体列留给 Phase 2B（**"`GatewayRouteBinding` 补 username 字段"这一条候选仍然不需要**；**Reality `dest`/`serverNames` 明确不需要 schema，只需环境变量，见下**） | 候选：Reality `privateKey`/`shortIds` 的持久化位置（新列或 `Secret` 表条目，方向已由 ADR-014"A2"确定，不再是 UNVERIFIED）；如果确认 Marzban 不会自己动态管理 client，需要新表存 client 认证材料 |
| 编排/时序 change（**本轮新增，不是 schema，是代码/编排层面**） | **需要** | `gateway_principal` 当前写入值和模型/测试契约期望值不一致，`accounting_user_id` 持久化时机晚于 `APPLY_GATEWAY` 渲染时刻——Phase 2B 必须先在 ADR-014 事实三的候选 A/B/C 中选定收敛方向，这不是"顺便决定的细节" |
| query/adapter change | **需要** | 期望态查询逻辑需要参照 `render_xray_routes.py::_active_routes()` 已经跑通的 JOIN 逻辑，而不是从零设计；`SqlAlchemyProvisioningState.desired_routing_state()` 现有实现需要重新评估是否要挪到这条新数据流里 |
| Xray renderer change | **需要** | `XrayFileProvider.render()` 需要能产出完整 outbound（不只是 tag），且要先解决 inbounds 是否属于它职责范围这个前提问题 |
| validation/preservation change | **需要** | 按 ADR-014 的"合法删除语义"重新定义：校验候选与"本次从数据库计算出的期望态"一致 + 安全不变量成立，不是"候选是不是运行时当前配置的超集" |

### Phase 2B 最小范围（严格收敛，不是"实现完整真实 Xray Provider"）

**补齐数据库 → desired-state 的最小契约 + Xray renderer/校验护栏**，
具体拆解为（Phase 2B 自己的 PR 里再细化，这里只定边界；本轮修正后步骤
数量不变，内容按上面结论 1/3 的更正同步调整，新增①.5 这一步）：

1. 先解决 ADR-014 收窄后的 `UNVERIFIED / DECISION REQUIRED`（Marzban
   是否/如何动态管理共享 `xray_config.json` 里的 client；Marzban 对应
   accounting 还是 transport）——这两点任一没有明确答案，后面的 DTO/
   schema 设计都是在猜。这一步本身可能需要跟运维/产品确认真实部署拓扑
   或 Marzban 自身行为，不是纯代码分析能回答的。
1.5.（**本轮新增**）在 ADR-014 事实三的候选 A/B/C 中选定
   `gateway_principal`/`accounting_user_id` 的收敛方向，解决"字段写入值
   和契约不一致"+"持久化时机晚于渲染"这两个问题——这一步和①同样是
   DTO/schema 设计能不能开始的前提，不能跳过直接进入②。
2. 在①、①.5 有答案之后，扩展期望态 DTO，让它至少能表达完整 outbound
   （参照 `render_xray_routes.py` 已验证的字段形状）和路由匹配键；按
   ①的答案决定要不要加 inbound/client 的位置。
3. 为 Reality `privateKey`/`shortIds` 和（如果需要）client 认证材料
   设计持久化位置，走 `Secret` 表的加密存储机制（方向已定，见 ADR-014
   "A2"）；同时把 `render_xray_routes.py` 读取 `dest`/`serverNames` 的
   逻辑从"模板优先、env 兜底"改成"只读环境变量/`Settings`"，移除模板
   作为这两个字段的来源（方向已定，见 ADR-014"A2"第 4 类，不需要
   schema 改动）。
4. 重写 preservation/校验逻辑为"候选与本次期望态一致 + 安全不变量"，
   替换掉现在"候选不能比运行时当前配置少任何东西"的旧语义。
5. 为①~④分别补齐契约测试，覆盖合法删除、Reality 材料稳定性、
   outbound 凭据正确性、`gateway_principal`/`accounting_user_id` 收敛
   方向等场景。
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
  ——详细缺口清单见上方结论 1/2 和 ADR-014（route-match 用户名的
  canonical source 已确认存在，但持久化时机、`gateway_principal` 字段
  一致性、Reality 材料是否允许留在静态模板这三点在本轮独立审查中被
  重新核实并更正，具体见 ADR-014"事实三"和"A2"）。
- 下一步（Phase 2B）需要先在 ADR-014 事实三的候选 A/B/C 中选定
  `gateway_principal`/`accounting_user_id` 收敛方向，并确认 ADR-014
  收窄后的两个 `UNVERIFIED` 问题的真实答案（可能需要跟运维或产品确认
  部署拓扑/Marzban 行为），再决定具体的 DTO/schema 改动范围。

## 阶段二 B0：关闭 Phase 2B 前置决策（2026-09-11，只读研究 +
ADR/TASK 决策，未改动业务代码；同日第二次修订：独立审查指出并核实了
四处事实错误——Marzban 会在特定管理 API 路径下写回共享文件、普通用户
CRUD 走增量 Handler API 而非"每次都整体重建+重启"、Marzban 确实有
真实的远端节点管理能力、候选 A 缺少既有数据收敛方案——已全部修正，
完整证据见 ADR-015 第二次修订。同日第三次修订：独立审查提出三个新
Major——route identity 的目标值本身错了（Xray 实际匹配的是
`{marzban_user_id}.{username}`，不是 accounting username，且 Marzban
公开 API 拿不到这个复合值）、既有数据收敛的 NULL 处理自相矛盾、
drift detection 算法会把正常业务变更误判为漂移——**Decision 3 及既有
数据收敛本轮改为 `BLOCKED`**，Decision 1 的 drift detection 改用三态
模型，Decision 4 矩阵改用 YES/NO/BLOCKED 三态，不再强行给出 YES/NO。
完整证据见 ADR-015 第三次修订，本节同步更新，第一版/第二版内容除被
明确替换的部分外保持不动）

Phase 2A（ADR-014）结尾留下三个前置问题，明确不能跳过直接进
`DesiredRoutingState`/schema/renderer 代码改动。本阶段（2B0）只读研究
公开的 Marzban v0.8.4 源码/文档和仓库既有 provider 契约，逐一关闭这
三个问题，完整证据和推导过程记录在新 ADR：
**`docs/80-decisions/ADR-015-marzban-ownership-and-route-identity.md`**
——本节只摘录结论。

### Decision 1：Marzban v0.8.4 是否管理 Xray inbound clients

**`YES`（已更正，第一版把"确实管理"和"从不写回文件"错误地等同成
同一件事）**——精确写法：Marzban DB 拥有持久化的用户/proxy 状态；
**普通用户 CRUD（创建/修改/禁用）通过 Xray gRPC Handler API
（`add_inbound_user`/`remove_inbound_user`）增量修改运行中的进程，
不重写共享文件**；Marzban 应用/core 自己启动或整体重建时，会从数据库
用 `include_db_users()` 重新生成内存里的完整配置，通过 stdin 灌给
Xray 子进程。**但 Marzban 确实有一个会写这个共享文件的管理 API**：
`PUT /api/core/config`（`app/routers/core.py::modify_core_config()`）
会执行 `with open(XRAY_JSON, "w") as f: f.write(json.dumps(payload,
...))`，随后重启 core 和已连接的远端节点——第一版"Marzban 从不写回
`XRAY_JSON`"的结论是错的，已更正。

**因此本仓库必须明确一个共享文件 ownership policy（本轮新增决定）**：
本仓库的 renderer 是这份共享骨架文件**唯一合法的 writer**；本应用
不得调用 Marzban 的 `PUT /api/core/config`；运维不得通过 Marzban
UI/API 手动改 core config；如果权限层面暂时无法完全阻止这条管理
API，Phase 2B 必须实现 **drift detection**——**第三次修订：算法已
重写为三态模型**（`last_applied_state`/`current_disk_state`/
`new_db_desired_state`）：`current_disk_state != last_applied_state`
才是未授权漂移，fail closed 并告警；`new_db_desired_state !=
last_applied_state` 且磁盘未被动过，是正常业务变更，应正常渲染，
不得当作漂移拒绝（第二版把这两者混为一谈，会把正常业务变更误判为
漂移，已废止）。**第三次修订第二轮更正两处**：①`current_disk_state`/
`last_applied_state` 的 fingerprint scope 必须覆盖**整份 repo-owned
的共享配置对象**（不只是 `outbounds`/`routing`——第一轮的 scope 太窄，
无法检测 Marzban `PUT /api/core/config` 只改 `inbounds`/Reality 字段
的未授权写入），任何排除字段需显式列出理由，两者 canonicalization
规则必须一致，无法解析的 JSON 必须 fail closed；②`last_applied_state`
只能在 `XrayFileProvider.apply()` 整个九步流程成功返回
`ApplyResult(True, ...)` 之后才提交（不是"写盘成功那一刻"），并区分
rollback 成功（基线保持不变）、rollback 自身失败（基线标记未知、
fail closed）、Xray 已生效但基线持久化失败（必须在"回滚运行时"或
"标记 unknown/fail-closed"之间明确选一个）三种场景。**第三次修订
第三轮再次更正**：核实 `XrayFileProvider.apply()` 现有代码和
`test_safe_reload.py` 现有测试覆盖后确认，第二轮"任何 apply 失败都
完整走 restore(backup) 回滚"这个描述和当前实现不符——`install()`/
第一次 `reload()` 周围没有 `try/except`，两者抛异常会绕过回滚路径
直接从 `apply()` 传播出去，现有护栏测试也没有覆盖这两种异常场景。
本轮据实更正 ADR 对当前代码行为的描述，新增第四种场景（更精确命名
为"pre-health mutating exception"：异常绕过回滚、磁盘/运行时状态
不确定，必须按"基线不推进 + fail closed 标记"处理，不能等同于"安全
地什么都没发生"，且要与"场景 C"——apply 成功之后基线持久化失败——
明确区分成两个不同的失败时间点），并要求 Phase 2B 在实现
drift-detection 基线机制之前，先加固 `apply()` 让这两处异常也进入
等价于回滚成功/回滚失败的处理路径，同时补齐四类护栏测试
（`install()` 异常、首次 `reload()` 异常、rollback `restore()`
异常、rollback 第二次 `reload()`/健康复核失败——第三次修订第四轮
把测试要求从 2 类扩展到 4 类）。完整规则见 ADR-015 Part A"drift
detection 三态模型"及"last-applied 基线的持久化位置"两节。基线持久化不复用 `TransportVersion`/
`EgressVersion`（grep 确认零引用且 bounded context 不匹配），需要
新的、scope 限定为共享 Xray 配置文件的持久化机制；首次运行需要显式
bootstrap 策略，不得静默假设"无基线=无漂移"。不允许"DB renderer +
Marzban core-config API"两个并列、互不知情的 source of truth 同时
存在。
完整的三条运行时路径
（Path A 应用启动/管理 API、Path B 普通建用户、Path C 普通改/删用户）
和 8 项逐条 `CONFIRMED`/`INFERENCE` 证据见 ADR-015 Part A。

### Decision 2：Marzban 的 provider classification

**`ACCOUNTING`（分类结论不变，理由已更正）**。`Settings` 里全部
`marzban_*` 字段逐一对应"调用 Marzban admin API 创建/管理用户"所需
信息，精确匹配 `AccountingProvider` Protocol。**Marzban 确实有真实的
远端节点管理 API**（`app/routers/node.py`：add/list/get/modify/
reconnect/remove/usage）——第一版"Marzban 完全没有节点库存能力"这句
话是事实错误，已删除。更正后的理由是：这些节点管理能力针对的是
**Marzban 自己的 Xray 执行拓扑**（横向扩展 Marzban 自己的入站处理
能力），和 Lingsway `TransportProvider` 描述的"从外部机场/订阅供应商
同步一份上游代理节点库存"是**两个不同的 bounded context**，逐项能力
映射表（见 ADR-015 Part B）显示所有维度都是 No match/表面相似但语义
不同的 Partial——**不是"能力不存在"，而是"能力存在但不符合这个
contract"**，因此不实现 `TransportProvider`，SPLIT 仍然否决，但否决
理由已更正。

`ADR-013`"Marzban 在新架构中的定位是 transport 层"这一句表述仍然被
`ADR-015-marzban-ownership-and-route-identity.md` supersede。
**`/admin/accounting/health` 的代码接线本轮新增决定，不再是"不需要
修改"**：`ACCOUNTING_PROVIDER=marzban` 且
`TRANSPORT_PROVIDER_MODE=subscription` 时，这个端点实际检查的是和
Marzban 无关的 `SubscriptionTransportProvider`，是真实语义错误。
选定修复方向 **Option H1**：给 `AccountingProvider` 新增
`health_check()`，端点改为调用 `accounting.health_check()`——现在有
ADR-015 满足铁律第 5 条的前置条件，此前"不合比例"的阻塞理由不再成立。
详见 ADR-015 Part B。

### Decision 3：Xray route identity 收敛方案

**`BLOCKED`（第三次修订：第二版"Selected: A"的结论被推翻）**——
exact-source 核实精确 commit `7f396db3e703d71a28060bc9ce4a532
ec64cb1f4` 后确认：Marzban 传给 Xray、Xray 实际用于
`routing.rules[].user` 匹配的 client email 是
`f"{marzban_db_user_id}.{username}"`（`include_db_users()` 与
`operations.py` 的 `add_user`/`update_user`/`remove_user` 两条独立
代码路径都是这个构造方式），**不是**纯 accounting username。因此
候选 A（`gateway_principal = request.username`）、候选 B
（`accounting_user_id` 提前持久化）、候选 C（`sub-{order.id}`）
**全部使用了错误的目标值**，全部被推翻，不是"三者中选一个代价最小
的"，而是"三者共同的前提本身是错的"。

进一步核实 Marzban v0.8.4 公开、受支持的 Admin API
（`UserResponse` 家族：`POST /api/user`/`GET /api/user/{username}`/
`GET /api/users` 的响应模型，以及 `add_user` 端点响应、订阅链接生成器
`generate_v2ray_links()` 的 `extra_data` 来源）——三个独立信号一致
确认**这些 API 均不暴露 Marzban DB user id**，因此当前没有官方支持的
方式能获取 Xray 实际匹配的复合 email。已被明确排除的绕过方式（禁止
使用）：猜测自增 id、直接读生产 SQLite、依赖未公开的 DB 内部实现。

**结论：Decision 3 `BLOCKED`，在 Marzban 官方 API 能提供该值之前，
不得实现候选 A/B/C 中的任何一个，不得实现既有数据 reconciliation
migration。** 完整推导、`AccountUserDTO`/编排数据流的 contingent
候选设计、`BLOCKED` 解除条件见 ADR-015 Part C 第三次修订。

### Reality ownership：无 NEW EVIDENCE

本阶段研究过程中没有发现任何推翻 ADR-014 已 Accepted 的 Reality
ownership 结论的证据，维持原结论不变，不重新讨论。

### Decision 4：Phase 2B implementation matrix（第三次修订：允许
`BLOCKED`，不强行归为 YES/NO）

| 改动类型 | 需要？ |
|---|---|
| `DesiredRoutingState` DTO change | **YES**（扩展 outbound 连接细节；route identity 部分 `BLOCKED`） |
| `AccountUserDTO`/编排数据流 change | **BLOCKED**（contingent on Decision 3；当前 `CREATE_ACCOUNTING_USER` 丢弃 `create_user()` 返回值，无数据通道传给 `APPLY_GATEWAY`） |
| Settings change | **YES**（`XRAY_REALITY_DEST`/`XRAY_REALITY_SERVER_NAME` 读取路径，ADR-014 已定方向，与 route identity 无关） |
| Secret persistence change | **YES**（Reality `privateKey`/`shortIds`，ADR-014 已定方向） |
| DB schema change（route identity 部分） | **BLOCKED**（第二版"NO"不再成立，取决于 Decision 3 是否需要持久化 Marzban DB user id） |
| **DB schema change（drift-detection 基线部分）** | **YES（新增独立条目）**（`TransportVersion`/`EgressVersion` 确认不适用，需要新的 scope 限定持久化） |
| **既有数据 reconciliation** | **BLOCKED**（contingent on Decision 3；通用 fail-closed 要求——全量 preflight、任何异常整体中止、禁止"跳过并记录"——已经确定，可先写进验收标准） |
| provisioning writer change（route identity 部分） | **BLOCKED**（同 Decision 3） |
| **accounting health contract/API** | **YES，不受本轮影响**（`AccountingProvider` 新增 `health_check()`，Option H1，见 ADR-015 Part B） |
| query/adapter change | **YES（不含 route identity 部分）**（参照 `render_xray_routes.py::_active_routes()` 的 JOIN 逻辑；route identity 相关 JOIN 逻辑 `BLOCKED`） |
| Xray renderer change | **YES（不含 route identity 部分）**（产出完整 outbound；Reality `dest`/`serverNames` 改为只读 env；`routing.rules[].user` 渲染逻辑 `BLOCKED`） |
| preservation/validation change | **YES**（按 ADR-014"合法删除语义"重写，且保留 Marzban 协议匹配约束，与 route identity 无关） |
| **共享 `XRAY_JSON` writer guard / drift policy** | **YES，算法已重写**（三态模型；新基线持久化；显式 bootstrap 策略） |
| tests | **部分 YES，部分 BLOCKED**（可写：`accounting.health_check()` 契约、drift-detection 三态模型单测（含 bootstrap/文件缺失/基线更新时机）、既有数据 reconciliation 通用 fail-closed 不变量测试；不可写：`gateway_principal` 最终值契约测试） |

### Phase 2B implementation handoff（第三次修订第二轮更正：整体维持
`ADR-014` 第 5 条门槛，不是"部分可以先做"）

**第一轮的错误，本轮推翻**：曾经写"Reality Settings/Secret、
accounting health、drift detection 等部分可以先实现，只排除 route
identity 相关部分"——这和 `ADR-014`"约束"第 5 条（"Phase 2B 开始前
必须先选定 route identity 收敛方向……这不是可以在写 DTO 的过程中顺便
决定的细节"）直接冲突，而 ADR-015 当时没有显式 supersede 这一条，
导致两份已接受的 ADR 对"能不能开始"给出不同答案。

**更正后的规则**：本 ADR 不 supersede `ADR-014` 第 5 条，**在 Decision
3 从 `BLOCKED` 解锁之前，不启动任何 Phase 2B 代码实现**——无论 Decision
4 矩阵里标的是 `YES` 还是 `BLOCKED`。PR #56（本阶段 2B0）可以作为
"记录了一个有证据支持的 blocker"合并，但合并它不代表授权开始 Phase
2B 实现。**下一步是一个专门解决 Decision 3 / route identity
architecture 的任务**，不是 Phase 2B 实现 PR；Decision 4 矩阵保留
作为"Decision 3 解锁后 Phase 2B 的范围记录"，供那时候的实现 PR 参照。
完整推理见 ADR-015"ADR-014 Phase 2B gate 与本 ADR 的关系"一节。

以上仍不包括：`registry.py` 的真实 opt-in wiring、生产环境启用、
真实生产凭据、真实 Xray reload、部署。这些仍然属于 Phase 2C，在
Phase 2B 本身开始之前更不适用。

### 本阶段（2B0）验收

- 新增/修改 `docs/80-decisions/ADR-015-marzban-ownership-and-route-identity.md`，
  修改本文档，`ADR-013` 补一段 supersede 说明（不重写原文），
  `backend/**`/`ops/**`/`infrastructure/**`/`deploy/**`/`frontend/**`
  等代码目录零改动。
- 只读检索了 Marzban 官方公开仓库精确 `v0.8.4` tag 的源码/文档，未
  连接任何真实 Marzban 实例，未使用任何真实凭据。
- 没有调用任何真实 provider 的网络请求，没有读取生产配置/`.env`/
  `/etc/lingsway/*.conf`。
- 没有 reload 或调用 Xray/Mihomo，没有修改生产数据库。
- **（第三次修订更正）** Decision 1/2 已给出唯一结论；**Decision 3
  截至本轮是 `BLOCKED`（有 exact-source 证据支持，不是未经核实的
  遗留问题）**，既有数据 reconciliation 同样 `BLOCKED`（contingent on
  Decision 3）——第二版"三个前置决策全部关闭"的表述已不再准确，
  已更正为诚实反映当前状态：2 个已关闭 + 1 个有证据支持的 BLOCKED。
- **（第三次修订第二轮更正）** 下一步：等待下一轮独立审查；本 ADR
  不 supersede `ADR-014` 第 5 条，在 Decision 3 的 `BLOCKED` 状态解除
  之前，**不得开始任何 Phase 2B 代码实现**（不只是 route identity
  相关部分——第一轮"其它 `YES` 条目可以单独推进"的表述已被推翻，见
  ADR-015"ADR-014 Phase 2B gate 与本 ADR 的关系"一节）；下一步应该是
  一个专门解决 Decision 3 / route identity architecture 的任务，而
  不是任何形式的 Phase 2B 实现 PR。

## 阶段二 B1：Decision 3 / route identity architecture unblock
（2026-09-11，PR #56 合并后的独立后续任务，docs/ADR-only，不实现代码）

`docs/80-decisions/ADR-016-route-identity-architecture-unblock.md`
（新建）完成了这个专项研究，**supersede** 本文档和 ADR-015 里 Decision
3 的 `BLOCKED` 结论。本节只摘录结论，完整推导/exact-source 证据/
候选比较见 ADR-016。

**Marzban 版本调研**：核实精确 pinned tag `v0.8.4`（commit
`7f396db3e703d71a28060bc9ce4a532ec64cb1f4`）与当前 `master` 分支
HEAD 两者的 `app/models/user.py`（`UserResponse` 家族）、
`app/xray/operations.py`（复合 email 构造公式）、官方 webhook
payload（`app/utils/notification.py`）——三者交叉验证，**从 v0.8.4
到当前 master，Marzban 官方公开受支持的 API/webhook 均未暴露 DB
`id` 或复合 client email，纯版本升级不能解决这个问题**。

**Decision 3：从 `BLOCKED` 改为 `SELECTED`**——选定 Candidate B：
为 pinned Marzban 镜像维护一个最小化 source patch。**第三轮更正**：
补丁机制不是此前写的 `model_validator(mode="before")`（这个描述
未经验证，且方向不完全正确——会丢失其它字段的 `from_attributes`
提取能力），而是"新增一个 `exclude=True` 的 `id: int` 字段（照旧
通过 `from_attributes` 从 ORM 对象提取，不影响任何其它字段）+ 一个
`@computed_field` 计算属性"。**第四轮更正**：验证证据从沙箱环境的
`pydantic==2.13.5`+手工模拟升级为 pinned Marzban 精确依赖版本
`pydantic==2.10.4`/`fastapi==0.115.2`，用真正的 FastAPI
`TestClient` 对两条路径发起真实 HTTP 请求（不是重复调用
`model_validate()` 模拟）：`POST /api/user`（对应 `add_user`）和
`GET /api/user/{username}`（对应 `get_user`，真正经过 FastAPI
`response_model` 序列化）产出完全一致的结果，现有字段全部正确
保留，`id` 本身正确从输出中排除。让 `routing_principal: str` 字段
（值 = `f"{marzban_db_user_id}.{username}"`，与 Marzban 自己
`operations.py` 内部计算公式逐字节一致）在本仓库实际依赖的
`POST /api/user`（`create_user`）和 `GET /api/user/{username}`
（`get_user`）两条路径上可靠可用——两者的响应构造均已 exact-source
核实持有带 `id` 的原始 ORM 对象，详见 ADR-016 "exact-source patch
contract" 小节；`list_users`/webhook 等其它复用 `UserResponse` 的
路径不在本方案承诺范围内。
真实 Marzban-backed `AccountingProvider` 读取这个字段，
`AccountUserDTO` 新增同名字段，`CREATE_ACCOUNTING_USER` 保留返回值
并传给 `APPLY_GATEWAY`，`GatewayRouteBinding.gateway_principal` 的
最终语义正式定为"Xray routing principal"。`accounting_user_id`
维持现状（accounting username），两者是不同 bounded context 的
独立标识符。Candidate A（纯升级）、Candidate C（只读 DB 集成）、
消除 per-user email 依赖的替代 Xray 拓扑均已评估并否决，理由见
ADR-016 Part C/D。

**既有数据 reconciliation：从 `BLOCKED` 改为 `CONFIRMED` 可行，且
明确不是 Alembic migration**——因为 `routing_principal` 是 Marzban
已有数据的纯衍生值，reconciliation 只需对每个历史 active binding
调用一次打了补丁的 `GET /api/user/{username}`，不存在"本地 DB
算不出目标值"的结构性障碍；但因为这个流程依赖外部 Marzban API
调用（网络可用性/admin token/限流），**独立审查更正**：不能实现为
Alembic revision（会把数据库 schema 升级绑定在一个非事务性外部
系统上，`AGENTS.md` 铁律第 7 条针对的是纯数据库确定性 reconciliation，
不适用于这种场景）——改为一个**独立的、显式触发的受控 reconciliation
工具/作业**：全量只读查询 + preflight 完成后，**第五轮更正（第三、
四轮的修复均不成立）**：独立审查核实 `GatewayRouteBinding` 真实
索引后指出，第四轮"`SELECT ... FOR UPDATE` 依赖索引范围 next-key
lock 挡住 phantom insert"的前提与真实 schema 不符——这张表没有
支撑 `enabled = TRUE AND released_at IS NULL` 这个复合条件的索引。
更正为 **MySQL named advisory lock**（`GET_LOCK()`/
`RELEASE_LOCK()`，命名空间隔离、覆盖数据库名）作为跨进程互斥的
唯一正确性来源：`ensure_gateway_route_binding()`、
`release_egress()`、未来的 reconciliation 工具全部必须获取同一把
命名锁才能修改 `GatewayRouteBinding`；锁的获取/释放必须覆盖完整
写事务（同一物理连接持有，commit/rollback 不自动释放锁，需要
显式 `RELEASE_LOCK()`）；`GET_LOCK()` 返回 `0`/`NULL` 一律 fail
closed。既有数据 reconciliation 拆成两阶段：Phase A 不持锁的只读
外部 preflight（避免 Marzban 慢请求期间持有全表锁），Phase B 持锁
后重新查询、与 Phase A 快照严格比较、通过后才在单一事务内批量
UPDATE。conditional UPDATE + rowcount 校验降级为纵深防御；人工
维护窗口降级为可选运维措施，不再是 correctness 的必要条件——真正
的互斥保证来自"所有 writer 都遵守同一把命名锁"这件事本身。完整
设计（含锁的边界说明和 Phase 2B 验收测试清单）见 ADR-016。

**Phase 2B gate**：`ADR-014` 第 5 条"选定收敛方向"的前提条件现在
已满足，门槛本身解除。**但这不等于可以立即开始实现**——真正的
Phase 2B 实现 PR（Candidate B 补丁本身及其契约测试、真实 Marzban
adapter、DTO/编排改动、独立的 reconciliation 工具/作业、
`XrayFileProvider.apply()` 异常兜底加固、ADR-015 Decision 4 矩阵
其余 `YES` 条目）仍需单独走完整实现/测试/审查流程，是本任务之后的
下一步，不在本次 docs-only 任务范围内。

### 本阶段（2B1）验收

- 新增 `docs/80-decisions/ADR-016-route-identity-architecture-unblock.md`，
  ADR-015 补一段 supersede 补记（不重写原 Decision 3 内容，保留
  作为历史记录），修改本文档，`backend/**`/`ops/**`/
  `infrastructure/**`/`deploy/**`/`frontend/**`/`.github/**` 等代码
  目录零改动，无新增 migration/schema 改动。
- 只读检索了 Marzban 官方公开仓库精确 `v0.8.4` tag 与 `master` 分支
  当前 HEAD 的源码/文档，未连接任何真实 Marzban 实例，未使用任何
  真实凭据，未读取生产 SQLite/MySQL。
- 没有调用任何真实 provider 的网络请求。
- 没有 reload 或调用 Xray/Mihomo，没有修改生产数据库。
- Decision 3 已从 `BLOCKED` 推进为 `SELECTED`（Candidate B），Phase
  2B 的门槛条件已满足，但 Phase 2B 实现本身仍未开始，是下一步的
  独立任务。

### Phase 2B 实现进度（本轮新增，最小状态同步，不重写以上历史记录）

- **Phase 2B2（PR #59，已合并）**：`XrayFileProvider.apply()` 异常
  兜底加固已完成——`install()`、首次 `reload()`、首次post-reload
  `health()` 三者中任一抛出异常，现在都会被同一个
  `try/except` 捕获并进入既有的 `_rollback()` 完整回滚路径（restore
  → reload → health 复核），不再绕过回滚裸抛异常；回滚成功也不会让
  `apply()` 返回成功；rollback 自身的 `restore()`/第二次 `reload()`/
  `health()` 失败或返回 unhealthy，均按 fail-closed 处理（不虚报
  `rollback_reverified`，disable `new_username`，alert，审计）。新增
  7 个失败注入测试覆盖上述全部场景，详见 PR #59 描述。上文"第三次
  修订第三轮"一节描述的 gap 到此已关闭，仅作历史记录保留。
- **Phase 2B3（PR #60，已合并）**：落实上文
  "更正后的强制机制"一节定义的 MySQL named advisory lock——新增
  `backend/app/infra/gateway_route_lock.py`
  （`gateway_route_binding_write()`），`SqlAlchemyProvisioningState.
  ensure_gateway_route_binding()`（经由
  `SqlAlchemyProvisioningState.gateway_route_binding_lock()` 与
  `backend/app/services.py::confirm_payment_and_provision()` 的调用
  方持锁）与 `accounting_sync.release_egress()` 均已接入同一把
  `lingsway:<database>:gateway-route-bindings-write` 命名锁；锁的获取
  发生在专用于持锁的独立 MySQL 连接上（而非复用 ORM Session 会随
  自身事务边界换连接的池化连接），因此 `GET_LOCK()`/`RELEASE_LOCK()`
  始终由同一物理连接持有，不依赖"连接池大概率还是同一条"的假设；
  调用方仍需在锁保护的 `with` 块内自行完成 mutation 与
  `commit()`/`rollback()`，锁只在该块退出时释放。详细设计、真实
  MySQL 并发测试结果见本 PR 描述。仍未开始：Candidate B 补丁本身、
  真实 Marzban adapter、DTO/编排改动、独立 reconciliation 工具、
  ADR-015 Decision 4 矩阵其余 `YES` 条目——均为后续独立 PR。

  **已知遗留问题（Work 独立审查发现，本轮已记录、未展开架构重构）**：
  provisioning 路径的命名锁当前从 `ProvisioningService.provision()`
  开始前就获取，持锁跨越 egress/accounting/gateway/notify 等多个外部
  调用，`DEFAULT_LOCK_TIMEOUT_SECONDS = 30` 只是一个占位默认值，不是
  基于真实 provider 调用延迟推导出的预算——当前测试用的是近乎瞬时
  返回的 mock provider，而真实 Webshare transport 单次调用已文档化
  超时 20 秒、外加限流/重试等待，可能远超这个假设。**在真实（非
  mock）provider 接入之前，必须重新评估锁的持有跨度与超时值**；
  如果要在不改变 provisioning saga 事务/补偿语义的前提下安全缩小
  锁的持有跨度，需要先给出具体设计（例如把锁的获取点从
  `provision()` 整体前移到 `APPLY_GATEWAY` 步骤内部，这需要修改
  `ProvisioningService.provision()` 本身的控制流），本轮不做这个
  架构改动，留给下一个 Phase 2B 实现 PR 作为已知 blocker 处理。
- **Phase 2B4（本 PR，Marzban pinned source patch contract）**：为
  pinned Marzban `v0.8.4`（commit
  `7f396db3e703d71a28060bc9ce4a532ec64cb1f4`）建立最小化、可重复、
  fail-closed 的 `app/models/user.py::UserResponse.routing_principal`
  source patch，落实上文"最终方案"一节与 ADR-016 Decision 3 定义的
  机制（**含本 PR review 过程中的第六次修订更正**）：新增
  `id: int = Field(exclude=True)` 字段 + 普通字段
  `routing_principal: str = Field(default="")` +
  `@model_validator(mode="after")` 方法赋值
  `f"{self.id}.{self.username}"`——与 `app/xray/operations.py` 内部
  既有公式逐字节一致；`SubscriptionUserResponse(UserResponse)`（客户
  可见 `GET /{token}/info` 订阅端点所用模型）新增
  `routing_principal: str = Field(default="", exclude=True)` 覆盖排除，
  防止这个源自数据库 id 的字段泄漏到本 ADR 范围之外的响应面（该机制
  最初用 `@computed_field` 实现，因其在 pinned `pydantic==2.10.4` 下
  无法被子类选择性排除而改为上述写法，详见 ADR-016 第六次修订）。
  新增
  `infrastructure/marzban/patches/0001-expose-routing-principal.patch`
  （真实 git 补丁，非伪代码）、`apply_patch.sh`（`--check` 模式为
  fail-closed drift guard，真实 `git apply` 验证，未通过时不产生任何
  部分修改、不 fallback 到未打补丁源码）。**上游源码不 vendor 进
  Git**：Marzban 是 AGPL-3.0 协议，本仓库根 `LICENSE` 是不带第三方
  代码例外条款的专有声明，两者冲突不应由本 PR 自行下法律结论解决；
  改为测试运行时（`infrastructure/marzban/patches/tests/
  _pinned_upstream.py`）通过网络抓取 pinned commit 的该文件并校验
  sha256，抓取失败或哈希不符一律 fail-closed，不 skip、不用陈旧本地
  副本 fallback——这使契约测试非完全离线（需要出站网络访问 pinned
  raw.githubusercontent.com），是本次为避免许可证风险刻意接受的
  已知折衷，是否要以及如何正式 vendor Marzban 源码留给仓库所有者或
  法务决定。契约测试导入真正打了补丁的上游
  `UserResponse`（`infrastructure/marzban/patches/tests/`，独立于
  `backend/tests/`），通过真实 FastAPI `TestClient` + pinned
  `pydantic==2.10.4`/`fastapi==0.115.2` 验证 `POST /api/user` 与
  `GET /api/user/{username}` 两条路径返回一致的
  `routing_principal`、raw `id` 不出现在响应体、既有字段不受影响、
  `SubscriptionUserResponse` 正确排除 `routing_principal`、以及
  `links`/`subscription_url` 为空时经过真实（非短路）校验路径也能
  正确计算 `routing_principal`；并验证未打补丁源码不会被误判为
  contract-ready。详细结果见本 PR 描述。**本 PR 不构建、不部署、不
  连接任何真实 Marzban 镜像/实例**，Marzban 在生产仍运行官方未打
  补丁镜像；仍未开始：真实 Marzban adapter、
  `AccountUserDTO.routing_principal`、编排改动、registry wiring、
  existing-data reconciliation——均为后续独立 PR。Phase 2B3 记录的
  provisioning named-lock 持有跨度 /
  `DEFAULT_LOCK_TIMEOUT_SECONDS = 30` blocker **本 PR 未处理，仍然
  存在**，必须在真实 provider wiring 前单独解决。

  **本 PR review 过程中的第四轮修订（接入 CI + 独立 pinned-upstream
  contract 校验）**：独立审查指出两处 Major——(1)
  `infrastructure/marzban/patches/tests` 当时完全没有接入 GitHub CI，
  `.github/workflows/ci.yml` 的 `backend` job 只跑
  `backend/tests/**`，意味着即使 patch/drift-guard/contract 被后续
  改动破坏，现有 required checks 仍可能全绿。修复：新增独立
  `marzban-contract` job，在专属 runner 上安装 pinned
  `pydantic==2.10.4`/`fastapi==0.115.2`/`starlette==0.40.0` 后运行
  `python infrastructure/marzban/patches/verify_pinned_upstream.py`
  与 `python -m pytest infrastructure/marzban/patches/tests -v`；不与
  `backend` job 共用 Python 环境，不污染其依赖版本；job
  无条件运行（不做路径过滤），因此即使将来把它加入 `main` 分支保护的
  required checks 也不会在不相关的 PR 上永久 pending。(2)
  `apply_patch.sh --check` 只证明补丁自身的 context 行仍能套用到给定
  source tree，并不证明 pinned commit 的其它文件仍然构成同一个
  contract——具体地，`app/xray/operations.py` 才是 Marzban 内部实际
  计算 Xray client email 公式的地方，若上游未来只改这个文件、不动
  `app/models/user.py`，drift guard 完全不会察觉。修复：新增
  `infrastructure/marzban/patches/pinned_upstream_manifest.py`
  （pinned commit、三个受影响文件——`app/models/user.py`、
  `app/xray/operations.py`、`requirements.txt`——及其 sha256、Xray
  email 公式字符串、pinned 依赖版本的唯一权威记录）与
  `verify_pinned_upstream.py`（fail-closed 校验器：任一文件抓取失败、
  哈希不符、公式缺失、依赖版本漂移，均非零退出并汇报全部问题，从不
  跳过或退回旧内容）。`tests/test_pinned_upstream_contract.py`
  用真实网络抓取 + 有针对性的 monkeypatch 逐项验证该校验器自身的
  fail-closed 行为。这是与 patch 可套用性验证（`apply_patch.sh
  --check`）互补、独立存在的第二层保证，两者含义不同，缺一不可。
  以上均已落地并通过验证（19/19 测试通过，含新增的 8 个）。

  **第五轮修订（人工明确批准的 Path B：完整依赖锁）**：审查指出
  `marzban-contract` job 只 pin 了 5 个 top-level 包，pip 仍会为
  `anyio`/`httpcore`/`certifi`/`typing-extensions`/`pydantic-core`/
  `pluggy`/`packaging` 等传递依赖解析当天可用的最新兼容版本，
  "pinned contract" 门禁并不真正可复现。修复：新增
  `infrastructure/marzban/patches/requirements-contract.in`
  （5 个 top-level exact pin 的唯一权威记录）与
  `requirements-contract.txt`（用 pip-tools 7.6.1 + Python 3.12.3 +
  pip 26.2.1 执行
  `pip-compile --generate-hashes --output-file=requirements-contract.txt
  --no-header requirements-contract.in` 生成，全部 17 个直接+传递
  依赖均精确 `==` 且带 sha256 hash，绝不手工编辑，变更 top-level pin
  时必须重新生成）；CI 改为
  `pip install --require-hashes -r requirements-contract.txt` 安装，
  不再手工列出 5 个包。新增 `lock_consistency.py`（fail-closed 一致性
  guard，纯标准库、无需先装依赖：top-level pin 缺失/版本不符、
  出现非精确 `==` 的传递依赖行、缺 hash，均非零退出并汇报全部问题）
  及 `tests/test_lock_consistency.py`（8 个测试，逐项覆盖上述四种
  漂移场景 + 正常 lock 的基线）。CI 新增独立步骤先跑一致性 guard，
  再用 `--require-hashes` 安装，安装/一致性任一失败直接使
  `marzban-contract` job 失败，不允许 fallback 到无 hash 安装。
  以上均已落地并在全新 venv 中端到端验证通过
  （27/27 Marzban 测试全部通过，含新增的 8 个 lock 一致性测试）。

- **Phase 2B5（本 PR，narrow GatewayRouteBinding named-lock hold span）**：
  解决 Phase 2B3 记录的、此前一直未处理的
  provisioning named-lock 持有跨度 blocker——真实（非 mock）
  `EgressProvider`/`AccountingProvider`/`GatewayProvider` 接入前的
  硬前置。新增 `docs/80-decisions/ADR-017-provisioning-lock-hold-span-narrowing.md`
  （modifying `backend/app/domain/provisioning.py` 前置要求的 ADR，
  详细定义 phase boundary、transaction/commit/rollback/lock
  acquire-release 各自的 owner、GET_LOCK 失败时的补偿语义、
  30 秒 timeout 的精确含义、以及为何 `NOTIFY` 暂不移出锁）。

  `ProvisioningService` 新增 `provision_prepare()`（steps 1-6，
  `CAPACITY`..`CREATE_ACCOUNTING_USER`，从不接触
  `GatewayRouteBinding`、从不获取 named lock）与
  `provision_apply_gateway()`（steps 7-9，`APPLY_GATEWAY`..`NOTIFY`，
  唯一会调用 `desired_routing_state()` 的方法，调用方必须全程持有
  named lock）；`provision()` 保留、改写为两者的组合，对现有直接调用方
  完全透明。新增 `ProvisioningCheckpoint`（阶段间传递 `run_id`/
  `endpoint`/`tenant` 的不可变 continuation）与
  `fail_apply_gateway_lock_acquisition()`（GET_LOCK 本身失败时的
  补偿：disable accounting user、`GATEWAY_APPLY_FAILED` alert、
  run 标记 FAILED——与 `APPLY_GATEWAY` 自身异常处理逐字节一致，避免
  两条路径语义漂移）。

  `backend/app/services.py::confirm_payment_and_provision()` 改为：
  `provision_prepare()` 在锁外运行；仅在拿到非终态 checkpoint 后，
  紧邻 `provision_apply_gateway()` 调用前才 `GET_LOCK`；新增
  `except GatewayRouteBindingLockError` 分支执行上述补偿 +
  `state.rollback_database()`，再走既有 `fail_paid_purchase()` 终态
  事务。锁的释放点不变——仍是 `gateway_route_binding_lock()` 这个
  `with` 块的退出，因此仍在 `activate_paid_purchase()`/commit 完成
  之后（`gateway_route_binding_write()`自身 `finally` 的结构性保证）。

  production-bypass audit（要求逐项确认，非字符串 grep）：
  仓库范围搜索确认当前唯一真实生产路径是
  `_OrderProvisioningState`/`confirm_payment_and_provision()`；
  `backend/app/services.py::provision()`（独立便利函数）与直接调用
  `ProvisioningService.provision()`均无生产调用点。为把这个不变量
  变成结构性保证而非"目前没人接错"，新增
  `backend/app/infra/gateway_route_lock.py::
  session_holds_gateway_route_binding_lock()`（基于真实 SQLAlchemy
  `Session.info` 的运行时标记，由 `gateway_route_binding_write()`
  自己维护）与
  `SqlAlchemyProvisioningState.ensure_gateway_route_binding()`
  的前置检查：未持有锁时调用直接 `raise
  GatewayRouteBindingLockError`（fail-closed，零 mutation）。

  真实 MySQL（本地用 MariaDB 10.11 best-effort 验证，CI 上以真实
  MySQL 8.4 backend job 为准——见下方"未解决项"）并发测试新增
  `backend/tests/integration/test_provisioning_phase_boundary.py`
  （6 个测试，均驱动真实 `confirm_payment_and_provision()` +
  `SqlAlchemyProvisioningState`，非重新实现）：steps 1-6 在另一
  connection 持锁期间完整跑完（证明不需要锁）；成功路径锁从
  `APPLY_GATEWAY` mutation 持有到 commit 完成、之后立即可被其它
  connection 获取；gateway 失败时 rollback 在锁释放前完成、零
  partial `GatewayRouteBinding` 行残留；GET_LOCK 本身超时时零写入、
  accounting user disabled、provisioning FAILED；`PENDING_MANUAL`
  与 accounting-create 失败路径 lock acquire count == 0。既有
  `test_gateway_route_binding_lock.py`（13 个测试）与
  `test_db_adapters.py` 全部继续通过，未回退任何 PR #60 已建立的
  writer mutual-exclusion 保证。

  `DEFAULT_LOCK_TIMEOUT_SECONDS = 30` 数值本身**未改动**——ADR-017
  明确它是 GET_LOCK 的等待获取超时（可用性参数），不是持有时长上限；
  缩小 hold span 后，Webshare/Marzban 的慢 HTTP 调用不再计入 named-lock
  持有时长，但真实 `GatewayProvider`（唯一仍在锁内执行的 provider）
  的生产延迟尚无实测数据，仍需后续测量调优。

  **NOTIFY 仍在锁内，未移出**：本 PR 明确只解决"steps 1-6 不持锁"这一
  真实 provider 接入的硬前置；`NOTIFY` 移出 critical section 需要先
  分析其失败补偿语义在锁外是否仍然一致，属于独立后续工作，ADR-017
  记录为未解决项，不得误报"所有 external call 已移出锁"。

  **本 PR 显式不做**（保持范围）：真实 Marzban/Webshare/Mihomo
  provider 实现、`AccountUserDTO.routing_principal` 或
  `create_user` 返回值数据链改动、`registry.py` 启用 marzban、
  existing-data reconciliation、DB schema/Alembic migration、
  生产凭据/生产网络调用、部署。

  **未解决项**：(1) 本地验证用的是 MariaDB 10.11（沙箱环境唯一可用），
  非仓库 CI 实际使用的真实 MySQL 8.4——GET_LOCK/RELEASE_LOCK 语义在两者
  上被验证一致，但权威结论以 GitHub Actions 的 MySQL 8.4 backend job
  为准，本 PR 描述中会同时报告两者结果。(2) `NOTIFY` 仍在锁内，真实
  `NotifyProvider` 接入仍被阻塞，见上文。(3) `GatewayProvider` 真实
  生产延迟下 30 秒 timeout 是否仍然合理，需要后续实测。

- **Phase 2B6（本 PR，route identity / routing_principal 内部数据流
  管道）**：按 ADR-016 Decision 3 已经定好的精确契约，打通
  `AccountingProvider.create_user()` → `AccountUserDTO.routing_principal`
  → `ProvisioningCheckpoint` → `provision_apply_gateway()` →
  `ProvisioningState.desired_routing_state()` →
  `GatewayRouteBinding.gateway_principal` →
  `DesiredRoutingState.user_routes` key → Xray `routing.rules[].user`
  这条完整数据链——**只做本仓库内部 contract/dataflow 改动，不实现
  真实 Marzban HTTP adapter**（ADR-016 Candidate B 的 Marzban 补丁/
  真实 provider 实现仍是独立的后续任务）。

  `backend/app/providers/base.py::AccountUserDTO` 新增必填字段
  `routing_principal: str`（无 default，不允许 `None`，不允许静默回退
  `username`/`tenant_id`）；`MockAccountingProvider.create_user()`
  返回确定性但与 `username`/egress `tenant_id` 均不同的
  `f"mock-routing.{username}"`（`disable_user`/`set_quota`/
  `set_expire` 全部改为 keyword 构造并完整保留原 `routing_principal`，
  不重新计算）。

  `ProvisioningCheckpoint` 新增 `account_user: AccountUserDTO`
  字段（保留完整 DTO，不只是一个字符串，遵照 ADR-016"必须保留
  create_user() 返回值"的要求）。`provision_prepare()` 的
  `CREATE_ACCOUNTING_USER` 步骤不再丢弃 `create_user()` 返回值，新增
  fail-closed contract 校验：返回的 `username` 必须等于
  `request.username`、`routing_principal` 必须是非空/非纯空白字符串
  ——任一校验失败都当作该步骤的 failure 处理（disable user、
  `state.rollback_database()`、run 标记 FAILED、不返回 checkpoint、
  不进入 `APPLY_GATEWAY`、不获取 named lock、零
  `GatewayRouteBinding` mutation），不允许 fallback。

  `ProvisioningState.desired_routing_state()`（protocol 和
  `SqlAlchemyProvisioningState`/`_OrderProvisioningState` 两个实现）
  新增第四个参数 `routing_principal: str`，由
  `provision_apply_gateway()` 从
  `checkpoint.account_user.routing_principal` 显式传入——domain 层
  自己不计算 Marzban 公式，infra 层也不重新调用
  accounting provider 或重新猜 principal。
  `SqlAlchemyProvisioningState.desired_routing_state()`
  内部调用 `ensure_gateway_route_binding(routing_principal, endpoint)`
  （此前错误地传入 `tenant.tenant_id`，即 Webshare 出口租户 id）；
  `DesiredRoutingState.user_routes` 的 key 也从 `request.username`
  改为 `binding.gateway_principal`——两处统一使用同一个
  routing principal，消除"DB 写 routing_principal 但即时 candidate
  仍写 username"这种双重语义。`Subscription.accounting_user_id`
  的语义/写入时机（`admin_confirm_payment` 里、accounting username）
  完全不变，未触碰 schema，未新增 Alembic migration。

  新增 6 条真实 MySQL/MariaDB production-backed 集成测试（驱动真实
  `confirm_payment_and_provision()` + `SqlAlchemyProvisioningState`）：
  证明一次成功开通后 `Subscription.accounting_user_id ==
  request.username` 且 `GatewayRouteBinding.gateway_principal` 等于
  provider 返回的 `routing_principal`，两者与 egress tenant_id 三者
  互不相同；证明用真实 `GatewayRouteBinding` 行渲染出的
  `XrayFileProvider.render()` 候选配置里 `routing.rules[].user` 的值
  就是 `routing_principal`，不是 `username`；证明 provider 返回空/
  纯空白 `routing_principal`，以及返回 `username` 不匹配的
  `AccountUserDTO`，两种场景下都会在 `CREATE_ACCOUNTING_USER`
  fail-closed（disable user、DB 回滚、run FAILED、零
  `GatewayRouteBinding` 写入、named lock 从未获取）。既有全部单元/
  集成测试（含 ADR-017 的 FAILED/SUCCEEDED/PENDING_MANUAL
  run-persistence-ordering 测试、ADR-016/017 建立的 named-lock
  writer 互斥测试）继续通过，未回退任何既有保证。

  **本 PR 显式不做**（保持范围）：真实 Marzban-backed
  `AccountingProvider` 实现、Marzban `UserResponse` 补丁本身
  （`infrastructure/marzban/patches/` 已有独立契约测试覆盖那一层）、
  `registry.py` 启用 marzban、既有数据 reconciliation 工具、
  DB schema/Alembic migration、生产凭据/生产网络调用、部署。

- **Phase 2B7（本 PR，`MarzbanAccountingProvider` adapter 实现）**：
  新增 `backend/app/providers/accounting/marzban.py`，完整实现
  `AccountingProvider` contract（`create_user`/`disable_user`/
  `set_quota`/`set_expire`/`get_connection_links`/`get_usage`），
  对接 pinned Marzban v0.8.4（commit
  `7f396db3e703d71a28060bc9ce4a532ec64cb1f4`，与
  `infrastructure/marzban/patches/pinned_upstream_manifest.py` 记录的
  同一个 pinned commit）的公开、已实测的 HTTP contract：
  `POST /api/admin/token`（OAuth2 password-grant form，非 JSON）、
  `POST /api/user`、`GET /api/user/{username}`、
  `PUT /api/user/{username}`——均以 exact-source 核对（本轮直接抓取
  `app/routers/admin.py`/`app/routers/user.py`/`app/models/admin.py`/
  `app/models/user.py`/`app/models/proxy.py`，未凭记忆假设 API 形状）。

  **已完成**：
  - auth/token 生命周期：token 缓存复用；对已认证请求收到 401 时清除
    缓存、重新认证一次、原请求重试一次；第二次仍 401 则 fail closed；
    除这一条以外不自动重试任何有副作用的 POST/PUT——
    `create_user()` 的 transport 层异常（结果不确定）绝不触发自动
    重复 POST。
  - `create_user()` 请求体严格遵循 pinned `UserCreate` contract
    （`status=active`、`data_limit`、`data_limit_reset_strategy=
    no_reset`、`expire`、`proxies`、`inbounds`）；`proxies` 对配置的
    协议发送空对象（`{protocol: {}}`），由 Marzban 自己按
    pinned `VLESSSettings`/`VMessSettings`/`TrojanSettings`/
    `ShadowsocksSettings`（均 `default_factory`，exact-source 已核实）
    生成 UUID/password，本仓库 domain 层完全不生成这类材料；
    `inbounds` 严格解析并验证 `marzban_default_protocol`/
    `marzban_default_inbounds_json`（invalid JSON、非 object、协议不
    受支持、inbound 非 `list[str]`、空白 tag，全部在构造期 fail
    closed，不静默 fallback 到任意 inbound）。
  - 响应映射：`routing_principal` 缺失/`null`/非字符串/空白，一律
    `MarzbanContractError`，绝不 fallback 到 `username`/`tenant_id`/
    本地计算 `f"{id}.{username}"`；`username` 不匹配请求同样 fail
    closed；`data_limit`/`used_traffic`/`expire`/`status` 类型/取值
    校验齐全。`expire` 映射：`None → 0`（pinned `POST /api/user`
    docstring 明确"Use 0 for unlimited"）；timezone-aware datetime
    正确转 UTC epoch seconds；naive datetime 直接 fail closed（仓库
    既有 contract 未定义"naive 按哪个时区解释"，不猜测）。
  - `409`（用户名冲突）明确 fail closed，不做"GET 已有用户后自动认领"
    这类幂等 reconciliation——按任务要求，这是一个尚未做出的、独立的
    idempotency architecture decision，本 PR 不擅自决定。
  - `disable_user()` 只 `PUT status=disabled`，绝不 `DELETE`
    （AGENTS.md 铁律 4）；`set_quota()`/`set_expire()` 只发送完成操作
    所需的最小 body。
  - `get_connection_links()`/`get_usage()` 严格校验响应类型
    （`links` 必须是 `list[str]`；`used_traffic` 必须是非负整数），
    不静默降级为空列表/忽略 malformed item。
  - 敏感信息永不出现在异常消息/`repr()`/日志：admin password、
    bearer token、connection links、完整 response payload 均不进入
    `MarzbanApiError`/`MarzbanContractError` 的公开消息或
    `MarzbanAccountingProvider.__repr__()`（自定义 `__repr__`，不用
    dataclass 默认逐字段输出）。
  - 39 条离线 HTTP contract 测试（`httpx.MockTransport`，真实
    `httpx.Client` 往返，不是直接调用 parser），覆盖本任务列出的全部
    20 类场景：认证请求是 form 不是 JSON、bearer token 注入、token
    缓存复用、两级 401 处理、create 请求体、routing_principal 响应
    映射与全部 fail-closed 分支、409 策略、transport-error-不重试
    策略、disable/quota/expire body、links/usage 映射与校验、
    非法 `default_inbounds_json` 在构造期失败、secret 不泄漏。
  - 顺手修正 `.env.example` 里与 `Settings.from_env()` 真实读取的
    canonical 环境变量名不一致的历史命名（`MARZBAN_USERNAME`/
    `MARZBAN_PASSWORD` → `MARZBAN_ADMIN_USERNAME`/
    `MARZBAN_ADMIN_PASSWORD`）——核实过仓库内没有任何部署脚本/文档
    依赖旧名字，纯粹是 `.env.example` 自身的历史文档错误，未新增
    兼容旧名字的 alias。

  **仍未完成**（不得宣称"Marzban 已接入生产"）：
  - patched Marzban 镜像的构建/部署（`infrastructure/marzban/patches/`
    仍只是补丁+验证脚本，没有任何 PR 真正构建过一个带补丁的 Marzban
    镜像）。
  - `registry.py` wiring——本 PR **没有**修改 `registry.py`，
    `ACCOUNTING_PROVIDER=marzban` 依然会被 `_unsupported()` 拒绝，
    merge 后生产环境仍然只能选中 mock accounting，不产生任何真实外部
    副作用。这是刻意的，显式 opt-in wiring 留给下一阶段。
  - 真实 staging 凭据、真实 Marzban 实例的 live 集成测试——本 PR 全部
    测试离线，没有连接任何真实 Marzban。
  - 既有数据 reconciliation（ADR-016 已定方向的独立工具，未实现）。
  - 生产部署。
