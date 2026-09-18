# TASK-S04-Mihomo 激活（S04-B2 / S04-C 的记录载体）

> **为什么现在才有这个文件：** `AGENTS.md`「协作角色与职责」规定
> `docs/82-tasks/TASK-*.md` 是需求与验收标准的**唯一记录载体**，
> 「聊天记录本身不构成记录」。但 S02→S04-B1 共 8 个已合并 PR（#120–#127）
> 全程没有任何 TASK 文件，验收标准只存在于 PR 描述和
> `docs/83-project-continuity.md` §5 的散文里。本文件**向前**补上这个载体，
> 覆盖尚未开始的 S04-B2 与 S04-C；已合并的 S02/S03/S04-A/S04-B1 不做追溯补录
> （追溯编写验收标准没有意义，其交付事实以 PR + 合并提交为准）。

## 目标

把 Mihomo 从"已有完整投影与持久化协调基础、但不可达"推进到"可以在生产被
选中并激活"。当前 `build_registry()` 对 `FORWARDER_PROVIDER != "mock"` 一律
抛 `ProviderConfigurationError`，`backend/app/providers/forwarder/mihomo.py`
与 `mihomo_projection.py` 因此完全不可达。

分两个独立阶段，**各自一个 PR**，前一个合并后再开下一个：

### S04-B2 — 运行时接线（不激活）

1. `reconcile_mihomo_job()` 的四个注入依赖在生产侧有真实实现：
   - `MihomoDesiredSnapshotLoader` —— 从数据库全量渲染期望快照
     （铁律 1：全量生成，禁止增量拼接）；
   - `ControllerSecretResolver` —— 经 `core/secrets.py` 的 purpose-bound 解析；
   - `ProjectionVerifier` —— 运行时回读校验；
   - `MihomoProjectionProvider` —— 即 `MihomoForwarderProvider`。
2. Mihomo DNS 字段/类型的候选 schema 校验闸门（S04-A 只保证了 canonical
   mapping/value ownership，未做完整字段校验——见 `83-project-continuity.md` §5）。
3. 进程生命周期接入：scheduler 侧的 Mihomo 协调循环，registry 由
   `main()` 单例持有并 `close()`。

### S04-C — 注册表放行与激活闸门

1. `build_registry()` 接受 `FORWARDER_PROVIDER=mihomo`，并在
   `Settings.validate_runtime_safety()` 中加入与 Marzban 同级的 fail-closed
   校验（控制端点、secret ref、生产环境下的 TLS 与非占位值检查）。
2. `.env.example` 补齐相应变量。
3. 生产激活仍需 User 明确批准，**不由本任务授权**。

## 约束

- 架构依据是 **ADR-023**（Mihomo 全量配置与激活边界）。不得在没有新 ADR 或
  ADR-023 修订的情况下改变其已裁定的边界。
- **铁律 1**：Mihomo 配置一律从数据库全量生成，禁止增量拼接。
- **铁律 3**：不提供任何 `force` / `override` / `bypass` 开关。
- **铁律 6 的同构要求**：Mihomo 的应用路径必须保持 S04-B1 已建立的
  fail-closed 语义——未决 blocker 阻断后续所有 writer，release 不确定的
  blocker 保留至受控恢复，不自动修复。
- 构造必须零 I/O（`build_registry()` 不得发起任何网络请求或读取运行时状态）。
- 凭据只能以 `secret_ref` 形式流经投影层；`ProjectionTemplate` 必须保持
  secret-free、不可安装，直到 `finalize()`。
- **S04-B2 不得放行注册表**，S04-C 不得顺带改动协调语义。两个阶段不得合并
  成一个 PR。
- 不得触碰 `backend/app/domain/`（本任务全部工作在 providers/infra/registry 层）。

## 允许修改的文件

S04-B2：

```
backend/app/providers/forwarder/mihomo.py
backend/app/providers/forwarder/mihomo_projection.py
backend/app/infra/mihomo_reconciliation.py
backend/app/workers/scheduler.py
backend/tests/unit/test_mihomo_*.py
backend/tests/guards/test_mihomo_*.py
backend/tests/integration/test_mihomo_*.py
docs/83-project-continuity.md
```

S04-C（在 B2 合并之后）：

```
backend/app/providers/registry.py
backend/app/core/config.py
.env.example
backend/tests/unit/test_registry.py
docs/83-project-continuity.md
```

未列出的文件不得修改。

## 验收标准

```sh
python -m ruff check backend
python -m mypy backend/app backend/tests
make test-unit                                    # 全 mock，无网络无 Docker
python -m pytest backend/tests/unit backend/tests/guards
python -m pytest backend/tests                    # 需 TEST_DATABASE_URL (MySQL 8.4)
```

必须成立的专项断言：

- **零 I/O 构造**：`FORWARDER_PROVIDER=mihomo` 下 `build_registry()` 不发起
  任何网络请求（S04-C）。
- **fail-closed 选择**：控制端点/secret 缺失或为占位值时，
  `validate_runtime_safety()` 抛错而不是构造出一个半配置的 provider（S04-C）。
- **无凭据泄漏**：`backend/tests/guards/test_secret_leak.py` 覆盖 Mihomo
  投影与候选配置的 `repr()`/日志路径。
- **全量渲染**：存在一条测试证明期望快照来自数据库全量渲染，而不是在既有
  配置上做增量修改（铁律 1）。
- **S04-B2 的边界证明**：一条测试断言此阶段结束时
  `FORWARDER_PROVIDER=mihomo` **仍然**被 `build_registry()` 拒绝。

人工检查：

- PR 描述必须明确写出本阶段**不做**什么（B2 不放行注册表；C 不授权生产激活）。
- 生产激活是独立的人工闸门，需要 User 明确批准，**不因本任务合并而自动获得**。
