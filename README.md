# my-wxauto

`my-wxauto` 是一个面向新版 Windows 微信客户端的自动化兼容层。当前目标是给微信机器人提供基础能力：打开会话、发送消息、监听未读消息，并通过本地 HTTP 桥接服务接入 Hermes/OpenClaw 等外部机器人。

## 快速开始

在项目根目录运行。下面几个命令是日常测试最常用的入口。

打开一个联系人或群聊：

```powershell
python -m my_wxauto "张三"
```

发送一条消息：

```powershell
python -m my_wxauto "张三" --message "你好"
```

只测试发送前的会话定位，不真正发送消息：

```powershell
python -m my_wxauto "张三" --message "发送前定位测试" --send-dry-run --trace-ui --output send-dry-run.txt
```

查看当前能识别到的微信进程和窗口：

```powershell
python -m my_wxauto --diagnose
```

## 命令行能力

默认会借助 `wxauto4` 恢复或置前最小化、托盘状态的微信窗口，但搜索、打开会话、粘贴发送由本项目控制。默认流程是：

1. 恢复并激活微信窗口。
2. 使用 `Ctrl+F` 聚焦搜索框。
3. 粘贴联系人或群聊名称。
4. 优先点击搜索结果里的聊天入口。
5. 打开会话后粘贴消息并回车发送。

搜索结果里如果同时出现“聊天记录”“搜索网络结果”和真正的联系人/群聊，本项目会优先选择 `最常使用 / 联系人 / 群聊` 分组下的精确匹配，避免点进聊天记录弹窗或网络搜索结果。

如果搜索快捷键不适配当前微信版本，可以改为坐标点击搜索框：

```powershell
python -m my_wxauto "张三" --no-shortcut --click-search-box
```

如果当前微信版本需要先按方向键下再回车，可以显式指定：

```powershell
python -m my_wxauto "张三" --search-down-count 1
```

## 发送定位诊断

如果怀疑消息发错会话，先不要直接真实发送，先执行 dry-run：

```powershell
python -m my_wxauto "张三" --message "发送前定位测试" --send-dry-run --trace-ui --output send-dry-run.txt
```

重点查看 `send-dry-run.txt` 里的两类日志：

- `search_result.candidates`：本次搜索识别到的候选控件。
- `search_result.selected`：最终选择的候选项和点击坐标。

确认 dry-run 打开的是正确会话后，再执行真实发送：

```powershell
python -m my_wxauto "张三" --message "真实发送测试" --trace-ui --output send-real-test.txt
```

## 本地 HTTP 桥接服务

桥接服务负责监听微信未读消息、按会话批次输出事件，并提供 `/send` 接口让外部机器人把回复发回微信。

推荐在独立终端启动桥接服务。这个命令会一直运行：

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 8765 --store-path .\.wxauto-bridge.sqlite3 --bridge-queue-size 1000 --listen-max-chats 5
```

参数含义：

- `--bridge-host 127.0.0.1`：只监听本机。
- `--bridge-port 8765`：HTTP 服务端口。
- `--store-path .\.wxauto-bridge.sqlite3`：桥接事件和状态的 SQLite 文件。
- `--bridge-queue-size 1000`：内存事件队列大小。没有及时消费 `/events` 时，队列过小会增加监听线程报 `queue.Full` 的概率。
- `--listen-max-chats 5`：每轮最多打开 5 个未读会话。

另开一个终端验证服务是否启动：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

只验证桥接服务的发送能力：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/send -ContentType "application/json; charset=utf-8" -Body (@{ who = "张三"; message = "桥接发送测试" } | ConvertTo-Json -Compress)
```

## Hermes Sidecar Adapter

如果 WSL 中已经安装 Hermes，可以启动 sidecar adapter，把微信事件交给 Hermes 生成回复，再把回复通过 bridge 发回微信。

启动顺序建议如下。

终端 1：启动 Windows 微信桥接服务：

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 8765 --store-path .\.wxauto-bridge.sqlite3 --bridge-queue-size 1000 --listen-max-chats 5
```

终端 2：首次联调用 dry-run，只打印 Hermes 准备回复的内容，不真正发送微信消息，也不会 ack/complete 事件：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765 --dry-run --once --debug
```

确认 dry-run 正常后，启动正式 sidecar：

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765
```

sidecar 会为每个微信会话维护独立 Hermes session。默认 session 文件在：

```text
~/.wxauto/hermes_sessions.json
```

## HTTP API

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

`/events` 返回的每个 event 都只属于一个微信会话，外部机器人应逐会话处理，不要把多个会话混进同一个模型请求。

事件生命周期：

- `frozen`：监听器已生成会话批次，等待外部机器人确认处理。
- `submitted`：外部机器人已通过 `/events/{batch_id}/ack` 确认开始处理。
- `completed`：外部机器人已完成处理，并通过 `/events/{batch_id}/complete` 确认。

`/events` 会返回尚未完成的 `frozen` 或 `submitted` 事件，但不会改变事件状态。外部机器人开始处理前应调用 `ack`，发送回复成功后应调用 `complete`，否则该事件会保留为可重试状态。sidecar 的 `--dry-run` 模式不会调用 `ack` 或 `complete`。

## Python API

```python
from my_wxauto import WeChat

wx = WeChat()
wx.ChatWith("张三")
wx.SendMsg("你好", "张三")
```

监听会话批次：

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

## 诊断建议

PowerShell 管道有时会让中文输出乱码，建议用 `--output` 直接写 UTF-8 文件：

```powershell
python -m my_wxauto "张三" --message "发送前定位测试" --send-dry-run --trace-ui --output send-dry-run.txt
```

如果桥接服务出现 `queue.Full`，通常表示监听线程生成事件的速度超过了 `/events` 消费速度。短期可以调大 `--bridge-queue-size`，并尽快启动 sidecar 或其他消费者。后续需要继续补 bridge 事件 lease 和队列满时的兜底处理。

## 设计取舍

新版微信 4.x 的界面大量迁移到 Qt Quick/QML 后，传统 UIAutomation 控件树经常不可用。因此本项目优先采用接近真人操作的路径：恢复窗口、聚焦搜索、粘贴名称、打开会话、粘贴并发送消息。

这类自动化天然无法像旧版 UIA 那样强校验“精确匹配”。调用方如需更高确定性，应传入足够唯一的联系人或群聊名称，并优先通过 dry-run 和 trace 日志验证。

## 免责声明

本工具仅供学习研究使用。使用者应遵守微信用户协议及相关法律法规，并自行承担使用本工具产生的风险与责任。
