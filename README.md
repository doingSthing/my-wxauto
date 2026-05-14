# my-wxauto

`my-wxauto` 是一个面向新版 Windows 微信客户端的自动化兼容层。当前核心目标是给微信机器人提供基础能力：打开会话、发送消息、监听未读消息，并通过本地 HTTP 桥接服务接入 Hermes/OpenClaw 等外部机器人。

```python
from my_wxauto import WeChat

wx = WeChat()
wx.ChatWith("张三")
wx.SendMsg("你好", "张三")
```

## 命令行

在项目根目录可以直接运行：

```powershell
python -m my_wxauto "张三"
```

发送文本消息：

```powershell
python -m my_wxauto "张三" --message "你好"
```

如果怀疑发送前找错了会话，先单独测试“发送前定位会话”这一步。下面命令会打开目标会话并输出搜索候选和最终点击项，但不会真正发送消息：

```powershell
python -m my_wxauto "张三" --message "发送前定位测试" --send-dry-run --trace-ui --output send-dry-run.txt
```

执行后重点看 `send-dry-run.txt` 里的两类日志：

- `search_result.candidates`：本次搜索识别到的候选控件。
- `search_result.selected`：最终准备点击的候选项和坐标。

查看当前能识别到的微信进程和窗口：

```powershell
python -m my_wxauto --diagnose
```

默认会借助 `wxauto4` 恢复/置前最小化或托盘状态的微信窗口，但搜索动作由本项目自己完成，默认使用 `Ctrl+F` 聚焦搜索框，再粘贴联系人或群聊名称并回车。

搜索结果里如果同时出现“搜网络结果”和真正的联系人/群聊，本项目会优先点击“联系人/群聊”分组下的精确匹配项；无法可靠识别时才回退到回车打开当前选中项。

如果搜索快捷键不适配当前微信版本，可以只用点击搜索框：

```powershell
python -m my_wxauto "张三" --no-shortcut --click-search-box
```

如果你的搜索结果需要先按方向键下再回车，可以显式开启：

```powershell
python -m my_wxauto "张三" --search-down-count 1
```

## Conversation Batch Listener

`my-wxauto` 提供面向机器人集成的会话批次监听能力。它会监听微信未读信号，按会话打开未读聊天，读取消息，做去重和批处理，然后一次输出一个会话批次。

```python
from my_wxauto import WeChat

wx = WeChat()

def on_batch(batch):
    print(batch.to_event_dict())

wx.listen_conversation_batches(
    on_batch,
    max_chats_per_drain=5,
)
```

发送人解析默认关闭，因为 `profile_card` 模式会点击消息头像、读取资料卡，速度更慢，也会短暂打扰微信界面。确实需要群聊发送人时再开启：

```python
wx.listen_conversation_batches(
    on_batch,
    max_chats_per_drain=5,
    resolve_senders="profile_card",
    sender_resolve_limit=5,
)
```

## 本地 HTTP 桥接服务

桥接服务负责监听微信、去重、按会话批次输出事件，并提供 `/send` 让外部机器人把回复发回微信。

推荐测试启动顺序：

1. 终端 1 启动桥接服务。这个命令会一直运行，不要关闭窗口：

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 8765 --store-path .\.wxauto-bridge.sqlite3 --bridge-queue-size 100 --listen-max-chats 5
```

2. 终端 2 验证桥接服务是否启动成功：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

3. 首次联调 Hermes 前，建议先用 dry-run。它只会打印 Hermes 准备回复的内容，不会真的发送微信消息，也不会 ack/complete 事件：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765 --dry-run --once --debug
```

4. dry-run 确认正常后，再启动正式 sidecar。这个命令也会一直运行，收到事件后会调用 Hermes，并把回复发回微信：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765
```

如果只想验证桥接服务的发送能力，可以在桥接服务运行时执行：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/send -ContentType "application/json; charset=utf-8" -Body (@{ who = "张三"; message = "桥接发送测试" } | ConvertTo-Json -Compress)
```

启动服务：

```powershell
python -m my_wxauto --bridge-server
```

常用参数：

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 8765 --store-path .\.wxauto-bridge.sqlite3 --bridge-queue-size 100 --listen-max-chats 5
```

HTTP API：

```text
GET  http://127.0.0.1:8765/health
GET  http://127.0.0.1:8765/events?timeout=30&limit=5
POST http://127.0.0.1:8765/events/{batch_id}/ack
POST http://127.0.0.1:8765/events/{batch_id}/complete
POST http://127.0.0.1:8765/send
```

`/send` 请求体示例：

```json
{ "who": "张三", "message": "你好" }
```

默认一次最多处理 5 个未读会话，桥接队列默认最多 100 条事件。`/events` 返回的每个 event 都只属于一个微信会话，外部机器人应逐会话处理，不要把多个会话混进同一个模型请求。

事件生命周期：

- `frozen`：监听器已生成会话批次，等待外部机器人确认处理。
- `submitted`：外部机器人已通过 `/events/{batch_id}/ack` 确认开始处理。
- `completed`：外部机器人已完成处理，并通过 `/events/{batch_id}/complete` 确认。

`/events` 会返回尚未完成的 `frozen` 或 `submitted` 事件，但不会改变事件状态。外部机器人开始处理前应调用 `ack`，发送回复成功后应调用 `complete`，否则该事件会保留为可重试状态。sidecar 的 `--dry-run` 模式不会调用 `ack` 或 `complete`。

## Hermes Sidecar Adapter

如果 WSL 中已经安装 Hermes，可以启动 sidecar adapter，把微信事件交给 Hermes 思考，再把回复发回微信。

先启动 Windows 微信桥：

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 8765
```

再启动 sidecar：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765
```

首次验证建议使用 dry-run，不真正发送微信消息：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765 --dry-run --once --debug
```

sidecar 会为每个微信会话维护独立 Hermes session。默认 session 文件在：

```text
~/.wxauto/hermes_sessions.json
```

## 设计取舍

新版微信 4.x 的界面大量迁移到 Qt Quick/QML 后，传统 UIAutomation 控件树经常不可用。因此本项目优先采用接近真人操作的路径：恢复窗口、聚焦搜索、粘贴名称、回车打开会话、粘贴并发送消息。

这类自动化天然无法像旧版 UIA 那样强校验“精确匹配”。调用方如需更高确定性，应传入足够唯一的联系人或群聊名称，并优先通过 dry-run 和 debug 日志验证。

## 免责声明

本工具仅供学习研究使用。使用者应遵守微信用户协议及相关法律法规，并自行承担使用本工具产生的风险与责任。
