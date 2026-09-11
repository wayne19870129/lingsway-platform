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

**B 类：本机文件/进程管理器，不涉及第三方账户/账单，但构造它们本身需要
真实的本机路径、二进制、进程调用、甚至热重载用的本地网络凭据（细节见下
一节，第一版说这类"只被 registry 的 mock 门挡住、没有其它技术障碍"是
错的）：**
- `gateway/xray_file.py`——`XrayFileProvider`（九步安全重载）
- `forwarder/mihomo.py`——`MihomoForwarderProvider`（文件+热重载+回滚）

修正后的结论没有变得比第一版更简单：B 类确实不涉及第三方账户/账单，比
A 类的"真实外部账户副作用"风险类别不同；但"不涉及外部账户"不等于"没有
真实副作用"或"接入工作量小"——见下一节的具体依赖清单。

### 逐项盘点表

| Provider 分类 | 真实实现文件 | 完整度 | 配置入口 | 凭据来源 | 现有测试覆盖 | 目前是否被任何代码路径实例化 |
|---|---|---|---|---|---|---|
| egress | `egress/webshare.py`（`WebshareProvider`） | **部分**：`list_endpoints()`/`capacity()`/`create_tenant()`/`update_tenant_quota()`/`replace_endpoint()` 都会发真实请求，但响应体全部被丢弃，返回写死的空/零值 DTO（`list_endpoints()` 恒返回 `[]`，`capacity()` 恒返回全零）；`get_tenant_usage()`/`get_credentials()` 直接 `raise NotImplementedError`。写操作有明确的路径白名单守卫（`_guard`），永久拒绝任何 purchase/renew/payment/billing 路径。 | `egress_provider` 只接受 `"mock"` | **没有 `webshare_api_key`（或同类）配置项**——`Settings` 里完全没有为 `WebshareProvider.__init__` 的 `api_key` 参数留位置 | `test_webshare_guard.py`、`test_webshare_procurement.py`：覆盖写操作白名单、限速/429 退避、procurement 只读适配器；**没有测试覆盖真实响应体到 DTO 的映射**（因为这部分还没写） | 否——只在测试文件里手动构造，`registry.py`/其余应用代码里从未 `import WebshareProvider` |
| accounting | **无**（`accounting/` 目录只有 `mock.py`） | 不适用——**这个 provider 类别完全没有真实实现**，连一个空壳文件都没有 | `accounting_provider` 只接受 `"mock"`；但 `Settings` 里已经有一整组 `marzban_*` 字段（`marzban_base_url`/`marzban_admin_username`/`marzban_admin_password`/`marzban_default_protocol`/`marzban_default_inbounds_json`/`marzban_verify_tls`），`validate_runtime_safety()` 甚至已经在校验"生产环境选了 marzban 但配置还是默认值就报错"——**配置层已经为一个不存在的实现做好了校验，这是意外发现，见下方** | 同上，字段已存在但没有对应实现读取它们 | 无——没有实现就没有测试 | 否——不存在这个类 |
| gateway | `gateway/xray_file.py`（`XrayFileProvider`） | **部分**（第一版"较完整"的判断已修正）：九步安全重载序列、校验失败/重载失败/健康检查失败的回滚路径都已实现，没有发现 `NotImplementedError`/占位注释；但**独立审查核实出一个真实的自我校验缺口**：`render()` 产出的候选配置不含 `inbounds` 字段，在真实环境（当前配置有 inbound 客户端）下会被自己的 `_preservation_errors()` 判定为"移除了现有 inbound 客户端"而拒绝——见下方"具体实现缺口"一节，这个问题不影响现有测试是因为测试固件两边都没有 `inbounds` | `gateway_provider` 只接受 `"mock"` | 不涉及第三方账户；但**构造 `XrayFileProvider` 需要一个真实的 `XrayRuntime`**（见下方"构造依赖清单"），不是"不需要任何东西就能接进 registry" | `test_safe_reload.py`：9 个测试，覆盖校验失败提前拦截、重载失败完整回滚、缺失路由/用户/出站的各种拒绝场景、Dockerfile 资产完整性；**没有测试覆盖"当前配置存在真实 inbound 客户端"这个场景**，这正是上面缺口没被测试暴露的原因 | 否——`registry.py` 从未 import，只有测试文件直接构造 |
| forwarder | `forwarder/mihomo.py`（`MihomoForwarderProvider`） | **部分**（第一版"较完整"的判断已修正）：文件安装+热重载+失败回滚，没有发现 `NotImplementedError`/占位注释；但**独立审查核实出一个真实的功能缺口**：`health()` 硬编码恒返回健康，`apply()` 从未调用它做重载后校验，成功判定完全依赖"`install()`/`reload()` 没抛异常"——见下方"具体实现缺口"一节 | `forwarder_provider` 只接受 `"mock"` | 不涉及第三方账户；但**构造 `MihomoForwarderProvider` 需要一个真实的 `MihomoRuntime`**，其本机实现 `LocalMihomoRuntime` 需要一个本地 Mihomo API 的 `api_secret`（见下方"构造依赖清单"）——这是一个真实凭据，只是范围在本机 API 而非第三方账户 | `test_db_adapters.py::test_mihomo_render_failure_restores_exact_pre_operation_state`——**测试描述已改正**：这个测试实际模拟的是 render/install 之后的**重载（reload）失败**并验证精确回滚，不是 render 本身失败（第一版盘点这里的描述不准确，已修正）；**没有测试覆盖 `health()`/重载后校验缺失这个问题** | 否——`registry.py` 从未 import；`MihomoForwarderProvider` 类定义本身是唯一"看到这个类名"的应用代码位置 |
| transport | `transport/subscription.py`（`SubscriptionTransportProvider`） | 未发现 `NotImplementedError`/占位注释（未逐行审计到函数级） | `transport_provider_mode` 只接受 `"mock"` | **需要外部网络访问且涉及敏感信息**：`sync_nodes()` 对订阅 URL（通常带私有 token）发起真实 `httpx` GET 请求，并可选写本地缓存文件——见上面"C 类"说明，第一版把它错误归类为"不需要外部网络凭据" | `test_transport_provider.py`：覆盖订阅解析（Clash YAML / base64 URI 列表）、凭据不泄漏到 metadata、mock transport 下的 `sync_nodes()` 刷新流程 | 否——`registry.py` 从未 import |
| payment / notify / email / captcha / storage | 无 | 不适用——这五类目前都只有 mock/noop 实现，没有任何真实实现文件或占位文件 | 各自只接受 `"mock"`/`"noop"` | 不适用 | 不适用 | 不适用 |

