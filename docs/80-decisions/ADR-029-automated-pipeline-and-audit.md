# ADR-029: 全自动流水线（Issue 触发 → Codex 实现 → ChatGPT 审查 → 自动合并）+ 定时架构审计与断路器

- 状态: **已接受（Accepted）**
- 日期: 2026-09-18
- 决策范围: `AGENTS.md` 铁律 8（合并永远人工）、模块写权限表、
  `.github/workflows/`、以及 Claude Code 的介入时机。
- 触发来源: User 明确要求全自动化，并在 Claude 列出风险后**重申选择全开**。

## 这条 ADR 撤销的东西

**`AGENTS.md` 铁律 8 的「合并永远人工」自本 ADR 起不再适用于业务 PR。**
按 `AGENTS.md` 自己写的例外条款——「不得在没有 User 重新明确要求的情况下
重新引入任何形式的自动合并」——User 已重新明确要求，本 ADR 即是那次要求的
记录。

铁律 8 的其余部分（不得直接 push main、不得 force push）**全部保留**。

## 上一次为什么失败，这次的风险为什么不同

诚实记录，不粉饰：

`.github/workflows/claude-automerge.yml`（TASK-T18）存在过并被 User 撤销。
失败模式是：审查连续多轮给出新的、有效的 `NEEDS_CHANGES`，PR 长期卡在返工
循环里，**从未真正走到过自动合并那一步**，只消耗审查/修复轮次。

**那次的失败是「太保守、走不到合并」。这次的风险是反过来的：它可能真的合并，
并且合并错的东西。** 两者是不同的失效方向，不能用「上次没出事」来推断这次
安全。

Claude 在决策前提出的具体反对意见，一并记录（User 已知悉并选择全开）：

1. **ChatGPT 将审查自己规格下产出的代码，main 之前再无独立检查。**
   流程变成 ChatGPT 写 TASK → Codex 实现 → ChatGPT 审查 → 自动合并，
   一个判断决定一切，而该判断**只能读代码，不能运行代码**。
2. **已有实证表明只读审查会漏掉跨 PR 的交互缺陷。** 2026-09-18 发现：
   `/subscriptions/{id}/renew` 能创建 `RENEWAL` 订单，而
   `admin_confirm_payment` 拒绝一切非 `PURCHASE` 订单，导致**客户可以点出
   一个永远无法被收款确认的订单**。该缺陷跨越多个已合并 PR 与多轮审查，
   无人靠读 diff 发现；它不在任何单个 diff 里，而在两个从未同时出现在一个
   diff 中的文件的交互里。**自动合并会把那些 PR 全部放行。**

本 ADR 因此把安全重心从「合并前的人工闸门」移到「合并后的检测 + 断路器」。
**这是姿态的实质变化，不是等价替换**：问题会在进入 `main` 之后才被发现。

## 决策

### 1. 流水线

1. ChatGPT 读 `docs/85-agent-operating-model.md` §5 派发队列，取第一优先级任务。
2. ChatGPT 建 GitHub Issue，内容**只有一句触发指令**，形如：
   `@codex 请执行 docs/82-tasks/TASK-S07-order-queue-billing.md，严格按仓库 AGENTS.md 与 docs/85 §2.3 执行。`
3. Codex 建分支、实现、跑测试、提 PR。
4. PR 事件（新建 / 新 commit / Draft→Ready）触发 ChatGPT 审查。
5. ChatGPT 按固定格式审查当前完整 head SHA：
   `Critical / Major / Minor / Checks performed / Verdict`。
6. `NEEDS_CHANGES` → 在 PR 评论 `@codex ...` 要求核实并修复，推同一个 PR。
7. Codex 推新 commit → 再次触发审查。**最多 5 轮**（沿用既有上限）。
8. `PASS` **且**同一 head SHA 上 CI/Security/Risk 全绿 → 自动合并。
9. 合并后 ChatGPT 重读三份核心文件与 §5，创建下一项任务 Issue。

### 2. Issue 是触发器，TASK 是权威

**Issue 里不得重写 TASK 内容**，只允许一句指向 TASK 文件的触发指令。
需求与验收标准的唯一记录载体仍然是 `docs/82-tasks/TASK-*.md`
（`AGENTS.md`「协作角色与职责」不变）。

这一条是为了避免同一需求出现两份、互相漂移——本仓库已经因为两份真相源
吃过多次亏。

### 3. 谁来执行合并

**GitHub workflow 执行合并，不是 Codex，也不是 ChatGPT。**

- Codex 写代码；
- ChatGPT 判断；
- GitHub 在条件全部满足时机械地合并。

不依赖「评论一句 `@codex merge`」——该动作未见官方文档保证为受支持的标准
merge 行为，不能作为流水线的关键环节。

### 4. 自动合并的**全部**前置条件（缺一不可，且必须落在同一个 head SHA 上）

1. 存在一条 `Verdict: PASS` 的审查，且其标注的 head SHA **等于**当前 head SHA；
2. 该 head SHA 上全部必需 check 成功（CI / Security / Risk）；
3. 无合并冲突；
4. 仓库根的 `.github/automerge-enabled` 存在且内容为 `true`（断路器，见第 5 条）；
5. PR **未**带 `no-automerge` 标签；
6. PR **不是**架构审计 PR（分支前缀 `claude/audit-`）——审计者不得自己批准
   自己的结论。

任一条不满足即不合并，且**不得**通过重推、空提交、关闭重开等方式绕过。

### 5. 断路器：`.github/automerge-enabled`

一个版本受控的开关文件。删除它、或把内容改成 `false`，自动合并立即停止。

**谁可以拉闸**：User、ChatGPT、以及定时架构审计（见第 6 条）。拉闸走一个
普通 PR，因此本身有记录、可追溯、可回滚。

