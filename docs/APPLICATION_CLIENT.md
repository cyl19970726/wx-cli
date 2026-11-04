# 普通应用发送通知

`ApplicationClient` 让已获授权的本地业务应用向受管会话提交文字意图，并按原业务 key 查询结果。应用无需运行模型或安装 Session 插件。Rust coordinator 和 outbox 保存会话绑定、意图及发送尝试；多个应用共用桌面队列。架构边界见[通道订阅合同](CHANNEL_SUBSCRIPTIONS.md)。

## 其他项目从哪里接入

公告采集、日报或业务告警项目走“业务程序 → 应用客户端 / 鉴权网关 → Rust outbox → 微信宿主共享桌面执行器”。它们不依赖 Codex 聊天冷唤醒、App Hook 或 Session 插件。跨机器调用优先使用下文的鉴权网关；只有与微信运行环境同机的可信程序才直接使用 `ApplicationClient`。

业务项目只需安装 Python 3.9+ 的 `wx-agent` 包，安装方法见[操作指南](OPERATING_GUIDE.md#1-安装)，并取得本项目独立的私有客户端配置。远端调用方不需要微信数据库、Rust CLI、原生助手、模型登录或管理员 SSH 凭据。微信宿主仍须准备自己的 Rust CLI、原生助手和授权通道状态，客户端安装不会部署它们；模型登录不是应用发送的依赖。

首次实际使用前，微信宿主维护方提供稳定的 `application_id`、明确的目标 conversation ID 范围、仅含 `send` / `result` 的网关授权，以及专用受限 SSH 连接。维护方还须核对绑定当前为 `active` / `authorized`、配置 owner/epoch 有效、发送执行器正在运行，且桌面发送已经正常启用。`send_text` 只负责入队，不会启动执行器、解除暂停、切换 owner 或启用自动回复；验收过发送能力不等于一个新业务项目已经开通。

业务项目保存公告 / 日报与原 request key 的对应关系及查询结果。单条公告可用稳定来源 ID 的摘要组成 key，日报可用日期、目标及明确的版本组成 key；不要用发送时间或每次重试新生成的随机数。公告采集、筛选、业务去重和内容整理由业务项目负责，微信发送尝试与结果以 wx-cli 的原 outbox 为准。

## 日报长度与分段

每条文字最多 2000 个 Unicode 字符（按 Python 字符计数），且 UTF-8 编码不超过 16000 字节。正文不能为空，不支持末尾换行或禁止的控制字符。应用客户端在任何子进程和意图写入前校验这些条件，非法文字返回明确的 `invalid_text`；提交后超时或结果不明仍保留为未知，不能据此重发。

较长日报可在业务项目中按公告条目分段，保留完整信息，每段带 `1/N` 等顺序标记；标记也计入上限。先冻结整份日报版本、总段数、每段正文及摘要和稳定 key，例如 `digest-20261104-target-v1-p01`。同一批次重启后沿用原清单，不重新随机编号或改变已提交段的内容。

当前应用网关没有额外的 batch/part_index 参数；每段分别用 `send_text` 提交。业务方在前一段查询达到 `ui_observed` 后再提交下一段；`pending` 时继续查询，未知或拒绝时停止后续段并核对原请求。整份日报分别记录部分已入队、部分本地已观察、全部本地已观察和独立收件确认，第一段完成不代表全部完成。共享桌面执行器始终由微信宿主运行，业务项目不另建发送器。

## 本地入门

1. 安装 `wx-agent`，准备现有的 `wx-cli`、受管会话配置、coordinator root 和目标 channel state dir。确认目标绑定处于 `active`、`reply_policy=authorized`，取得该绑定的 `owner` 与 `epoch`。本客户端不会替应用更换 owner。
2. 将[合成配置样例](../examples/application-config.synthetic.json)复制到仓库外的私有文件，替换为本机**规范绝对路径**、获准的 32 位十六进制 conversation ID、当前 owner 和 epoch。配置文件必须由当前用户持有，权限为 `0600`，且不是符号链接或硬链接。`application_id` 固定为本应用的稳定命名空间。
3. 用[本地通知样例](../examples/application-notifier.py)提交一次意图，之后用同一个本地 key 查询。以下命令的路径与 ID 都是合成占位值；执行前须替换为获授权的私有配置。

```sh
printf '%s' 'Synthetic notice' | python3 examples/application-notifier.py \
  --config /ABS/private/app-a.json send \
  --conversation-id 0123456789abcdef0123456789abcdef \
  --request-id notice-001 --text-stdin

python3 examples/application-notifier.py \
  --config /ABS/private/app-a.json status \
  --conversation-id 0123456789abcdef0123456789abcdef \
  --request-id notice-001
```

另一应用使用自己的 `application_id`（例如 `app_b`）、独立配置与稳定本地 key。`app_a` 的 `notice-001` 冻结为 `app_a.notice-001`，`app_b` 的同名 key 冻结为 `app_b.notice-001`；这两个完整 ID 不冲突。`send_text()` 和 `status()` **都接收原始本地 key** `notice-001`，而不是返回的完整 ID。业务重试须沿用原 key 与完全相同的内容；改变已用 key 的正文会被拒绝。`send_text()` 使用 `--no-wait` 入队，不等待桌面执行。

`send_text()` 在新发送前检查固定的 owner、epoch、控制状态及回复授权，并将应用 ID 冻结为 Rust outbox 的 `publisher_id`。该字段与业务 owner 分开，重启或交还不会改变；已有 ID 也不能更换 publisher。`status()` 只读原会话中属于本应用 publisher 的请求，即使本应用后来续授权到新 owner/epoch，仍可查询自己的旧结果；旧记录的 `NULL` publisher 不会被冒认为本应用。查询不会重新发送。超时或返回不明时，保留原 key，检查原请求状态，不能改 key 重投。`pending` 是排队状态，`ui_observed` 是本地界面证据，均不等于收件人确认。

`ApplicationClient` 是**可信本机配置适配器**，配置限定会话范围并固定 owner/epoch。独立应用凭据和远端能力由下述 gateway 负责；本地适配器本身不隔离同一系统用户拥有任意 shell 权限的程序。图片发送后置。

## 鉴权远端接入

网关是一次性 stdin/stdout JSON RPC，不启动模型或额外消息账本。`wx-agent-gateway --config POLICY request` 校验应用 token 的 SHA-256 摘要、允许会话和权限；`wx-agent-gateway-client --config CLIENT METHOD --params-stdin` 可直接调用或经已有 SSH alias 调用它。正文和 token 都从 stdin 传送，SSH 命令不携带它们；transport 超时不自动重试。

使用[网关策略样例](../examples/gateway-policy.synthetic.json)和[客户端样例](../examples/gateway-client.synthetic.json)准备私有 `0600` 文件。为实际应用生成独立随机 token，客户端保存原值，策略只保存摘要；样例 token 仅供合成测试。策略中的 `client_config` 指向上文应用配置。`send` 允许提交，`result` 允许查询本应用原请求；`inbox` 用于明确绑定 Session 的业务订阅，须额外指定固定 `session_ref` 和 `thread_id`。只发通知的应用不授予 `inbox`。

远端客户端在样例中增加 `ssh_target`，值为已配置的 SSH alias；`command` 保持指向微信宿主上的绝对网关程序、`--config`、私有策略路径、`request` 这四项。客户端使用非交互 SSH 和连接超时，不修改 SSH 配置。

可选 `ssh_config` 指向本机规范绝对路径、权限为 `0600` 的项目私有 SSH 配置，客户端以 `-F` 读取它。给业务应用分发连接时，应提供只能执行网关请求的专用受限凭据；管理员的通用 SSH 连接不等同于应用权限边界。网关令牌仍按应用及会话限定能力，不应把管理员私钥交给业务应用。

```sh
wx-agent-gateway-client --config /ABS/private/app-client.json send_text --params-stdin \
  < /ABS/private/send-parameters.json
wx-agent-gateway-client --config /ABS/private/app-client.json result --params-stdin \
  < /ABS/private/result-parameters.json
```

`send-parameters.json` 的合成示例：

```json
{
  "conversation_id": "0123456789abcdef0123456789abcdef",
  "request_id": "notice-0123456789abcdef-v1",
  "text": "公告通知\n标题：合成项目\n来源：合成来源\n链接：https://example.com/notices/001"
}
```

对应的 `result-parameters.json` 使用同一个原始业务 key：

```json
{
  "conversation_id": "0123456789abcdef0123456789abcdef",
  "request_id": "notice-0123456789abcdef-v1"
}
```

发送参数只含 `conversation_id`、原本地 `request_id` 和 `text`；查询去掉 `text`。接口不接受临时 owner、执行路径或任意命令。跨群、错误凭据及无权限方法在处理前拒绝。冻结事件按 delivery 范围分页，终结旧 Session 的 transport 与重新授权新的业务处理分别核验。源码/合成检查和真实 SSH/微信收件分别记入[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)，不能互相代替。

当前应用客户端只提供文字通知和原结果查询，不提供图片发布方法或 HTTP 发送服务。图片及配文已在通道共享队列另有实机证据，不能据此推断应用网关有 `send_image` 接口。每个新业务项目仍须核对自身目标和凭据，做一次有界端到端验收；API 成功入队、`ui_observed` 与收件人确认分别记录。网络超时或结果未知时查询原 key，保留原记录，不换 key 重投。

## 微信宿主持续运行发送执行器

业务应用在自己的 Worker 中生成和提交日报；微信宿主另行运行唯一共享执行器，无需启动模型、收件处理或 Session 插件：

```sh
wx-agent dispatch --config /ABS/private/conversations.json run --seconds 3600
```

已有执行器时复用它。需要控制电脑离线后继续工作时，在微信宿主用系统进程管理器托管这一入口；macOS 可参考[合成 LaunchAgent 样例](../examples/desktop-executor.synthetic.plist)，将所有路径和标签替换为实际部署值，并预建私有日志目录及文件。样例每小时正常结束一个有界执行周期，由 `KeepAlive` 拉起下一周期；`SIGTERM` 请求当前周期在正在执行的有界操作后退出。相同 coordinator root 的 `executor.lock` / `sender.lock` 仍负责排他，不为每个应用另起桌面执行器。

持续运行与发送授权分别配置：维护方只为明确获准的目标设置 `authorized`，保留原 owner，按实际新 epoch 续本应用授权。执行器本身不启动模型或自动回复；失效授权、人工接管、未知尝试和桌面暂停仍受原合同约束。系统重启进程不能自动执行 `review`、解除暂停、恢复旧意图或删除状态。原生助手签名和权限保持固定，Python 或进程管理配置更新不重建它。

进程存活、桌面可用和实际投递分别核对。macOS 图形登录、微信登录、未锁定的控制台和现有原生权限是桌面发送前提；控制电脑离线与微信宿主锁屏、登出或重启是不同场景。部署进程后仍需用明确授权的业务请求核对一次 attempt、精确源事件和结果查询，独立收件另记。

实机证据包括两个应用各经受限鉴权 SSH 发出一次文字，均为一次 attempt、`ui_observed`，用户分别确认目标、内容、换行及各收到一次；鉴权、跨范围、无 shell/PTY 探针通过。另一物理宿主的只读 inbox API，以及已加载原 App 宿主的 queue snapshot 处理各有独立证据。图片队列、30 条交替文字和人工接管的后续结果见[实施记录](MULTI_CONVERSATION_IMPLEMENTATION.md)；自动冷唤醒与旧 App Hook 回调不计入已通过范围，也不构成本页应用发送的前置依赖。
