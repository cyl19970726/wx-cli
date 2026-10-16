# Agent 与指定微信群

采用 **项目内 skill + CLI**。技能说明群绑定、需求沟通、来源核对和人工接管；CLI 执行读写与状态变更；常驻运行器负责监听。不要同时启动常驻自动回复和另一个主动发言 Agent。

- [wechat-group-agent skill](../.agents/skills/wechat-group-agent/SKILL.md)：已作为仓库文件提供，不安装全局。
- [wx-agent](../packages/wx-agent/pyproject.toml)：Python 安装包，生命周期、Codex 适配、会话笔记。
- [单群 CLI](SINGLE_GROUP_CLI.md)：绑定、订阅、发件箱和状态控制。
- [契约](contracts.md)：版本、身份、来源和不明发送结果。

会话原文由 Rust channel 保留；模型最近上下文之外，笔记保存有来源的摘要、已确认事项和未决问题。笔记由操作者或接入 Agent 显式更新，回复模型不自行写回。新功能的离线通过与旧版真实实测分开记录。

MCP 尚未实现。未来只增加相同命令/协议的工具适配层，不增加第二套消息进度、记忆或发送重试。多群也未开放；需要共享 GUI 资源的统一调度与身份切换验证。
