# ADR-009: Provider 抽象层

- 状态: 已接受
- 日期: 2026-08-30
- 决策者: Lingsway Platform

## 问题

系统依赖多个外部服务，包括 Webshare 出口、Marzban 账务、Xray 网关、
Mihomo 转发、支付、通知、邮件、验证码和对象存储。这些服务可能涨价、封号、
停止维护，或需要同时接入多家。若业务逻辑直接调用具体 SDK，更换供应商需要
改动大量业务代码。

## 决定

引入 `providers/` 抽象层。`domain/` 只依赖 `base.py` 中的 `Protocol`，
具体实现由 `registry.py` 按环境变量装配。每个 provider 必须提供 mock 实现。

## 约束

1. `domain/` 中禁止 `import httpx`、禁止读写文件、禁止调用 shell。
2. 业务代码禁止直接 import 具体 provider 实现，只能经 registry 获取。
3. 每个 provider 的 mock 实现必须支持通过构造参数注入失败，供补偿逻辑测试使用。

## 验收标准

所有 provider 环境变量设为 mock / noop 时，单元测试可在无网络、无 Docker 的
环境下通过。这是本决定是否成立的唯一检验。

## 考虑过的替代方案

1. 直接调用 SDK：更换供应商需大改业务代码，否决。
2. 拆成独立微服务：当前全部跑在同一进程，拆分只增加部署复杂度，否决。
3. 使用依赖注入框架：项目规模小，`Protocol` + registry 已足够，否决。

## 重新评估条件

当需要同时运行两家出口供应商并做动态调度时，重新评估是否需要更完整的
依赖注入容器。
