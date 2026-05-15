# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```powershell
# Install in editable mode
python -m pip install -e ".[dev]"

# Run all tests
python -m pytest

# Run a single test file
python -m pytest tests/test_wechat.py

# Run a specific test function
python -m pytest tests/test_wechat.py -k test_sendmsg_records_outgoing_echo

# Open a WeChat conversation by name
python -m my_wxauto "张三"

# Send a message
python -m my_wxauto "张三" --message "你好"

# Diagnose detected WeChat processes/windows
python -m my_wxauto --diagnose

# Run debug batch listener (prints deduplication diagnostics)
python -m my_wxauto --listen-batches --listen-seconds 30 --listen-max-chats 5
```

## Architecture overview

`my-wxauto` is a Windows WeChat 4.x automation layer. It does **not** rely on stable UIAutomation control trees (which are often broken in Qt Quick/QML). Instead it uses a "human operator" approach: find the window, click/paste/hotkey to search and send messages.

### Source layout

All production code lives in `src/my_wxauto/`. The `my_wxauto/` at the repo root is a thin compatibility shim.

| Module | Role |
|---|---|
| `wechat.py` | Public `WeChat` facade — `ChatWith()`, `SendMsg()`, `listen_conversation_batches()` |
| `window.py` | `WeChatWindow` dataclass + `WeChatWindowController` (enumerate/activate/wait for WeChat windows via win32 API) |
| `keyboard.py` | `KeyboardController` — hotkeys, paste-via-clipboard, mouse click via win32 API |
| `tray.py` | `TrayIconRestorer` — restore WeChat from system tray using pywinauto UIA |
| `cli.py` | `argparse` CLI with flags for all modes (chat, send, diagnose, listen, bridge server, probes) |
| `response.py` | `WxResponse` dataclass (success/failure/error, used by `ChatWith`/`SendMsg`) |
| `listener.py` | Core listener: flash detection → open unread chats → collect UIA message controls → optional sender resolution (profile-card click) → batch delivery |
| `probes.py` | Low-level probes: screen capture (BGRA pixel buffers), red-badge detection, UIA control collection, taskbar icon inspection, WinEvent hooks |
| `bridge_events.py` | `BridgeMessage`, `ConversationBatch`, SHA-256 message key generation for dedup |
| `bridge_batcher.py` | `ConversationBatcher` — accumulates messages per chat, freezes batches on quiet window (1.5s) or max size (10) or max wait (8s) |
| `bridge_store.py` | SQLite store for seen-message dedup, conversation batch tracking, outgoing echo suppression |
| `bridge_server.py` | `ThreadingHTTPServer` with `GET /health`, `GET /events?timeout=30&limit=5`, `POST /send` — wraps the batch listener in an HTTP API |
| `hermes_sidecar.py` | Polls bridge `/events`, formats WeChat messages as Hermes prompts, calls `wsl.exe hermes chat`, posts replies back via `/send` |
| `exceptions.py` | `WxAutoError`, `WeChatWindowNotFoundError`, `WindowActivationError`, `ClipboardError` |
| `debug_trace.py` | `make_ui_tracer()` — emits `MY_WXAUTO_TRACE` JSON lines with foreground window/cursor snapshots |
| `wxauto4_backend.py` | Optional wrapper around third-party wxauto4 for window prep and legacy `ChatWith`/`SendMsg` |

### Data flow (batch listener)

```
TaskbarFlashDetector (pixel signature changes)
  → probes._probe_sessions_after_wakeup_with_timeout (spawns subprocess)
    → open unread sessions one-by-one (click session, collect UIA message controls)
    → optionally resolve senders via profile-card clicks
  → listener callback (on_chat_opened)
    → bridge_events.messages_from_chat_payload → BridgeMessage with SHA-256 keys
    → ConversationBatcher.add_messages (dedup, accumulate)
    → ConversationBatcher.freeze_due_batches (quiet-window or size trigger)
    → emit via callback or HTTP queue
```

### Two package locations

- `src/my_wxauto/` — the installable package (`pyproject.toml` uses `tool.setuptools.packages.find where = ["src"]`)
- `my_wxauto/` — thin shim at repo root for `python -m my_wxauto` from the project directory

When adding new modules, place them in `src/my_wxauto/`. Update `src/my_wxauto/__init__.py` to re-export public symbols.

### Third-party dependency

`third_party/wxauto4-41.1.2/` contains a vendored copy of wxauto4 used by `wxauto4_backend.py`. This is an optional backend for window restore and legacy ChatWith/SendMsg — the main automation path uses the project's own window/keyboard controllers.

### Key design decisions

- **No stable UIA for targeting**: WeChat 4.x uses Qt Quick, so UIA control trees are sparse. The tool searches by pasting names into the search box rather than reading a contact list.
- **Sender resolution is opt-in**: `resolve_senders="profile_card"` clicks message avatars and reads profile-card UIA names. It's slow and disturbs the UI — disabled by default.
- **Subprocess isolation for wakeup probes**: The flash→restore→read cycle runs in a `multiprocessing.spawn` child to bound UI hangs with a timeout.
- **Self-message pixel detection**: `_annotate_messages_with_self_flags` samples screen pixels to detect the green WeChat self-bubble color, avoiding UIA reliance for is_self.
- **Outgoing echo suppression**: `BridgeStore.record_outgoing_echo` stores sent messages so the listener doesn't treat the robot's own replies as new incoming messages.
- **Per-conversation batching**: Each `ConversationBatch` belongs to exactly one chat. The bridge server's `/events` returns individual batches — callers should process one chat at a time, not combine them into a single model request.
