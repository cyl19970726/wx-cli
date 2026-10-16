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

[打开会话 CLI 能力图](../docs/diagrams/conversation-capabilities.html)。该图明确区分当前已验证单群能力与通用会话 CLI 的未来目标；以图中的源码 revision 为证据基线。

```sh
node "$ARCHIFY_ROOT/bin/archify.mjs" finalize tree \
  docs/diagrams/conversation-capabilities.tree.json \
  .archify/tree-conversation-capabilities-20261016-155423/conversation-capabilities.html \
  --repo-root . --quality showcase --json \
  --out-dir .archify/tree-conversation-capabilities-20261016-155423/rebuild-review
python3 scripts/publish_architecture.py --diagram capabilities \
  --receipt .archify/tree-conversation-capabilities-20261016-155423/rebuild-review/conversation-capabilities.finalize-summary.json
python3 scripts/publish_architecture.py --diagram capabilities --check \
  --receipt .archify/tree-conversation-capabilities-20261016-155423/rebuild-review/conversation-capabilities.finalize-summary.json
```

规范化规则与上图相同，原始校验回执和本机路径不进入公开源。每次重复生成需要使用新的 `--out-dir`；发布脚本不会替代 Archify 的校验。
