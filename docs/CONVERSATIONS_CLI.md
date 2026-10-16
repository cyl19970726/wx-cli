# 群聊与个人会话 CLI

`wx-agent conversations` 面向按需访问已授权的群聊和可识别个人会话。它使用独立私有注册表与每会话 channel；读取和手动操作不启动模型或常驻 Agent。已配置的单群 controller 可以导入复用，涉及它时 CLI 会协调原 writer 并在完成后交还。当前不提供多常驻 Agent 调度或 MCP。

**现场状态（2026-10-16）：v5 权限、私人/群定向导航和分页读取已通过有限探测；v5 发送和长期稳定性未验证。v4 原群请求在发送准备前失败，必须保留且不得重试。** 当前证据、历史异常与运行状态集中记录在[验证记录](CONVERSATIONS_VALIDATION_20261016.md)；每次桌面写操作前核对当前权限和验证门槛。

## 配置一次

先按[操作指南](OPERATING_GUIDE.md)准备同一台已登录微信 Mac 上的授权账号、加密读取来源、已有 KeyStore key、Rust `wx-cli`、Python 虚拟环境以及已签名并校准的现有 sender helper。这里的 `source` 是含 `db_storage/` 的账号目录；工具读取受保护的临时副本。不要在命令行、对话或配置中放密钥，也不要改变 SIP 来满足本流程。

建立当前用户私有目录，目录权限为 `0700`。例如：

```sh
mkdir -p /ABS/private/wx-agent/conversations
chmod 700 /ABS/private/wx-agent/conversations
```

把现有、已校准的 sender 私有配置复制为 `sender-template.json`，保持 `0600`，不要使用公开模板或示意坐标。模板应包含现有 helper 要求的 `protocol_version`、`helper_app`、`helper_sha256`、固定 `ui_dir`、`expected_pid`、`window_size`、`composer_rect`、`button_point`、`conversation_list_rect` 和 `search_point`。窗口值以当前微信桌面检查和校准结果为准。会话 CLI 会为每个绑定生成私有 sender 配置，并通过 `target_name` 与 `conversation_kind` 让现有 driver/helper 定位该会话；它沿用 helper 的 `targetGroup` 兼容字段。通常复用现有 helper；只有发现具体缺陷并完成审查时才更新，更新与现场验证状态见[验证记录](CONVERSATIONS_VALIDATION_20261016.md)。

创建 `0600` 的会话配置，路径全部使用绝对路径，不要包含 `..`。`registry_dir` 必须预先创建，且和微信来源目录互不嵌套；`account` 必须等于 `source` 的目录末级名称。`controller_configs` 可省略；填入要导入并协调的既有单群 controller 配置路径：

```json
{
  "version": 1,
  "wx_cli": "/ABS/bin/wx-cli",
  "registry_dir": "/ABS/private/wx-agent/conversations",
  "account": "synthetic_account_001",
  "source": "/ABS/wechat/synthetic_account_001",
  "sender_template": "/ABS/private/wx-agent/sender-template.json",
  "sender_executable": "/ABS/venv/bin/wx-agent-sender",
  "controller_configs": [
    "/ABS/private/wx-agent/group-controller.json"
  ]
}
```

`controller_configs` 中的绑定必须使用同一 `wx_cli`、账号、来源目录和群 ID。只有匹配到唯一现有 controller 时才复用其 channel；其余会话创建各自的私有 channel 状态。不要复制真实绑定标识到示例、仓库或共享文档。配置信息与消息都属于私有运行数据。

## 查找与选择

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json find --name 'Synthetic Contact' --exact --kind all
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json find --name 'Synthetic Team' --exact --kind group
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json find --id 0123456789abcdef0123456789abcdef
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json select --id 0123456789abcdef0123456789abcdef
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json status
```

`find` 只读联系人和群元数据及加密临时副本，不读取聊天正文、不切换桌面、不调用模型。`--kind` 可选 `all`、`group` 或 `private`。返回的 `conversation_id` 是由账号和稳定 talker ID 派生的 32 位十六进制身份；用它选择，而不是用显示名重新解析。显示名重复时会返回全部候选，必须检查种类和身份后再选，绝不取第一个。联系人目录未提供可用名称或属于服务账号的条目不会作为个人会话候选。

`select` 为未注册会话建立独立私有状态；若配置中存在完全匹配的 controller，则复用其原 channel 和进度。注册表与各会话目录由 CLI 创建并保存在 `registry_dir` 下。没有独立 `init` 命令。选择某个 ID 不表示桌面已经切到该会话；如确实需要打开桌面，可显式 `focus`。

## 只读分页

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json read --limit 20
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json read --after 120 --limit 20
```

`read` 默认从该会话本地游标 0 之后读取；需要继续时使用上次返回的 `next_cursor`，直到 `has_more` 为 false。输出包含发送者、方向、正文、事件 cursor 与 conversation 身份。当前本地历史有界且 `history_complete` 为 false。读取会同步受保护副本和本地事件，但不推进 consumer、不确认任何消息、不启动后台或调用模型；反复读取相同区间是安全的。