### `XrayFileProvider` / `MihomoForwarderProvider` 的真实构造依赖清单

第一版盘点说这两个 provider"只被 `registry.py` 的 mock 门挡住，没有其它
技术障碍"——**这个结论被 Work 审查指出不成立，核实后确认审查是对的，
改正如下**。`registry.py` 的 `build_registry()` 自己的文档字符串明确
承诺"不需要网络、Docker、文件系统或 shell 访问就能完成组装"
（"Build all providers without network, Docker, filesystem, or shell
access"）。但这两个 provider 的真实构造依赖，直接和这条承诺冲突：

- **`XrayFileProvider.__init__(runtime, ...)`** 需要一个实现了
  `XrayRuntime` 协议（`backup`/`current`/`xray_test`/`install`/`reload`/
  `health`/`restore`）的对象。本机实现 `LocalXrayRuntime` 需要：
  `config_path`、`backup_dir`（文件系统路径）、`xray_binary`/
  `asset_dir`（本机二进制和资源目录，默认写死
  `/usr/local/bin/xray`/`/usr/local/share/xray`）、
  `reload_command`（默认 `systemctl reload xray`，需要 shell/subprocess
  执行权限）；`xray_test()` 用 `subprocess.run` 真的跑一次
  `xray run -test`；`health()` 用 `socket.create_connection` 探测本机
  8443 端口。这些全部是真实的文件系统/subprocess/socket 访问，和
  `build_registry()` 自己"无文件系统无 shell"的承诺矛盾。
- **`MihomoForwarderProvider.__init__(render_document, runtime)`** 需要
  一个 `render_document` 回调和一个实现了 `MihomoRuntime` 协议
  （`backup`/`install`/`reload`/`restore`）的对象。本机实现
  `LocalMihomoRuntime` 需要：`config_path`、`backup_dir`（文件系统路径）、
  **`api_url`、`api_secret`**——`reload()` 会真的对本地 Mihomo API 发起
  一次带 `Authorization: Bearer {api_secret}` 的 HTTP PUT 请求。
  `api_secret` 是一个真实凭据，只是作用范围是本机 API 而不是第三方账户；
  当前 `Settings` 里没有任何字段可以配置它。

结论：把这两个 provider 接进 `build_registry()`，至少需要先回答几个
设计问题，不是简单地把 `registry.py` 里那一行 `!= "mock"` 判断删掉就
可以：①`build_registry()` 是否还要保持"无副作用/无 IO"这条契约——如果
要保持，这两个 provider 的构造需要挪到别处（比如惰性构造、依赖注入
在更外层完成）；②`Settings` 需要新增哪些字段来传递
`config_path`/`backup_dir`/`api_url`/`api_secret` 等路径和凭据；③这些
新字段（尤其 `api_secret`）要按什么规则管理（参照 `AGENTS.md` 的凭据
持有范围规则，不能直接写进 `.env`/代码）。**本阶段不在这里替用户做决定，
只列出问题**——这是"阶段二具体怎么做"需要单独设计的内容，不是本盘点
能够替代的。

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
  必须先修（要么 `render()` 补上从 `desired`/现有配置读取并保留
  `inbounds`，要么明确这个 provider 目前不支持任何已有 inbound 客户端的
  场景），不能指望"先接进 registry 再说"。
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
   接口契约测试，再以显式 opt-in 方式接入 registry**——上一版建议是
   "B 类选一个直接接入 registry"，前提是"没有其它技术障碍"；上面两节
   （构造依赖清单、具体实现缺口）已经确认这个前提不成立：`XrayFileProvider`
   在有真实 inbound 客户端时会被自己的校验拒绝，`MihomoForwarderProvider`
   的 `health()`/`apply()` 没有真正的重载后校验。在这些缺口修好、并且有
   对应的契约测试（例如"候选配置必须保留现有 inbound 客户端""重载后
   `health()` 必须反映真实状态、`apply()` 必须依赖它做成功判定"）之前，
   把它们接进 `build_registry()` 只会让 registry 组装出一个会在真实环境
   下自我拒绝或误报成功的实例，比继续保持 mock 更危险。因此阶段二调整为
   两步：①先在 `gateway/xray_file.py` 或 `forwarder/mihomo.py` 二选一
   （不改动 `registry.py`），把上面确认的实现缺口修好，并补上对应的接口
   契约测试；②契约测试稳定之后，再以**显式 opt-in**（而不是让它替换
   `registry.py` 现有的默认拒绝分支）的方式接入 registry——具体是新增一个
   独立的环境变量开关还是别的机制，留给阶段二自己设计，本阶段只定这个
   "先加固测试、再显式 opt-in"的顺序，不替阶段二做实现细节的决定。
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
