# ADR-038: Mihomo finalization 的 resolver 契约（egress credential plaintext + revision identity）

- 状态：**Accepted**（2026-09-22）
- 关系：**扩展 ADR-037 §6，不推翻它的任何一条**。ADR-019 §4/§7、
  ADR-023 §2.2、ADR-025 §4、ADR-035 §6 均原样成立。
- 触发：B2-B2c 开工前的范围闭包发现 ADR-037 §6 规定了**必须成立什么**，
  但没有定义**由哪个接口承载**，导致 B2-B2c 的 exact allowed files 无法闭合。

---

## 0. 为什么需要这条 ADR（ADR-037 §6 为什么不够）

ADR-037 §6 写死了五条性质：

1. 明文第一次出现只能在 render/finalization 边界的 resolver 返回
   `CredentialDTO` 时；
2. **复用已存在的 `SqlAlchemyCredentialResolver`，不新建**；
3. resolve 与 snapshot 必须在**同一个 Mihomo projection 命名锁 span 内**；
4. resolve 时读到的 `Secret.revision` 必须等于 snapshot 的
   `credential_revision`；
5. 不一致 ⇒ 候选作废、fail closed，**绝不用新明文配旧 fingerprint**。

**这五条都是性质，不是接口。** 实测当前仓库（`main` = `bb95ffc`）：

| 事实 | 位置 |
|---|---|
| `SqlAlchemyCredentialResolver.resolve(secret_ref)` 只返回 `CredentialDTO`，**不暴露 `Secret.revision`** | `credential_resolver.py:73-102` |
| 它内部**已经**做了 locking / `populate_existing` 的 fresh read，`stored.revision` 就在手里 —— **只是被丢弃了** | `credential_resolver.py:78`（`_current_secret_statement`） |
| `MihomoForwarderProvider.finalize(template, resolver: ControllerSecretResolver)` 只接受**一个** controller resolver | `mihomo.py:217-219` |
| `MihomoProjectionProvider` Protocol 的 `finalize` 同样只有 controller resolver | `mihomo_reconciliation.py:512-514` |
| `reconcile_mihomo_job()` 在 `resolver is None` 时只构造 `SqlMihomoControllerSecretResolver(db)` | `mihomo_reconciliation.py:1004-1005` |
| `ProjectionTemplate` 只携带 `(content, version, controller_secret_ref, controller_secret_revision)`，**没有任何 egress credential 需求** | `base.py:337-360` |
| `mihomo_projection.py` **零处**引用 `egress_proxies` | 实测 grep 无命中 |

⇒ **第 4 条（revision identity）在当前接口上根本无法实现**：finalize 拿不到
resolve 时的 revision。第 2 条（复用而不新建）与第 3 条（同一锁 span）
也没有被任何接口机械保证。

> **这条缺口的性质**：在 `test_mihomo_projection.py` 里造一个 fake resolver
> 可以让 B2-B2c 的五条测试全绿，但 **production path 依然不满足 ADR-037 §6**。
> 那是假绿，**不接受**。本 ADR 的存在就是为了让"绿"等于"真的接线了"。

---

## 1. 裁决：**组合（composition），不是扩展**

**egress credential resolver 与 controller secret resolver 是两个独立契约，
由一个 frozen bundle 组合后一并交给 `finalize()`。**

### 1.1 为什么不扩展 `ControllerSecretResolver`（逐条理由，不要重开）

| # | 理由 |
|---|---|
| 1 | **purpose 绑定相反**。controller secret 是 **purpose-bound**（`reveal_secret_snapshot_for_purpose(..., MIHOMO_CONTROLLER_API_SECRET)`，`credential_resolver.py:42-44`）；egress credential 是 **purpose-unbound**（ADR-019 §7 与 ADR-037 §3.3 明写「purpose 不绑定」，与 Xray 读同一行）。合并成一个 resolver，必然把其中一条弄错 |
| 2 | **ADR-037 §8 明确禁止**改 `SqlAlchemyCredentialResolver` 的 purpose-unbound 现状 —— 它与 Xray 路径共用。把 egress 塞进 controller resolver 会反向污染 controller 的 purpose 绑定 |
| 3 | **返回形状不同**。`ControllerSecretSnapshot` 是单值 `value: str`；egress 是 `(username, password)` 两值。强行统一要引入 union 或 `Any`，反而变成不可校验的形状 —— 正是 ADR-037 §7 否决「往无类型 mapping 里加键」的同一条理由 |

### 1.2 为什么是**一个 bundle 参数**而不是两个并列参数

- ADR-037 §6 要求两次 resolve 都落在**同一个命名锁 span** 内。一个 frozen
  bundle 让"这两个 resolver 同生共死、绑在同一个 Session 上"成为**一个对象
  的生命周期**，而不是两个参数各自的偶然；
- `MihomoProjectionProvider.finalize()` 保持 **2 参数协议**，
  `reconcile_mihomo_job()` 的调用点只改名不改形状；
