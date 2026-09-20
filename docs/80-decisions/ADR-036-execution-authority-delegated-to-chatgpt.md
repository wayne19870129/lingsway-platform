# ADR-036: 取消每阶段回 Claude 的固定卡点；日常执行权归 ChatGPT

- 状态: **已接受（Accepted）**
- 日期: 2026-09-20
- 决策范围: `AGENTS.md` 第 99 / 112 行的 `docs/82-tasks/**` 独占写权限、
  `docs/85-agent-operating-model.md` §0 角色表 / §2.1 / §3 返工段 / §4.1
  （何时叫 Claude）/ §5（派发队列归属）、`docs/83-project-continuity.md`
  §2 的角色分工。
- 触发来源: User 的明确决策（2026-09-20）——
  **「取消每个执行阶段必须回 Claude 确认的固定卡点。」**
- 承接: ADR-032（派活交回 ChatGPT）、ADR-033 / ADR-034（审查只由 User 触发）。
  本 ADR 是这条线上的最后一步：**把"规格"也一并下放**。
- 不涉及: 本 ADR 与 ADR-035（Mihomo deployment 常量落点）无关，
  也不改变 S04 的任何技术结论。

## 1. 决定

**ChatGPT 在已接受的架构与计划之内，拥有完整的日常执行权，
不需要回来找 Claude 确认。**

### 1.1 四方职责（User 2026-09-20 的原始划分，逐条落文）

| 角色 | 负责 |
|---|---|
| **Claude** | 整体架构；总体计划；**ADR**；**User 主动触发时**做阶段性审核 / 调整。**不设置每 checkpoint 强制回来确认的卡点。** |
| **ChatGPT** | 按已接受架构 / 计划持续推进；拆解执行阶段；**写 TASK 文件**；安排 Codex；审查 Codex 的 PR；**处理返工**；**一个阶段完成后直接进入已有计划中的下一阶段** |
| **Codex** | 执行实现；测试；提 PR；按 review 指令返工；**不自行扩大架构范围** |
| **User** | 最终决策；**人工合并**；决定什么时候叫 Claude 做整体审核或架构调整 |

### 1.2 具体变更

| 事项 | 之前 | 本 ADR 之后 |
|---|---|---|
| 写 `docs/82-tasks/TASK-*.md` | Claude 独占（`AGENTS.md` 99 / 112） | **ChatGPT**（Claude 也可写，但不再是必经之路） |
| "要不要先有 TASK 文件"这道门槛 | 卡在 Claude | **ChatGPT 按 `docs/85` §2.1 自己判** |
| 拆任务、定粒度、排顺序 | ChatGPT（ADR-032） | ChatGPT，不变 |
| 派活给 Codex、审查 Codex 产出、处理返工 | ChatGPT（ADR-031 / 032） | ChatGPT，不变 |
| 维护 `docs/85` §5 派发队列 | Claude | **ChatGPT**（它才是读写这张表的人） |
| **一个执行阶段完成之后** | 回来找 Claude 确认架构 / 计划 | **不用。直接进入已有计划中的下一阶段** |
| 写 ADR（`docs/80-decisions/**`） | Claude 独占 | **Claude 独占，不变** |
| 写 REVIEW（`docs/81-reviews/**`）、`ARCHITECTURE.md` | Claude 独占 | **Claude 独占，不变** |
| 叫 Claude 审查 | 只由 User 触发（ADR-034） | 只由 User 触发，**不变** |
| 合并 | User 人工合并（断路器 `false`） | **不变** |

**"需要先有 TASK 文件"这条门槛本身也一并放开。** TASK 仍然是需求与验收标准
的唯一记录载体（`AGENTS.md` 流程段，未改），**但不再是一个必须等 Claude 才能
跨过的闸门**。

## 2. 唯一保留的架构停止条件：去找 User，不是找 Claude

`AGENTS.md` 铁律 5 仍然成立：**改变一条已接受的架构决定，必须先有 ADR**，
而 ADR 仍由 Claude 独占撰写。本 ADR 不动这一条，也无权动它——那是本仓库最后
一道防止执行者自我授权的结构性约束。

