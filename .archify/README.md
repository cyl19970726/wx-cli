# 仓库架构图

[打开单群渠道架构图](../docs/diagrams/agent-channel.html)。图展示代码中的组件责任和数据流，不表示新包已经部署。原始输入为同目录 `candidate.json`；源码链接固定到实现提交，后续代码变更时应重新核对来源。

使用 Archify 3.1.0 和 Node.js 重建。`ARCHIFY_ROOT` 指向你已安装的 Archify；该工具不是本仓库运行依赖，也不会由本仓库安装到全局。

```sh
node "$ARCHIFY_ROOT/bin/archify.mjs" finalize architecture \
  .archify/architecture-agent-channel-20261016-123000/candidate.json \
  .archify/architecture-agent-channel-20261016-123000/agent-channel.html \
  --repo-root . --quality showcase --json \
  --out-dir .archify/architecture-agent-channel-20261016-123000/rebuild-review
python3 scripts/publish_architecture.py
```

首次生成已通过结构、产物和真实浏览器自动检查。原始产物、校验回执和截图保留本机；公开 HTML 由 `scripts/publish_architecture.py` 仅规范化空白行缩进，便于 Git 检查，因此不是原始产物的逐字节副本。脚本在写入前核验 Archify 回执中的源与产物哈希，`--check` 可在重建后验证公开副本一致性。仓库保留可维护图源和可直接打开的公开 HTML。

## 会话能力规划

[打开会话 CLI 能力图](../docs/diagrams/conversation-capabilities.html)。图以源码 revision `fa3187b6f775f6ffcc0d8c10f9f0c3f83d92e72b` 为基线，区分已实现的通用会话 CLI、v5 授权目标的导航/读取实测、v3 单条发送的历史收件确认，以及当前版本发送/回复闭环尚未完成的边界。现场证据不代表任意目标均可无条件操作。

```sh
node "$ARCHIFY_ROOT/bin/archify.mjs" finalize tree \
  docs/diagrams/conversation-capabilities.tree.json \
  .archify/tree-conversation-capabilities-20261016-general/conversation-capabilities.html \
  --repo-root . --quality showcase --json \
  --out-dir .archify/tree-conversation-capabilities-20261016-general/rebuild-review-YYYYMMDD-HHMMSS
python3 scripts/publish_architecture.py --diagram capabilities \
  --receipt .archify/tree-conversation-capabilities-20261016-general/rebuild-review-YYYYMMDD-HHMMSS/conversation-capabilities.finalize-summary.json
python3 scripts/publish_architecture.py --diagram capabilities --check \
  --receipt .archify/tree-conversation-capabilities-20261016-general/rebuild-review-YYYYMMDD-HHMMSS/conversation-capabilities.finalize-summary.json
```

发布脚本默认使用已通过的 `.archify/tree-conversation-capabilities-20261016-general/revision-3/conversation-capabilities.finalize-summary.json`；`--check` 核对公开 HTML 是否等于该 raw HTML 的空白行规范化副本。重建时必须为 `--receipt` 传入本次新 finalize 产生的 summary。`.archify` raw HTML 与回执留在本机，不进入公开源。每次重复生成需要替换为新的 `--out-dir`；发布脚本不会替代 Archify 的校验。旧的、忽略的 `docs/diagrams/conversation-capabilities.delivery.json` 回执已归档到 `.local/conversations-navigation-20261016/`，不会参与当前检查。
