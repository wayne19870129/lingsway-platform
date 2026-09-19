# TASK-S08-自动合并流水线与断路器

> 触发 `AGENTS.md` 的 TASK 文件门槛：触及 `.github/workflows/`，改变合并
> 与部署的治理边界。
>
> 架构依据：**ADR-029**（状态已是「已接受」），因此本任务**可以开工**。
>
> ⚠️ **本任务改变的是这个仓库的安全姿态本身。** 它开启自动合并，同时建立
> 断路器与定时审计。审查这个 PR 时的标准应当高于普通业务 PR。

## 目标

实现 ADR-029 的流水线机械部分。交付后必须成立：

1. **`.github/workflows/auto-merge.yml`** 在 ADR-029 §4 的**六个条件全部**
   落在同一个 head SHA 上时执行合并，缺一不可。
2. **`.github/automerge-enabled`** 存在，内容为 `true`。这是断路器：删除它
   或改成 `false`，自动合并立即停止。
3. **`.github/ISSUE_TEMPLATE/codex-dispatch.md`** —— 派活用的 Issue 模板，
   模板里只有一句指向 TASK 文件的触发指令，**不含任何需求正文**。
4. 自动合并**不触碰生产部署**：`deploy-auto.yml` / `deploy-gateway.yml` /
   `deploy-migration.yml` 的 `DRY_RUN` 闸门与人工批准一行都不动。

## 约束

- **六个条件缺一不可**，不得为了「先跑起来」省略任何一条：
  1. `Verdict: PASS` 的审查，且其标注的 head SHA **等于**当前 head SHA；
  2. 该 SHA 上全部必需 check 成功；
  3. 无合并冲突；
  4. `.github/automerge-enabled` 存在且为 `true`；
  5. 无 `no-automerge` 标签；
  6. **不是** `claude/audit-` 前缀分支的 PR。
- **不得**提供任何绕过开关（`force` / `override` / `skip-checks`）——
  铁律 3 的同构要求。
- **不得**让 workflow 在条件不满足时"重试到满足"，也不得用空提交、
  关闭重开等方式触发重新判定。
- **不得**自动合并任何触及 `deploy/` 的 PR——即使六个条件都满足。
  生产部署的人工闸门不受 ADR-029 影响。
- **不得**改动 `ci.yml` / `security.yml` / `risk-classify.yml` 的判定逻辑。
  自动合并是**消费**它们的结论，不是改写它们。
- workflow 的 token 权限按最小必要授予；**不得**使用能绕过分支保护的凭据。
- **不得**在 workflow 里回显任何 secret；日志不得出现 token。

## 允许修改的文件

未列出的路径一律不得修改。

```
.github/workflows/auto-merge.yml                      # 新增
.github/automerge-enabled                             # 新增
.github/ISSUE_TEMPLATE/codex-dispatch.md              # 新增
scripts/automerge_gate.py                             # 新增（见下方修订说明）
backend/tests/guards/test_automerge_gate.py           # 新增（见下方修订说明）
docs/82-tasks/TASK-S08-automerge-pipeline.md          # 本文件（记录这次修订）
docs/83-project-continuity.md
docs/85-agent-operating-model.md
```

> **2026-09-18 修订：加入后两个路径。** 原清单只有 `.github/**`，那意味着
> 判定逻辑只能内联在 workflow 的 YAML 里——而内联逻辑**没有任何办法在本地
> 跑一遍**，下面「验收标准」要求的「逐条可复现」就只能退化成贴源码说
> "这里判断了"，正是本 TASK 明确不接受的那种交付。
>
> 因此把判定抽成一个纯函数 `scripts/automerge_gate.py`（`scripts/` 已在
> `make lint` 的 ruff 覆盖范围内），workflow 只负责采集事实并调用它；
> 八条验收标准由 `backend/tests/guards/test_automerge_gate.py` 在 CI 里
> 逐条跑。**这是扩大了允许清单，不是绕过它**——按 §2.1「需要动未列出的
> 文件就停下来说明」的要求，理由记在这里和 PR 描述里。

**明确不在范围内**：任何 `backend/app/**`、`frontend/**`、`ops/**`、
`infrastructure/**`、`deploy/**`。本任务不碰一行业务代码；
`backend/tests/guards/` 下的新增文件是上面说明的判定逻辑测试，不是业务改动。

## 验收标准

```sh
# workflow 语法
python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/auto-merge.yml'))"
make lint
python -m pytest backend/tests/unit backend/tests/guards
```

ADR-029「验证要求」六条必须逐条给出**可复现的验证方式**，不接受
「逻辑上应该成立」：

1. 缺少 `.github/automerge-enabled` → **不合并**。
2. 内容为 `false` → **不合并**。
3. 审查 SHA ≠ 当前 head SHA（stale review）→ **不合并**。
4. 任一必需 check 非 success → **不合并**。
5. 存在 `no-automerge` 标签 → **不合并**。
6. `claude/audit-` 前缀分支 → **不合并**。

外加两条：

7. 触及 `deploy/` 的 PR → **不合并**（即使六条都满足）。
8. 六条全满足的普通 PR → **合并**，且合并方式与现有历史一致（merge commit）。

验证方式建议：用 `workflow_dispatch` 或一个只在分支上存在的测试 PR 逐条
构造，把每次的实际判定结果贴进 PR 描述。**不接受只贴 workflow 源码说
"这里判断了"。**

## 人工检查

- 这个 PR **必须由 User 人工合并**。自动合并不能自己把自己合进来——
  开关生效之前它还不存在，生效之后由它批准自己也是循环论证。
- PR 描述必须明确写出：开启自动合并之后，**问题会在进入 `main` 之后才被
  发现**，这是安全姿态的实质变化（ADR-029 已记录，PR 里要再说一次，
  确保审查者不是无意中放行的）。
- PR 描述必须写明断路器怎么用：改 `.github/automerge-enabled` 为 `false`。