**但路由变了：**

> **ChatGPT 判断"这件事必须改变一条已接受 ADR 的结论"时：
> 1. 停止自行推进该架构变化；
> 2. 报告给 User；
> 3. 是否调用 Claude，由 User 决定。**

**这不是恢复旧流程。** 旧流程是"每个 checkpoint 自动找 Claude"；本条只在
**真的要推翻一条已接受 ADR** 时触发，而且终点是 User，不是 Claude。
与 ADR-034"Claude 的唤醒只有 User 开口这一个入口"一致。

**判据（别把缺口读大了）**：只有**必须改变某条已接受 ADR 的结论**才算架构
缺口。以下都**不是**，ChatGPT 自己定：任务怎么切、指令卡多细、先做哪个、
允许改哪些文件、某个实现细节选哪种写法、要不要补测试、TASK 写不写、写多长、
返工怎么处理、一个阶段完成后先做哪一个下一阶段。

## 3. 本 ADR 覆盖 `AGENTS.md`，并且**没有**修改 `AGENTS.md`

`AGENTS.md` 自己规定的冲突次序是 **ADR > AGENTS.md > REVIEW**，
`CLAUDE.md` 开头复述了同一条：“an accepted ADR under `docs/80-decisions/`
can intentionally override `AGENTS.md`”。本 ADR 就是按那条次序行使的覆盖。

**`AGENTS.md` 本身一个字没改**——它规定修改自身需要 User 确认，Claude 不
自行动它。**`CLAUDE.md` 同样未改**（同一条人工确认要求）。后果是：

> **`AGENTS.md` 第 99 行（`docs/82-tasks/** → Claude Code 独占`）与第 112 行
> （“需要新 TASK 或新 ADR 时仍由 Claude Code 撰写”）从本 ADR 起读起来是
> 过时的。** 以本 ADR 为准。
>
> **`CLAUDE.md`「Before implementation」第 3 条**的
> “consider whether one should be written first (Claude's exclusive lane per
> `AGENTS.md`)”**同样过时**，以本 ADR 为准。

User 若愿意把两个文件改干净，需要改的就是这三处：`AGENTS.md` 第 99 行改为
`docs/82-tasks/**  → ChatGPT / Claude Code 均可写`，第 112 行的“新 TASK”
三个字删掉、只留“新 ADR 仍由 Claude Code 撰写”，`CLAUDE.md` 那句括号换成
“ChatGPT 或 Claude 均可写（ADR-036）”。**不改也不影响本 ADR 生效**——
只是仓库里会有三处已知过时的表述，此处已逐条记录，不算隐性双事实来源。

## 4. 代价：规格与审查的独立性没有了

**必须明写，因为 `AGENTS.md` 103–106 与 137 行专门论证过这条独立性"不是
形式"**，本 ADR 正是把它拿掉：

- 原论证是：**ChatGPT 审查 Codex 时，对照的是一份它没有写的 TASK**。
  ADR-029 记录的第 1 条防线就建立在这上面。
- 本 ADR 之后，ChatGPT **写规格、派活、又审查**。它审的是自己的规格，
  **"规格写错了"这一类缺陷因此失去了独立的发现者**。
- 这类缺陷是真实发生过的，不是假想：S07 的「允许修改的文件」**连续漏项两次**
  （`docs/85` §5.0），S11 的 TASK 漏掉 `risk-classify.yml` 没有 checkout 这个
  前提（§5.3b）。**这三次都不是被审查抓住的**——两次被 CI 抓住，一次被执行者
  按规则停住上报。

**还剩下什么（这些是本 ADR 依然成立的理由，不是安慰）**：

1. **实现者仍然是另一个 agent。** 规格写得含糊，Codex 仍会照字面执行并把
   矛盾暴露出来，或按规则停住上报——S11、S07 各出现过一次。
2. **ADR 仍是 Claude 独占。** ChatGPT 无法自我授权改变架构结论。
3. **每一个 PR 都由 User 亲手合并。** 自动合并断路器自 PR #158 起为 `false`，
   这是**机械成立**的，不是自觉。
