# 从源码到单群 Agent

这是一套本机桌面渠道，不是扫码即用的托管服务。先有 macOS 上已登录的授权测试账号、本地密钥和已校准的图形助手，再开启自动回复。微信解析沿用上游；包结构调整不会自动修改你的系统权限。

## 安装

```sh
git clone https://github.com/cyl19970726/wx-cli.git
cd wx-cli
cargo build --release --locked
python3 -m venv .local/venv
.local/venv/bin/python -m pip install ./packages/wx-agent
.local/venv/bin/wx-agent --help
```

Python 3.9+，Rust stable；图形助手当前需要 Apple Silicon / macOS 14+。本文中的 `/ABS/` 与环境变量都是占位路径，必须替换为本机绝对路径。不要在配置中加入密码或密钥。

## 准备唯一群绑定

按 [单群 CLI](SINGLE_GROUP_CLI.md) 用准确群 ID、账号目录、session 与 `--encrypted-snapshot` 绑定。先 `sync`、`status`，核对消息发送者、方向及最新游标，明确从哪里开始自动回复。不能用群昵称代替身份绑定。

密钥管理沿用 `wx-cli key --help` 和固定版本的上游指引；已有密钥不需要重新提取。本仓库没有自动修改 SIP、安装调试权限或登录微信的脚本。图形发送需要 [原生助手](../native/macos/wechat-bridge/README.md) 的独立构建、授权和当前窗口校准；它的输出目录当前固定，须按该指南部署。

## 设置运行器

将 [合成配置](../examples/agent-config.example.json) 复制到私有目录，改成真实本机可执行路径。目录 `0700`，文件 `0600`。`agent_command` 可调用已登录的 Codex；默认示例模型是此前验证的选择，是否可用取决于你的账户，不会静默换模型。

发送驱动是安装后的 `wx-agent-sender`。它从 `WX_SENDER_CONFIG` 读取独立私有配置；先按助手指南校准绑定、签名哈希、窗口和输入框。所有运行器子进程继承启动时环境，不要把敏感配置写进公开脚本。

```sh
export WX_SENDER_CONFIG=/ABS/private/sender-config.json
/ABS/venv/bin/wx-agent status --config /ABS/private/agent-config.json
/ABS/venv/bin/wx-agent start --config /ABS/private/agent-config.json
/ABS/venv/bin/wx-agent pause --config /ABS/private/agent-config.json
/ABS/venv/bin/wx-agent resume --config /ABS/private/agent-config.json
/ABS/venv/bin/wx-agent stop --config /ABS/private/agent-config.json
```

`run` 是同配置的前台运行；`start` 是本机后台子进程，不是开机服务。第一次 `start_after` 必须是核对过的当前事件位置；后续沿用原 consumer/config，不每次重置。配置变化会被拒绝，见下文迁移。

## 建立会话需求笔记

```sh
/ABS/venv/bin/wx-agent memory show --state-dir /ABS/private/channel --wx-cli /ABS/bin/wx-cli
/ABS/venv/bin/wx-agent memory update --state-dir /ABS/private/channel --wx-cli /ABS/bin/wx-cli \
  --expected-revision 0 --file /ABS/private/memory-update.json
```

输入参考 [合成笔记](../examples/session-memory.example.json)，换成 `channel status` 的绑定和实际入站游标。更新输入不带 revision；expected-revision 使用最新 show 值。摘要与每条事项必须有来源。先读原文再写确认事项，推测放未决问题；程序核验来源存在，不替你判断话语含义。

笔记存在时运行器会在新一轮生成前加载。损坏或跨群笔记会停下；没有笔记仍按最近上下文工作。笔记不会自动生成，也不会从模型输出直接写回。

## 从旧平铺脚本迁移

旧 mini 运行版可以继续服务，本次源码整理不自动升级它。源码 `scripts/*.py` 现在是兼容启动器，不能只复制这几个文件到远端。部署应安装完整 wheel；原生助手保持独立。

升级时先正常停止、确认 pending/outbox，再备份私有状态并安装到新环境。保持同一绑定、consumer、游标与历史请求。新的命令路径会触发旧运行器的配置漂移拒绝；当前没有自动迁移命令。应审阅并迁移记录中的确切命令路径，保留可回退副本，再做一次有界重启与防重验证。**不要删除状态、换 consumer 或换 request ID 绕过拒绝。**

即使 CLI 活着，锁屏、微信注销或权限失效也会阻止发送。本地 `ui_observed` 不等于手机收到；不明发送结果应核对原请求，不做自动重试。
