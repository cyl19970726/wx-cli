# wx-cli 0.7.4 源码审查与本地测试

审查基线：[pandorafuture/wx-cli @ 2abe708f55bfe135539a385df856fdc58f97fc74](https://github.com/pandorafuture/wx-cli/tree/2abe708f55bfe135539a385df856fdc58f97fc74)。本 fork 的新增 channel / outbox 不属于原版审查结论，需单独验证。

## 结论与覆盖范围

在所审应用源码、构建脚本、CI、文件读写、命令执行和网络路径中，未发现明确的隐藏外传、开机持久化或恶意载荷。已审的是固定提交，不是对所有依赖源码、预编译发布包或未来更新的绝对安全保证。未使用上游预编译包，未安装仓库中的 skill，未执行密钥提取、调试器附加、微信重启、系统保护设置修改或真实聊天查询。

默认 HTTP 客户端访问本机服务；用户可以配置其他服务器地址。密钥提取通过 LLDB 或进程内存读取，数据库解析不依赖 OCR。原版不提供微信消息发送能力。

## 与本地解析相关的发现

| 发现 | 源码位置（基线提交） | 本轮处理 |
| --- | --- | --- |
| 密钥以明文 TOML 保存，解密聊天数据库落本机缓存，默认创建未强制私有权限 | `wx-keychain/src/store.rs`、`wx-context/src/cache.rs` | 合成测试在私有目录中运行；未导入真实密钥 |
| 默认 HTTP 服务无 token 可访问，路由配置 permissive CORS；非回环地址要求显式 token，但仍是 HTTP | `wx-cli/src/cmd/serve/auth.rs`、`routes.rs`、`mod.rs` | 本轮不启动 HTTP 服务；浏览器是否可访问还受其本地网络策略约束 |
| keystore 加载会迁移并删除旧配置，`--data-dir` 不隔离全局缓存 | `wx-keychain/src/store.rs`、`wx-paths/src/migration.rs`、`wx-context/src/cache.rs` | 不运行默认账号查询；合成验证直接使用指定目录的 `db-dev` |
| 密钥提取可能重启微信并扫描多个账户盐值；LLDB 脚本输出派生函数的输入 | `crates/wx-keychain/src/lldb.rs`、`script.rs`、`process.rs` | 未执行；当前机器 SIP 开启且无现成测试密钥，真实解析未验证 |
| server stop 使用已保存 PID 发信号前缺少进程身份核对，运行配置/参数可能包含 key/token | `crates/wx-cli/src/cmd/server/manager.rs`、`types.rs` | 不使用 server 命令 |
| 原版 watch 是会话更新通知；跨分片消息结果无全局去重，增量缓存忽略部分 WAL 错误 | `wx-monitor/src`、`wx-db/src/messages.rs` | 不能直接作为无遗漏的逐条消息订阅；在 fork 中另建持久日志 |

这些属于可审阅的风险或实现缺陷，不等同于已经发生恶意行为。

## 依赖公告核对

Cargo.lock 有 347 个注册表依赖，均来自 crates.io。对名称和版本调用 OSV 批量查询，6 个包命中公告。只向查询服务发送公开包名和版本，没有发送用户信息。版本命中不证明应用可触发漏洞。

| 包 | 公告 | 与当前路径的关系 |
| --- | --- | --- |
| ansi_term 0.12.1 | [RUSTSEC-2021-0139](https://rustsec.org/advisories/RUSTSEC-2021-0139.html) | 停止维护；默认音频功能的 bindgen 构建依赖 |
| atty 0.2.14 | [RUSTSEC-2021-0145](https://rustsec.org/advisories/RUSTSEC-2021-0145.html)、[2024-0375](https://rustsec.org/advisories/RUSTSEC-2024-0375.html) | 构建侧；未对齐读取公告限定 Windows，另有停止维护公告 |
| anyhow 1.0.102 | [RUSTSEC-2026-0190](https://rustsec.org/advisories/RUSTSEC-2026-0190.html) | 构建 / 宏侧；受影响 downcast_mut 调用未在本应用路径证实 |
| crossbeam-epoch 0.9.18 | [RUSTSEC-2026-0204](https://rustsec.org/advisories/RUSTSEC-2026-0204.html) | 经 rayon 进入运行依赖；受影响指针格式化调用未证实 |
| lru 0.12.5 | [RUSTSEC-2026-0002](https://rustsec.org/advisories/RUSTSEC-2026-0002.html)、[2026-0253](https://rustsec.org/advisories/RUSTSEC-2026-0253.html) | 本项目可见 get/put；公告涉及 IterMut/pop，方法级触发未证实 |
| rustls 0.23.40 | [RUSTSEC-2026-0285](https://rustsec.org/advisories/RUSTSEC-2026-0285.html) | 客户端自定义 HTTPS 地址使用 TLS；默认本机 HTTP 不走该路径 |

本次保持锁文件原样，没有偷偷更新依赖后把新结果归给原版。发布或启用网络服务前应独立升级和回归。

## 实际验证

- macOS arm64，Rust 1.96.0；完整 clone，Git 对象检查通过。
- 固定锁文件从源码 release 构建成功；版本输出为 `wx-cli 0.7.4 (2abe708 2026-10-13)`。
- 构建及执行阶段限制网络、私人用户文件内容读取和审查目录之外的写入；允许构建所需文件元数据访问。用合成 canary 验证读写拒绝及网络拒绝，未以聊天数据探测边界。
- `wx-db`、`wx-decrypt`、`wx-monitor` 默认测试：**300 通过、0 失败、8 跳过**。跳过项为 2 个慢速加密集成、1 个文件系统通知测试和 5 个文档示例。随后显式运行 2 个慢速加密 / 监听集成：**2 通过、0 失败**；其余 6 个跳过项未运行。
- 实际 CLI `db-dev` 对合成 SQLite：4 个联系人、2 个会话、3 条群消息、1 条私聊均符合预期。该测试证明解析入口能运行，不证明真实微信版本兼容或持续订阅可用。
- `doctor` 正常执行但报告 SIP 及 DevToolsSecurity 前置条件不满足；命令退出 0 **不能**解释成所有检查通过。

私有构建日志、模拟数据库及二进制留在本地忽略目录。真实微信数据库、聊天正文及密钥不进入公开仓库。
