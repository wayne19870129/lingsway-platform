# ADR-036: 执行权全部下放给 ChatGPT；Claude 只做架构与 User 触发的阶段性审核

- 状态: **已接受（Accepted）**
- 日期: 2026-09-20
- 决策范围: `AGENTS.md` 第 99 / 112 行的 `docs/82-tasks/**` 独占写权限、
  `docs/85-agent-operating-model.md` §4（何时叫 Claude）与 §5（派发队列）、
  `docs/83-project-continuity.md` §2 的角色分工。
- 触发来源: User 的明确决策（2026-09-20）——“**别设置卡点啦，把权利都下放给
  ChatGPT。你只负责整体架构设计和阶段性审核，这个我会不定时让你执行。让
  ChatGPT 能够完全自主的根据你的架构和计划执行，而不是做一段任务之后，又得
  回来找你确认架构和计划。**”
- 承接: ADR-032（派活交回 ChatGPT）、ADR-033 / ADR-034（审查只由 User 触发）。
  本 ADR 是这条线上的最后一步：**把“规格”也一并下放**。

## 1. 决定

**ChatGPT 在已接受的架构之内，拥有完整的规划与执行权，不需要回来找 Claude
确认。**

| 事项 | 之前 | 本 ADR 之后 |
|---|---|---|
| 写 `docs/82-tasks/TASK-*.md` | Claude 独占（`AGENTS.md` 99 / 112） | **ChatGPT**（Claude 也可写，但不再是必经之路） |
| 拆任务、定粒度、排顺序 | ChatGPT（ADR-032） | ChatGPT，不变 |
| 派活给 Codex、审查 Codex 产出 | ChatGPT（ADR-031 / 032） | ChatGPT，不变 |
| 维护 `docs/85` §5 派发队列 | Claude | **ChatGPT**（它才是读写这张表的人） |
| 一段任务做完之后 | 回来找 Claude 确认架构/计划 | **不用**。直接开下一段 |
| 写 ADR（`docs/80-decisions/**`） | Claude 独占 | **Claude 独占，不变** |
| 写 REVIEW（`docs/81-reviews/**`）、`ARCHITECTURE.md` | Claude 独占 | **Claude 独占，不变** |
| 叫 Claude 审查 | 只由 User 触发（ADR-034） | 只由 User 触发，**不变** |

**“需要先有 TASK 文件”这条门槛本身也一并放开**：ChatGPT 按 `docs/85` §2.1
自己判断哪些任务值得落 TASK 文件。它仍然是唯一的需求记录载体（`AGENTS.md`
流程段），**但不再是一个必须等 Claude 才能跨过的闸门**。

## 2. 遇到真正的架构缺口怎么办：去找 User，不是找 Claude

这是本 ADR 唯一保留的停止条件，而且它**不指向 Claude**。

`AGENTS.md` 铁律 5 仍然成立：**改变一条已接受的架构决定，必须先有 ADR**，
而 ADR 仍由 Claude 独占撰写。本 ADR 不动这一条，也无权动它——那是本仓库
最后一道防止执行者自我授权的结构性约束。

但路由变了：

> **ChatGPT 判断“这件事必须改架构”时，把它交给 User，由 User 决定要不要
> 叫醒 Claude。ChatGPT 不再直接“回来找 Claude”，Claude 也不再因为出现缺口
> 就自动介入。**

**这不是新增卡点**，它是把原来“ChatGPT → Claude → 继续”的往返，换成
“ChatGPT → User”，与 ADR-034“Claude 的唤醒只有 User 开口这一个入口”一致，
也与 User 本次的“**你的唤醒和执行任务完全由我来决定**”一致。

**判据（别把缺口读大了）**：只有当一件事**必须改变某条已接受 ADR 的结论**
时才算架构缺口。以下都**不是**，ChatGPT 自己定：任务怎么切、指令卡多细、
先做哪个、允许改哪些文件、某个实现细节选哪种写法、要不要补测试、
TASK 写不写、写多长。

## 3. 本 ADR 覆盖 `AGENTS.md`，并且**没有**修改 `AGENTS.md`

`AGENTS.md` 自己规定的冲突次序是 **ADR > AGENTS.md > REVIEW**，
`CLAUDE.md` 开头复述了同一条：“an accepted ADR under `docs/80-decisions/`
can intentionally override `AGENTS.md`”。本 ADR 就是按那条次序行使的覆盖。

**`AGENTS.md` 本身一个字没改**——它规定修改自身需要 User 确认，Claude 不
自行动它。后果是：

> **`AGENTS.md` 第 99 行（`docs/82-tasks/** → Claude Code 独占`）与第 112 行
> （“需要新 TASK 或新 ADR 时仍由 Claude Code 撰写”）从本 ADR 起读起来是
> 过时的。** 以本 ADR 为准。