没有断路器的全自动合并，失效形态是「二十个坏 PR 之后才有人发现」。有断路器
就是可恢复的。**这是让全自动可生还的关键部件，不是可选装饰。**

### 6. 定时架构审计

> **⚠️ 2026-09-19：本节已由 ADR-034 整节撤销。**
> 「每 6 小时定时审计」在 ADR-033 就已停止（其 Routine 载体先已被实测证明
> 失效）；**本节末尾那三个"强制人工触发"的节点——接线真实外部系统前、
> 生产部署前、一条工作线完成后——由 ADR-034 一并取消。**
> **Claude 的审查现在只有一个触发方式：User 开口。**
>
> **未被撤销的**：本节关于**审查该怎么做**的行为契约（必须实际跑
> lint/mypy/测试并贴真实输出、重点查 diff 范围看不见的四类问题、按严重度
> 处置、审计 PR 不自动合并）在 User 触发审查时**仍然全部适用**；
> `docs/83` §10 的审计记录表也继续保留（ADR-034 §4.2）。
>
> **尤其注意**：取消的是"必须让 Claude 审一遍"，**不是**"必须有人批准"。
> `AGENTS.md`「禁止自主执行」整张表、「灰区」条款、生产部署的人工闸门
> **一个字没动**——论证见 ADR-034 §2。

Claude Code 由**定时任务**触发（每 6 小时），作为 `main` 之后**唯一**的独立
检查。行为契约：

- **先判断要不要做**：若 `main` 自上次审计前进 < 5 个提交、且未触及
  `backend/app/domain/`、`backend/app/providers/`、
  `infrastructure/alembic/versions/`、`deploy/` 或支付/凭据路径，直接跳过，
  不产出任何 PR。
- **必须实际跑** lint / mypy / 测试并贴出真实输出，不得只读代码下结论。
- **重点查 diff 范围审查必然漏掉的东西**：跨 PR 的交互缺陷、实现与已接受
  ADR 的漂移、记忆文件过期或自相矛盾、铁律被绕过、补偿路径是否仍 fail-closed。
- **按严重度处置**：
  - Critical（数据损坏 / 凭据泄漏 / 客户可踩到的业务错误 / 铁律被绕过）
    → **立即拉闸**并明确告知 User；
  - Major → 写 ADR / TASK，更新 §5，不拉闸；
  - Minor 或无发现 → 只更新审计记录。
- **每次都要**在 `docs/83-project-continuity.md` 末尾更新「最近一次自动审计」：
  日期、审计的 `main` SHA、跑了什么、发现了什么、是否拉闸。
- **审计 PR 自己不自动合并**（第 4 条第 6 项）。

除定时之外，以下节点**强制**人工触发一次审计，不得省略：

- 接线任何真实外部系统之前（Webshare / Mihomo 生产激活）；
- 任何生产部署之前；
- 一条工作线（如整个 S04）完成之后。

### 7. 模块写权限的调整

`AGENTS.md` 写权限表此前规定 `docs/80-decisions/**`、`docs/81-reviews/**`、
`docs/82-tasks/**` 为 **Claude Code 独占**，而
`docs/85-agent-operating-model.md` 的角色表却写了 ChatGPT 负责「写 TASK 文件」。
**这是 Claude 在 2026-09-18 引入的矛盾**，按权威顺序（ADR > AGENTS.md > 85）
应以 AGENTS.md 为准。

本 ADR 予以裁定：

| 路径 | 写入者 |
|---|---|
| `docs/80-decisions/**`（ADR） | **Claude Code 独占** |
| `docs/81-reviews/**` | **Claude Code 独占** |
| `docs/82-tasks/**`（TASK） | **Claude Code 独占** |

ChatGPT 的职责是**指挥与审查**：读 §5、建 Issue 触发 Codex、审查 PR。
**需要新 TASK 或新 ADR 时，由 ChatGPT 提出需求，Claude Code 撰写。**

理由：TASK 与 ADR 是这条流水线唯一的权威约束来源。让下达任务的人同时定义
约束自己的规则，会让「允许修改的文件」这类护栏失去意义——而那正是目前防
范围蔓延最有效的一条。

## 不做的事

- 不允许 Codex 自行决定合并。
- 不允许在 Issue 里重写 TASK 内容。
- 不放宽 5 轮返工上限。
- 不放宽铁律 1–7，也不放宽「不得直接 push main / 不得 force push」。
- 不放宽凭据边界、禁止自主执行清单、灰区必须停下询问。
- **不自动合并生产部署**。`deploy-*.yml` 的 `DRY_RUN=true` 闸门与人工批准
  不受本 ADR 影响。

## 重新评估条件

- **只要定时审计发现过一次 Critical，就必须重新评估本 ADR**，而不是拉完闸
  再开回去了事。反复出现 Critical 说明「只读审查 + 事后检测」这个组合不足以
  替代人工闸门。
- 若 5 轮上限被频繁触顶（说明流水线在空转，与上一次失败同形），重新评估。
- 若 ChatGPT 出现过一次「PASS 但实际有 Critical」的误判，重新评估第 4 条的
  条件是否足够。

## 验证要求

实现本 ADR 的 PR（TASK-S08）必须证明：

1. 缺少 `.github/automerge-enabled` 时**不合并**；内容为 `false` 时**不合并**。
2. 审查 SHA 与当前 head SHA 不一致时**不合并**（stale review 不生效）。
3. 任一必需 check 非 success 时**不合并**。
4. `no-automerge` 标签存在时**不合并**。
5. `claude/audit-` 前缀分支的 PR **不合并**。
6. 以上每一条都要有可复现的验证方式，不接受「逻辑上应该成立」。
