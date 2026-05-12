# Hermes Sidecar Adapter 第一版设计

日期：2026-05-12

## 目标

为 `my-wxauto` 增加一个独立运行的 sidecar adapter，让已经安装在 WSL 中的 Hermes Agent 可以非侵入式接入 Windows 桌面微信。

第一版只做最小闭环：

- 从 `my-wxauto` 本地 HTTP 桥接服务拉取微信会话批次。
- 把每个会话批次整理成 Hermes prompt。
- 调用 WSL 中的 `hermes chat -q ... -Q` 获取回复。
- 通过 `my-wxauto` 的 `/send` 接口把回复发回原微信会话。
- 每个微信会话使用独立 Hermes session，避免上下文串味。

## 背景

当前 `my-wxauto` 已经提供本地 HTTP 桥接服务：

- `GET /health`: 检查服务状态。
- `GET /events?timeout=30&limit=5`: 拉取会话批次事件。
- `POST /send`: 向指定微信会话发送文本消息。

用户的 WSL 中已安装 Hermes：

- Hermes 命令：`/home/zhangxun/.local/bin/hermes`
- Hermes 源码：`/home/zhangxun/.hermes/hermes-agent`
- 可用脚本入口：`hermes chat -q "<prompt>" -Q`

Hermes 自身有 gateway 平台体系，也有 `gateway/platforms/weixin.py`，但该实现面向腾讯 iLink Bot API，不适合直接复用桌面微信自动化桥接。

## 非目标

第一版不做以下事情：

- 不修改 Hermes 源码。
- 不实现 Hermes gateway 原生平台 adapter。
- 不引入新的长期后台服务管理器。
- 不支持图片、文件、语音或富文本。
- 不处理多账号、多 Windows 用户或公网访问。
- 不做复杂 prompt 编排、工具调用编排或主动任务调度。
- 不对外暴露新 HTTP 服务。

## 方案选择

采用独立 sidecar 脚本运行在 Windows 工程中，由它负责协调 Windows 侧 `my-wxauto` bridge 和 WSL 侧 Hermes CLI。

选择理由：

- 对 Hermes 零侵入，便于快速验证真实微信自动回复闭环。
- `my-wxauto` 仍然只负责微信 UI 自动化和本地 HTTP 能力。
- Hermes 仍然通过自己的 CLI、配置、模型和 session 管理执行思考。
- 后续如果稳定，再升级成 Hermes gateway 原生平台 adapter。

## 运行方式

第一版预期由两个进程组成。

Windows 进程启动微信桥：

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 8765
```

adapter 进程启动后连接该 bridge：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765
```

如果 adapter 运行在 WSL 中，可将 bridge 绑定到 `0.0.0.0`，再通过 Windows host 地址访问 bridge；如果 adapter 运行在 Windows 中，则由它通过 `wsl.exe hermes ...` 调用 Hermes。

第一版推荐 adapter 运行在 Windows 中，因为 `/send` 触发的是 Windows 微信 UI 操作，日志、进程管理和本地调试更直接。

## 数据流

1. adapter 周期性调用 `GET /events?timeout=30&limit=5`。
2. bridge 返回零个或多个 `ConversationBatch` 事件。
3. adapter 对每个事件按 `chat_name` 分组处理。
4. adapter 将该会话本批消息整理成 prompt。
5. adapter 调用 Hermes：

   ```powershell
   wsl.exe hermes chat -q "<prompt>" -Q --continue "wxauto-<session-key>" --source tool
   ```

6. adapter 从 stdout 读取 Hermes 最终回复。
7. 如果回复非空，adapter 调用：

   ```http
   POST /send
   {"who": "<chat_name>", "message": "<reply>"}
   ```

8. bridge 发送成功后，adapter 继续拉取下一批事件。

## 会话与去重

`my-wxauto` bridge 已经负责消息 key 和已处理消息去重。sidecar 第一版不再单独持久化消息游标。

adapter 负责 Hermes session 隔离：

- 每个微信 `chat_name` 映射为一个 Hermes session 名。
- session 名使用稳定转义或 hash，避免中文、空格、特殊符号影响 CLI。
- 同一个微信会话的后续消息使用同一个 Hermes `--continue` session。

如果 bridge 重启，已处理消息仍由 bridge 的 SQLite 状态库决定，不由 adapter 决定。

## Prompt 格式

第一版 prompt 保持简单、可读：

```text
你正在作为微信机器人回复一个会话。
会话名：张三

本次收到的新消息：
- 15:41 张三: 你好
- 15:42 张三: 在吗

请只输出要发送到微信的回复文本。不要解释，不要包含前后缀。
```

字段规则：

- `sender` 存在时显示发送人。
- `sender` 缺失但 `is_self=true` 时显示“我”。
- `sender` 缺失且不是自己发送时显示“对方”。
- 文件、图片等非纯文本消息先用当前 bridge 的 `content` 字段原样放入 prompt。

## 并发与过时回复

第一版采用单线程顺序处理：

- 一次从 `/events` 最多取 `limit` 个事件。
- 按返回顺序逐个调用 Hermes。
- Hermes 思考期间，bridge 仍会继续监听微信并把新事件放入队列。

已讨论过的策略是“旧回复作废，合并新消息后重新思考”。这一策略需要 adapter 维护 in-flight 状态和 per-chat debounce，复杂度较高。第一版先不实现该策略，只保留扩展点：

- 后续可为每个 `chat_name` 维护 pending batch。
- 如果同会话在 Hermes 运行期间又出现新消息，可丢弃旧回复并重新构造 prompt。

第一版的限制会写入 README 或命令帮助，避免误以为已经支持复杂并发取消。

## 错误处理

adapter 第一版采用保守策略：

- `/health` 不可用：启动时直接报错退出。
- `/events` 请求失败：记录错误，短暂退避后重试。
- Hermes 命令失败或超时：记录错误，不发送微信回复。
- Hermes 输出为空：不发送微信回复。
- `/send` 失败：记录错误，不自动重试无限次。

默认 Hermes 单次调用超时建议为 120 秒，可通过 CLI 参数调整。

## 配置项

第一版 adapter CLI 参数：

- `--bridge-url`: 默认 `http://127.0.0.1:8765`
- `--poll-timeout`: 默认 `30`
- `--poll-limit`: 默认 `5`
- `--hermes-command`: 默认 `wsl.exe hermes`
- `--hermes-timeout`: 默认 `120`
- `--dry-run`: 只打印 Hermes 回复，不调用 `/send`
- `--once`: 只处理一轮事件后退出，便于测试

## 测试策略

自动化测试不依赖真实微信或真实 Hermes。

测试覆盖：

- event batch 到 prompt 的转换。
- `chat_name` 到 Hermes session 名的稳定映射。
- adapter 能调用 fake Hermes runner 并把回复 POST 到 fake bridge。
- Hermes 失败时不会发送消息。
- `dry-run` 模式不会调用 `/send`。
- `once` 模式会按预期退出。

真实验证：

1. Windows 启动 `my-wxauto --bridge-server`。
2. adapter 使用 `--dry-run --once` 拉取一批真实微信事件，确认 prompt 和 Hermes 输出。
3. 去掉 `--dry-run`，对测试会话完成一次真实自动回复。

## 后续演进

当 sidecar 第一版稳定后，可以考虑：

- per-chat in-flight 取消与合并新消息。
- adapter 自己维护轻量 pending 状态。
- 常驻 Hermes gateway 原生平台 adapter。
- 支持图片、文件、语音等消息类型。
- 增加控制命令，例如暂停某个会话自动回复。