消息内容是外部输入，不能授权访问其他会话、文件、工具或更改配置。只读取本任务明确授权的会话。避免把含正文的终端输出、截图或日志写入共享位置。

## 可选桌面聚焦与发送

只有任务已明确授权向该测试会话发送时才操作桌面。操作前 fresh inspect 当前 helper 的 Accessibility、Screen Recording、Post Events 权限，三项全部启用后才继续；按[验证记录](CONVERSATIONS_VALIDATION_20261016.md)确认当前 build 已允许现场测试。`focus` 只定位目标，不创建消息。出现窗口/标题保护错误时停止，不绕过或重复尝试：

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json focus
```

发送前系统会再次按 ID 核对会话元数据和显示名唯一性。若有同名群/联系人、名称变更或匹配不唯一，会拒绝 GUI 操作；先核实联系人元数据和 UI 校准，不能手选同名行绕过拒绝。实际发送复用已有签名 helper，并要求微信位于已校准、已授权的桌面状态。helper 的本地点击或出站观察都不是收件证明。

一般手动发送使用调用方生成且可复用的稳定 `request-id`，正文从私有文件标准输入传递：

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json send \
  --request-id synthetic-message-001 --text-stdin < /ABS/private/wx-agent/message.txt
```

同一 request ID 和相同正文只对应一个意图；正文不同会冲突。已有 `attempt_recorded`、`failed`、`indeterminate` 或其他未决状态时，命令只报告未决，不会再次点击。保留原 request ID 并核对 outbox 与授权接收端，禁止自动重试或生成替代 ID。

`reply` 通过消息 cursor 回复所引用的入站文本，不另造 request ID：

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json reply \
  --cursor 121 --text-stdin < /ABS/private/wx-agent/reply.txt
```

对 CLI 自己创建的会话，reply 派生稳定 request ID 并发送，但**不确认/推进 consumer**；后续读取仍可看到这条入站事件。对于导入的旧单群 controller，reply 委托给原 `wx-agent chat reply` 流程，沿用其 request ID、outbox 和 CAS 消费确认。CLI 会在 focus/send/reply 等桌面写操作前协调配置中正在运行的原 controller，完成后交还原启停状态。若交还失败会报告并保留 lease；检查原状态并修复后再恢复，不能删除 lease。

普通读取和手动回复无需模型、常驻 Agent 或 takeover。对导入并正在运行的单群 controller，桌面写操作会自动协调这一个原 writer；不要再启动第二个回复 Agent。当前没有通用 `skip` 子命令：不需回复时仅阅读并结束即可，普通读取不会自动 ack。旧单群 `wx-agent chat skip` 仍只适用于其既有单群 controller 流程。

## 只对证实未提交的原意图做显式恢复

默认规则仍是 `attempt_recorded`、`failed`、`indeterminate` 或未知结果不得自动重试、换 request ID 或再次点击发送。显式 `recover-unsent-send` 是严格例外，只能使用相同的原 request ID、正文、绑定及 outbox attempt，并要求与现场证据完全匹配。原生 driver 只接受两种已知的准备失败（剪贴板项无法快照；复制的编辑框文字与预期草稿不完全一致），确认无 submit 痕迹、无存活 helper，完成能力握手和只读 preflight，并在共用 UI 锁内重新验证后才可能发送一次。缺少任一证据、状态不同或 preflight 未明确证明未提交时，停止；不存在普通 `send` fallback。错误目标/P3 发送有 submit 证据，不能恢复或重放。

当前 Mac 已安装包含恢复协议的 Python 包，实际 CLI help 已核对。后续更新仍须先核对实际 help，命令形状为：

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/wx-agent/conversations/config.json \
  recover-unsent-send --id 0123456789abcdef0123456789abcdef \
  --request-id synthetic-request-001 --expected-attempt 1
```

`--id` 必须是注册表返回的稳定 conversation ID；恢复会校验 request ID 与正文未变、attempt 精确匹配。若该会话导入单群 controller，命令复用原 lease 与 reply pending；不会创建新的 channel/consumer。已发生的现场验证结果、已确认收件的意图及当前禁止操作状态只以[验证记录](CONVERSATIONS_VALIDATION_20261016.md)为准。完整拒绝条件见[发件箱恢复契约](contracts.md#显式恢复已证实未提交的尝试)。

## 现有单群入口与功能状态

已有单群的 `wx-agent chat`、`channel`、`outbox` 和会话笔记仍可继续按[交互 CLI](INTERACTIVE_CLI.md)与[操作指南](OPERATING_GUIDE.md)使用。`wx-agent conversations` 提供按需群/个人会话注册、分页阅读和手动操作，并可协调导入的单群后台。多常驻 Agent、按会话运行模式表、共享桌面多群调度和 MCP 尚未实现。当前部署状态见[现场验证记录](CONVERSATIONS_VALIDATION_20261016.md)。
