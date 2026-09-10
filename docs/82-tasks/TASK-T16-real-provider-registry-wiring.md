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