- 将来再加第三个 resolver 不需要再动一次签名。

---

## 2. 精确接口（**唯一结构，不得替换，不得让执行者重新设计**）

### 2.1 `backend/app/providers/base.py`

```python
@dataclass(frozen=True, slots=True)
class EgressCredentialRequirement:
    """render 阶段声明的一条待解析凭据；只有 opaque ref 与 revision。"""
    proxy_name: str
    secret_ref: str = field(repr=False)
    revision: int


@dataclass(frozen=True, slots=True)
class EgressCredentialSnapshot:
    """finalize 阶段拿到的明文 + 与之同一次 fresh read 的 revision。"""
    secret_ref: str = field(repr=False)
    revision: int
    credential: CredentialDTO = field(repr=False)


class EgressCredentialResolver(Protocol):
    def resolve_egress_credential(self, secret_ref: str) -> EgressCredentialSnapshot: ...
```

`ProjectionTemplate` 增加一个**带默认值**的字段与只读属性：

```python
class ProjectionTemplate(CandidateConfig):
    def __init__(
        self,
        content: Mapping[str, object],
        version: str,
        controller_secret_ref: str,
        controller_secret_revision: int,
        egress_credentials: tuple[EgressCredentialRequirement, ...] = (),
    ) -> None: ...

    @property
    def egress_credentials(self) -> tuple[EgressCredentialRequirement, ...]: ...
```

> **默认值 `= ()` 是刻意的**，与 ADR-037 §1a.4 给 `XrayOutboundDTO` 的处理
> 同形：`ProjectionTemplate` 在仓库里的构造点只有
> `mihomo.py:215` 与 `test_mihomo_projection.py:672`（实测），默认值让后者
> 无需改动即可继续通过。**若实现时发现还有别的构造点必须改，停下来上报。**

### 2.2 `backend/app/providers/forwarder/mihomo.py`

```python
@dataclass(frozen=True, slots=True)
class MihomoFinalizationResolvers:
    controller: ControllerSecretResolver
    egress: EgressCredentialResolver


class MihomoForwarderProvider(ForwarderProvider):
    def finalize(
        self, template: ProjectionTemplate, resolvers: MihomoFinalizationResolvers
    ) -> MihomoCandidateConfig: ...
```

`finalize()` 对 `template.egress_credentials` 的**每一条**：

1. `snapshot = resolvers.egress.resolve_egress_credential(req.secret_ref)`；
2. `snapshot.secret_ref != req.secret_ref` ⇒
   **`MIHOMO_EGRESS_CREDENTIAL_UNRESOLVED`**；
3. `snapshot.revision != req.revision` ⇒
   **`MIHOMO_EGRESS_CREDENTIAL_REVISION_MISMATCH`**（这就是 ADR-037 §6
   第 4/5 条的落点）；
4. 通过后把 `username` / `password` 写进 `content["proxies"]` 里
   `name == req.proxy_name` 的那一条；找不到或找到多条 ⇒
   **`MIHOMO_EGRESS_CREDENTIAL_UNRESOLVED`**。

**任一条不满足 ⇒ 整份候选作废、fail closed**，不得产出"某个出口没有凭据"
的部分配置（铁律 1）。controller secret 的既有校验
（`mihomo.py:222-232`）**一字不改**，与上面这段并列执行。

### 2.3 `backend/app/infra/credential_resolver.py` —— **复用，零复制**

**不新建 resolver 类**（ADR-037 §6 第 2 条）。在**现有**
`SqlAlchemyCredentialResolver` 上加一个方法，并让现有 `resolve()`
**委托**给它：

```python
class SqlAlchemyCredentialResolver(XrayRenderResolver):

    def resolve_egress_credential(self, secret_ref: str) -> EgressCredentialSnapshot:
        # 现有 resolve() 的完整实现原样搬到这里，唯一的增量是：
        #   - 额外校验 stored.revision 为正整数（非法 ⇒ CREDENTIAL_MALFORMED）
        #   - 返回 EgressCredentialSnapshot(secret_ref, stored.revision, CredentialDTO(...))
        ...

    def resolve(self, secret_ref: str) -> CredentialDTO:
        return self.resolve_egress_credential(secret_ref).credential
```

> **这是本 ADR 里最重要的一条实现约束。** 解密与校验逻辑
> （`_current_secret_statement` 的 locking fresh read、`decrypt_secret`、
> payload 形状校验、四类 `CredentialResolutionError` 分支）
> **在仓库里只能存在一份**。`resolve()` 变成一行委托之后，
> **Xray 路径的行为逐字节不变** —— 它拿到的仍是同一个 `CredentialDTO`，
> 走的仍是同一段代码。
>
> **禁止**：新建 `SqlAlchemyEgressCredentialResolver` 类、
> 复制一份解密逻辑、或用继承/覆盖绕开这条委托。

### 2.4 `backend/app/infra/mihomo_reconciliation.py` —— **必须改**

三处，缺一不可：