如果 User 愿意顺手把 `AGENTS.md` 改干净，需要改的就是这两处：第 99 行改为
`docs/82-tasks/**  → ChatGPT / Claude Code 均可写`，第 112 行的“新 TASK”
三个字删掉、只留“新 ADR 仍由 Claude Code 撰写”。**不改也不影响本 ADR 生效**
——只是仓库里会有一处已知过时的表述，此处已记录，不算隐性双事实来源。

**`CLAUDE.md` 同理，也没有改动**（User 本次的范围里明确排除了它）。它在
“Before implementation”第 3 条写着“consider whether one should be written
first (Claude's exclusive lane per `AGENTS.md`)”，**这句话从本 ADR 起同样是
过时的**。要改的话就是把括号里的 exclusive-lane 说明换成“ChatGPT 或
Claude 均可写（ADR-036）”。同样，不改也不影响本 ADR 生效。

## 4. 代价：规格与审查的独立性没有了

**必须明写，因为 `AGENTS.md` 103–106 与 137 行专门论证过这条独立性“不是
形式”**，本 ADR 正是把它拿掉：

- 原论证是：**ChatGPT 审查 Codex 时，对照的是一份它没有写的 TASK**。
  ADR-029 记录的第 1 条防线就建立在这上面。
- 本 ADR 之后，ChatGPT **写规格、派活、又审查**。它审的是自己的规格，
  **“规格写错了”这一类缺陷因此失去了独立的发现者**。
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
5. **User 不定时叫 Claude 做阶段性审核。** 频率由 User 定；本 ADR 不给它
   任何固定节奏，也不给任何“到了某个节点就必须审”的规则。

**这笔交换是 User 明确选择的：用规格-审查独立性换吞吐量。** 记在这里是为了
以后出问题时能对上原因，不是为了留一个翻案的伏笔。

## 5. Claude 不在任何后台自动运行（已核实的事实，不是承诺）

User 提到“看你在后台自主执行审查和监督，浪费 Token”。**2026-09-20 实测，
本仓库不存在任何会自动消耗 Claude token 的东西**：

| 载体 | 实测状态 |
|---|---|
| Claude 侧 Routines / 定时唤醒 | **一个都没有**（`list_triggers` 返回空） |
| `.github/workflows/claude.yml` | 触发器只有 `issue_comment`，且条件是 `comment.user.login == 'wayne19870129'` **且**评论含 `@claude`。**没有 schedule** |
| `.github/workflows/pipeline-health.yml` | 每 2 小时跑，但**不含任何 LLM、不含任何 Claude 凭据**（文件头第 8–14 行写明这是刻意的设计约束）。它只把流水线状态压成一个 JSON，**Claude token 消耗为零** |
| `.github/workflows/security.yml` | 每周一次，纯扫描工具，无 LLM |
| ADR-029 §6 的定时架构审计 | **已于 ADR-034 整节撤销**，载体也早已失效 |

**结论：没有需要关掉的后台 Claude。** 本 ADR 把这条状态固定下来：

> **Claude 的每一次运行都由 User 发起——对话里开口，或在 PR/Issue 里
> `@claude`。没有定时器，没有事件触发，没有“某个节点必须叫 Claude”的规则。**

`pipeline-health.yml` 每 2 小时消耗的是 GitHub Actions 分钟数，不是 token；
要不要关是 User 的成本取舍，**本 ADR 不动它**。

## 6. Claude 剩下的职责（完整清单，没有别的）

1. **整体架构设计**：写 ADR、维护 `ARCHITECTURE.md`。
2. **User 触发的阶段性审核**：User 开口时，做架构级 + 细节级审查，产出写进
   `docs/81-reviews/`。
3. **被 User 点名时的具体任务**（改文档、补规格、查一个具体问题等）。

**不再做**：维护派发队列、按任务写 TASK、在缺口出现时自动介入、任何形式的
定期巡检。

## 7. 重新评估条件

- 连续出现**由规格缺陷导致**的返工，且 CI 与执行者都没能拦住——那正是 §4
  拿掉的那道防线在起作用的证据，届时需要重新权衡；
- User 要求恢复 TASK 的独占撰写；
- `AGENTS.md` 铁律 5 被改变（ADR 不再是架构变更的前置）——那会让 §2 唯一的
  停止条件失效，必须先处理。

## 8. 本 ADR 不授权什么

不放宽 `AGENTS.md`「禁止自主执行」整张表；不放宽凭据与密钥边界；不开启
自动合并；不授权任何人改 `AGENTS.md`；不授权生产部署。**下放的是规划与
执行的决定权，不是安全边界。**
