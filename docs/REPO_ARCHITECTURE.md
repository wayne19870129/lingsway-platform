# 仓库架构规范 — 已合并到根目录 `ARCHITECTURE.md`

> **本文件不再承载内容。** 架构规范的唯一正本是仓库根目录的
> [`ARCHITECTURE.md`](../ARCHITECTURE.md)。

## 为什么是一个指针

在 2026-09-18 的仓库审计之前，本文件与根目录 `ARCHITECTURE.md` 是
**逐字节完全相同**的两份拷贝（各 533 行），且两份都自称"唯一架构依据"。
这构成了一个真实的双事实源风险：

- `AGENTS.md` 的「模块写权限」表只写了 `ARCHITECTURE.md → Claude Code 独占`，
  完全没有提到 `docs/REPO_ARCHITECTURE.md`。按字面执行规则的 Agent 会更新
  根目录那一份，而把这一份留在原地慢慢过期。
- `README.md` 与 `docs/CODEX_PROMPT.md` 当时都把读者指向**这一份**，
  也就是指向那份注定会过期的拷贝。

因此本文件被收敛成指针，而不是删除：删除会让 `README.md`、
`docs/CODEX_PROMPT.md` 以及任何历史 PR / 评论里的既有链接直接失效。

## 权威顺序提醒

架构规范本身不改变 `AGENTS.md` 声明的冲突优先级：

> **ADR > `AGENTS.md` > REVIEW**

根目录 `ARCHITECTURE.md` 记录的是**原始搭建规范（T0~T8 时期）**。凡是它与
已接受的 ADR（`docs/80-decisions/`）冲突的地方，以 ADR 为准；它的目录树、
provider 清单和任务序列有相当一部分已经被后续演进取代，具体偏差见
[`docs/83-project-continuity.md`](83-project-continuity.md) 的
「Spec-vs-reality drift」一节。