```python
class MihomoProjectionProvider(Protocol):
    def finalize(
        self, template: object, resolvers: MihomoFinalizationResolvers
    ) -> ProjectionCandidate: ...


def reconcile_mihomo_job(
    provider: MihomoProjectionProvider,
    loader: MihomoDesiredSnapshotLoader | None,
    resolvers: MihomoFinalizationResolvers | None,      # ← 由 resolver 改名改型
    verifier: ProjectionVerifier | None,
    *,
    db_factory: Callable[[], Session] = SessionLocal,
) -> MihomoReconciliationResult | None:
    with db_factory() as db, mihomo_projection_write(db):
        ...
        if resolvers is None:
            resolvers = MihomoFinalizationResolvers(
                controller=SqlMihomoControllerSecretResolver(db),
                egress=SqlAlchemyCredentialResolver(db),      # ← 同一个 db
            )
        ...
        candidate = provider.finalize(template, resolvers)     # ← 调用点
```

> **ADR-037 §6 第 3 条（同一命名锁 span）的机械保证就在这里**：两个 resolver
> 都用 `with db_factory() as db, mihomo_projection_write(db):` 里的**那一个
> `db`**。锁由 `mihomo_projection_write(db)` 在同一 Session 上持有，
> 所以"resolve 与 snapshot 同 span"不是靠约定，是靠**它们物理上没有第二个
> Session 可用**。
>
> `session_holds_mihomo_projection_lock(session)`
> （`mihomo_projection_lock.py:157`）**已经存在**，测试可以直接用它断言
> 解密发生的时刻锁确实被持有 —— 见 §4。

---

## 3. 不属于本 ADR / 不授权

- **不改 `SqlAlchemyCredentialResolver` 的 purpose-unbound 现状**
  （ADR-037 §8 原样保留）。`resolve_egress_credential()` 与 `resolve()`
  查的是同一行、同样不绑 purpose。
- **不改 `ControllerSecretResolver` / `ControllerSecretSnapshot` 的任何形状。**
- **不改 `SqlMihomoControllerSecretResolver` 的行为。**
- **不把 `ControllerSecretResolver` 搬到 `providers/base.py`。** 它当前在
  `providers/forwarder/mihomo.py`，位置可议但不在本次范围 —— 搬它会把
  `credential_resolver.py` 的 import 方向一起翻掉，属独立重构。
- **不重开 ADR-037 已裁定的任何行为**：`dialer-proxy` 是唯一链式编码；
  `{socks, socks5}` → Mihomo `socks5`；其余协议
  `MIHOMO_EGRESS_PROTOCOL_UNSUPPORTED` fail closed；跨来源重名复用
  `MIHOMO_DUPLICATE_PROXY_IDENTITY`；无 ACTIVE assignment 时
  **整个 `dialer-proxy` 键省略**（不是空值）；明文不进
  loader / manifest / generation row / `repr()` / 日志。
- **不授权生产激活**，不授权 `build_registry()` 放行
  `FORWARDER_PROVIDER=mihomo`，不授权部署。
- **`metacubex/mihomo:v1.19.27` 对 `dialer-proxy` 的实测要求原样保留**
  （ADR-037 §2.4）：B2-B2c 的 candidate validation 必须确认它被接受；
  **不被接受则停止并报告 User，不得自行改用其它编码。**

---

## 4. Testing requirements（精确名与断言见 TASK-S04 的 B2-B2c 测试表）

本 ADR 只规定**必须被机械证明的三件事**，测试名归 TASK：

1. **revision freshness 真的被校验**：snapshot 之后、finalize 之前把
   `Secret.revision` 由 N 改为 N+1（并改明文）⇒ finalize 抛
   `MIHOMO_EGRESS_CREDENTIAL_REVISION_MISMATCH`，且
   **`provider.apply()` 从未被调用**，新明文不出现在 job / blocker /
   日志的任何位置。
2. **production wiring 真的接上了，不是 fake**：调用
   `reconcile_mihomo_job()` 时 **`resolvers=None`**（不注入任何 resolver），
   断言候选 document 里 proxy 的 `username` / `password` 等于库里那条真实
   `Secret` 解密出来的值。**这条只有默认接线真的工作才能过** ——
   它是本 ADR 针对"假绿"的唯一机械防线。
3. **解密确实发生在锁内**：monkeypatch `decrypt_secret`，在被调用的那一刻
   断言 `session_holds_mihomo_projection_lock(db)` 为 `True`。

---

## 5. 重新评估条件

- Mihomo 支持不含明文的 credential reference（届时 §2.2 的注入步骤可去掉，
  与 ADR-019 §4 末条、ADR-037 §10 末条同理）；
- 住宅出口改为不需认证，或改用非 socks5 协议；
- egress credential 需要 purpose 绑定 —— 那会同时影响 Xray 路径，
  必须单独评估（ADR-037 §8 已点名）；
- 出现第三个需要在 finalization 边界解析的 secret 类别 ——
  `MihomoFinalizationResolvers` 加字段即可，不需要推翻本 ADR。
