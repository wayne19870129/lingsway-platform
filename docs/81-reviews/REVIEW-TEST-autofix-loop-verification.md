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
  编辑本文件并提交新 commit(见本次提交 SHA)。
- T3: (待补,需要在推送新 SHA 后由外部事件触发复审时记录)
- T4: (待补,记录本 PR 生命周期内出现的任何重复/无关事件,以及它们是否
  被正确判定为不需要处理)