4. **9 个必需状态检查 + `main` 的 ruleset。** 上面三次里有两次是 CI 抓住的。
5. **User 不定时叫 Claude 做阶段性审核 / 调整。** 频率由 User 定；本 ADR 不给
   它任何固定节奏，也不给任何"到了某个节点就必须审"的规则。

**这笔交换是 User 明确选择的：用规格-审查独立性换吞吐量。** 记在这里是为了
以后出问题时能对上原因，不是为了留一个翻案的伏笔。

**因此有一条操作要求随之加重**：ChatGPT 写 TASK 时的**范围闭包检查**必须
沿四条轴各走一遍——被改的**函数 / 协议**、被改的**数据库列**、被改的
**API 契约**、被改的**渲染字段**。只沿一条轴查出来的是"这条轴上闭合"，
不是"闭合"（`docs/85` §5.0，S07 第二次漏项正是这么来的）。

## 5. Claude 不在任何后台自动运行（已核实的事实，不是承诺）

User 的要求：**不设置任何 Claude Routine、schedule、自动唤醒或定期审查。**

**2026-09-20 实测，本仓库与账号下不存在任何会自动消耗 Claude token 的东西**：

| 载体 | 实测状态 |
|---|---|
| Claude 侧 Routines / 定时唤醒 | **一个都没有** |
| `.github/workflows/claude.yml` | 触发器只有 `issue_comment`，且条件是评论作者为 User **且**评论含 `@claude`。**没有 schedule** |
| `.github/workflows/pipeline-health.yml` | 每 2 小时跑，但**不含任何 LLM、不含任何 Claude 凭据**（文件头写明这是刻意的设计约束）。它只把流水线状态压成一个 JSON，**Claude token 消耗为零** |
| `.github/workflows/security.yml` | 每周一次，纯扫描工具，无 LLM |
| ADR-029 §6 的定时架构审计 | **已于 ADR-034 整节撤销**，载体也早已失效 |

**结论：没有需要关掉的后台 Claude。** 本 ADR 把这条状态固定下来：

> **Claude 的每一次运行都由 User 发起**——对话里开口，或在 PR / Issue 里
> `@claude`。**没有定时器，没有事件触发，没有"某个节点必须叫 Claude"的规则。**
> **本 ADR 禁止新增任何 Claude 侧的 Routine / schedule / 自动唤醒 / 定期审查。**

`pipeline-health.yml` 每 2 小时消耗的是 GitHub Actions 分钟数，不是 token；
要不要关是 User 的成本取舍，**本 ADR 不动它**。

## 6. 不受本 ADR 影响的既有原则

- **一任务一 PR**（`CLAUDE.md`）——**保留**。本 ADR 自己就是按这条从
  PR #161 拆出来单独走的。
- **`AGENTS.md`「禁止自主执行」整张表**——一个字没动。
- **凭据与密钥边界**——一个字没动。
- **自动合并断路器 `.github/automerge-enabled` = `false`**——不动，不开。
- **生产部署仍是人工的**，`DRY_RUN` 闸门未变。
- **ADR-035 与 S04 的全部技术结论**——本 ADR 与之无关，未触及。

## 7. 重新评估条件

- 连续出现**由规格缺陷导致**的返工，且 CI 与执行者都没能拦住——那正是 §4
  拿掉的那道防线在起作用的证据，届时需要重新权衡；
- User 要求恢复 TASK 的独占撰写；
- `AGENTS.md` 铁律 5 被改变（ADR 不再是架构变更的前置）——那会让 §2 唯一的
  停止条件失效，必须先处理。

## 8. 本 ADR 不授权什么

不放宽 `AGENTS.md`「禁止自主执行」整张表；不放宽凭据与密钥边界；不开启
自动合并；不授权任何人改 `AGENTS.md` 或 `CLAUDE.md`；不授权生产部署；
不授权 ChatGPT 或 Codex 自行改变任何已接受 ADR 的结论。
**下放的是规划与执行的决定权，不是安全边界。**
