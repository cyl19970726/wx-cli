# 从源码到按需会话

这是一套本机桌面渠道，不是扫码即用的托管服务。先有 macOS 上已登录的授权测试账号、本地密钥和已校准的图形助手。微信解析沿用上游；包结构调整不会自动修改你的系统权限。

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

密钥管理沿用 `wx-cli key --help` 和固定版本的上游指引；已有密钥不需要重新提取。本仓库没有自动修改 SIP、安装调试权限或登录微信的脚本。图形发送需要[原生助手](../native/macos/wechat-bridge/README.md)的独立构建、授权和当前窗口校准。

## 选择并读取会话

按[操作指南](OPERATING_GUIDE.md#4-会话配置与发送助手)写好私有会话配置，然后用稳定 conversation ID 选择会话：

```sh
/ABS/venv/bin/wx-agent conversations --config /ABS/private/conversations/config.json find --name 'Synthetic Test Group' --exact --kind all
/ABS/venv/bin/wx-agent conversations --config /ABS/private/conversations/config.json select --id 0123456789abcdef0123456789abcdef
/ABS/venv/bin/wx-agent conversations --config /ABS/private/conversations/config.json read --limit 20
```

`select` 为新会话建立独立的私有 channel 状态。读取不推进消费、不启动模型。发送与回复见[会话 CLI](CONVERSATIONS_CLI.md)；需要持久 inbox、Session 推送或共享发送队列时，见[托管指南](MANAGED_CONVERSATIONS.md)。

## 建立会话需求笔记

```sh
/ABS/venv/bin/wx-agent memory show --state-dir /ABS/private/conversations/channels/0123456789abcdef0123456789abcdef --wx-cli /ABS/bin/wx-cli
/ABS/venv/bin/wx-agent memory update --state-dir /ABS/private/conversations/channels/0123456789abcdef0123456789abcdef --wx-cli /ABS/bin/wx-cli \
  --expected-revision 0 --file /ABS/private/memory-update.json
```

输入参考[合成笔记](../examples/session-memory.example.json)，换成该会话 channel 的绑定和实际入站游标。更新输入不带 revision；expected-revision 使用最新 show 值。摘要与每条事项必须有来源。先读原文再写确认事项，推测放未决问题；程序核验来源存在，不替你判断话语含义。笔记不会自动生成，也不会从模型输出直接写回。

## 从旧单群 runner 迁移

旧的 `wx-agent start/status/pause/resume/stop/run`、`wx-agent chat` 和 `wx-agent-reply` 已移除。先用旧版本正常停止 runner、处理完 pending 回复和交互占用，再安装新包，并对每个曾导入的会话执行一次 `conversations retire-controller --id ID`。原状态目录、consumer、outbox 和请求历史原地保留。完整步骤见[操作指南](OPERATING_GUIDE.md#8-退役旧单群-controller)。**不要删除状态、换 consumer 或换 request ID 绕过拒绝。**

即使 CLI 活着，锁屏、微信注销或权限失效也会阻止发送。本地 `ui_observed` 不等于手机收到；不明发送结果应核对原请求，不做自动重试。
