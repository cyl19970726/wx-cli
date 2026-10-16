# ADR 0001：在 wx-cli fork 内维护单群 Agent

状态：采用。日期：2026-10-16。

## 决定

继续使用 `cyl19970726/wx-cli`，保留其 Git 历史、MIT 许可证和上游来源。Rust workspace、Python 运行包、Swift 助手、项目 skill、契约与测试共同版本化。不采用 Git submodule，也不把已实现的消息读取、消费确认和发件箱再复制到旧的视觉原型仓库。

## 为什么

当前真正通过单群测试的是这个 fork 的 channel/outbox 与桌面驱动组合。它们共享消息身份、协议、错误分类和发送尝试语义，修改时需要一起验证。submodule 会额外引入两个提交版本、两次发布及部署配套检查，却没有带来独立消费者或独立团队的收益。

旧 `wechat-desktop-agent` 仓库保留为视觉路线的历史原型；本次不覆盖它尚未提交的探索。产品入口为本 fork，而不是要求用户拼装两个项目。

## 包边界

- `crates/`：上游 Rust 库与 `wx-cli`。channel/outbox 当前仍为 CLI 内部模块，JSON 协议是对 Python 的边界。
- `packages/wx-agent/`：独立可安装的 Python 包，暴露 `wx-agent`。不会复制 Rust 的消息表、确认游标或发送状态机。
- `native/macos/wechat-bridge/`：独立构建和授权的 Swift 助手。Python 发布不自动替换它。
- `.agents/skills/wechat-group-agent/`：项目内 Agent 操作约定，复用 CLI。

暂不抽出 `wx-channel` 公共 Rust crate：当前没有第二个 Rust 消费者，稳定公开 API 会提前固定仍在演进的内部结构。未来若 MCP 需要嵌入式调用，可以在同一 workspace 抽取库；如果只是工具适配，直接复用现有进程协议即可。

## 代价和后续条件

仓库需要同时运行 Rust、Python 和 Swift 检查；版本不意味着所有组件都部署过。每次说明独立包版本、协议版本与现场验证范围。上游更新按正常 Git 合并或精选提交进入，经同一检查后发布；不定时自动拉取和运行上游新代码。

MCP、多群调度与完整业务记忆仍是独立后续功能。先解决单群的需求摘要与已有能力的安装、操作、恢复，不为未来能力创建空服务。
