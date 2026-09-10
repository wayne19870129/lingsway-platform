# [PROCESS TEST] Auto-fix / ChatGPT Work closed-loop verification

- 状态: 流程测试记录,不是真实架构决策或事故复盘
- 目的: 验证 "Claude 建 PR → 独立审查在具体 SHA 上产出结果 → 带有明确
  测试标记的修复请求 → Claude 处理并推送新 SHA → 新 SHA 触发复审 →
  普通事件不误触发返工 → PASS 后停止等待人工" 这条闭环,并把真实触发的
  返工与本文件模拟的流程测试分开记录。

本文件本身就是测试用的文档夹具:不涉及任何业务代码、不改变任何运行时
行为。测试完成后可以按需保留(作为闭环验证的证据留存)或删除。

## 时间线(测试执行时逐步补全)

- T0: 本文件随 PR #42 首个 commit(`af993da`)一起创建。
- T1: 截至处理本条流程测试请求时(评论
  `https://github.com/wayne19870129/lingsway-platform/pull/42#issuecomment-5620876284`),
  PR #42 尚未收到任何独立审查(无论来自 ChatGPT Work 还是其他审查者)。
- T2: 收到 PR #42 上明确标记 `[PROCESS-TEST]` 的修复请求(同上评论)后,
  Claude 识别出该评论不具备 Work 审查的 `Critical`/`Major`/`Minor`/
  `Checks performed`/`Verdict` 结构,判定为流程测试而非真实审查,按要求
  编辑本文件并提交新 commit,完整 SHA:
  `72020c86270a2fde6998bf5a85cf9cd96536ff9c`。
- T2.5(方法论修正): Work 对 T2 对应 SHA(`72020c86`)的独立审查指出
  ——本 PR 当时的 base 是旧 `main`(`301dc380d50770653d6d5799426c99ea4097d881`),
  该基线既没有根目录 `CLAUDE.md`,`AGENTS.md` 也还是旧的职责分工("Claude
  只产出 TASK/REVIEW/ADR、Codex 实现")。也就是说 T1/T2 只能证明"Claude
  收到一条流程测试评论后编辑了文档",**不能**证明本测试原本要验证的
  SHA 去重、PASS 后停止、三轮返工上限、按 Work 审查格式识别来源这几条
  规则真的按预期工作——因为这些规则当时根本不存在于本分支所在的代码里。
  该 finding 判定 `VALID`,处理方式:把本分支 merge 到已修复的治理分支
  `docs/unify-collab-roles-and-autofix-loop`(此时为 `1f174ef`),使本
  PR 的代码里实际包含待验证的规则,merge commit 完整 SHA 见 Git 历史
  (`273b1f4`)。T3/T4 需要在这次 restack 之后重新观察记录,之前任何
  关于 T3/T4 的推断均不成立。
- T3: (待补,需要在 restack 之后由外部事件触发复审时记录——记录时必须
  同时写明对应的 head SHA)
- T4: (待补,记录本 PR 生命周期内出现的任何重复/无关事件,以及它们是否
  被正确判定为不需要处理;在最终汇报里,T3/T4 空缺状态必须原样写成
  "尚未验证",不得写成已通过)
