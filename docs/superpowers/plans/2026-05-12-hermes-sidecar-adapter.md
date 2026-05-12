# Hermes Sidecar Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a first-version sidecar adapter that polls `my-wxauto` bridge events, asks WSL Hermes for a reply per WeChat conversation, and sends the reply back through the bridge.

**Architecture:** Add one focused module, `my_wxauto.hermes_sidecar`, with small units for event formatting, bridge HTTP calls, Hermes subprocess execution, and the polling loop. The adapter runs in Windows, calls the Windows bridge over HTTP, and invokes Hermes through `wsl.exe hermes ...` by default.

**Tech Stack:** Python standard library only (`argparse`, `json`, `subprocess`, `urllib.request`, `urllib.error`, `hashlib`, `time`, `dataclasses`), existing pytest suite.

---

## File Structure

- Create `src/my_wxauto/hermes_sidecar.py`
  - `SidecarConfig`: runtime configuration.
  - `BridgeClient`: standard-library HTTP client for `/health`, `/events`, and `/send`.
  - `HermesRunner`: subprocess wrapper for `hermes chat -q ... -Q`.
  - `format_prompt(event)`: converts one bridge event into a Hermes prompt.
  - `session_name_for_chat(chat_name)`: stable Hermes session name.
  - `HermesSidecar`: orchestration loop.
  - `main(argv=None)`: CLI entry point for `python -m my_wxauto.hermes_sidecar`.
- Create `tests/test_hermes_sidecar.py`
  - Unit tests use fake bridge clients and fake Hermes runners.
  - No real WeChat, no real HTTP server, no real WSL/Hermes dependency.
- Modify `README.md`
  - Add a short command example for running the sidecar.

## Task 1: Prompt Formatting And Session Names

**Files:**
- Create: `src/my_wxauto/hermes_sidecar.py`
- Create: `tests/test_hermes_sidecar.py`

- [ ] **Step 1: Write failing tests for prompt formatting and stable session names**

Add `tests/test_hermes_sidecar.py`:

```python
from __future__ import annotations

from my_wxauto.hermes_sidecar import format_prompt, session_name_for_chat


def _event(chat_name: str = "张三") -> dict[str, object]:
    return {
        "batch_id": "batch-1",
        "chat_name": chat_name,
        "messages": [
            {
                "content": "你好",
                "sender": "张三",
                "is_self": False,
                "time_text": "15:41",
                "message_type": "text",
            },
            {
                "content": "我刚刚发的",
                "sender": None,
                "is_self": True,
                "time_text": "15:42",
                "message_type": "text",
            },
            {
                "content": "在吗",
                "sender": None,
                "is_self": False,
                "time_text": None,
                "message_type": "text",
            },
        ],
    }


def test_format_prompt_includes_chat_and_messages() -> None:
    prompt = format_prompt(_event())

    assert "会话名：张三" in prompt
    assert "- 15:41 张三: 你好" in prompt
    assert "- 15:42 我: 我刚刚发的" in prompt
    assert "- 对方: 在吗" in prompt
    assert "请只输出要发送到微信的回复文本" in prompt


def test_session_name_for_chat_is_stable_and_ascii() -> None:
    first = session_name_for_chat("张三、测试群")
    second = session_name_for_chat("张三、测试群")

    assert first == second
    assert first.startswith("wxauto-")
    assert first.isascii()
    assert len(first) <= 48
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'my_wxauto.hermes_sidecar'`.

- [ ] **Step 3: Implement prompt formatting and session names**

Create `src/my_wxauto/hermes_sidecar.py`:

```python
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Sequence
from urllib import error as urlerror
from urllib import request


def session_name_for_chat(chat_name: str) -> str:
    digest = hashlib.sha1(str(chat_name or "").encode("utf-8")).hexdigest()[:16]
    return f"wxauto-{digest}"


def format_prompt(event: dict[str, Any]) -> str:
    chat_name = str(event.get("chat_name") or "")
    lines = [
        "你正在作为微信机器人回复一个会话。",
        f"会话名：{chat_name}",
        "",
        "本次收到的新消息：",
    ]
    messages = event.get("messages") or []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        sender = _message_sender_label(message)
        time_text = str(message.get("time_text") or "").strip()
        prefix = f"{time_text} {sender}" if time_text else sender
        lines.append(f"- {prefix}: {content}")
    lines.extend(
        [
            "",
            "请只输出要发送到微信的回复文本。不要解释，不要包含前后缀。",
        ]
    )
    return "\n".join(lines)


def _message_sender_label(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, str) and sender.strip():
        return sender.strip()
    if message.get("is_self") is True:
        return "我"
    return "对方"
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```powershell
git add src\my_wxauto\hermes_sidecar.py tests\test_hermes_sidecar.py
git commit -m "Add Hermes sidecar prompt formatting"
```

## Task 2: Bridge HTTP Client

**Files:**
- Modify: `src/my_wxauto/hermes_sidecar.py`
- Modify: `tests/test_hermes_sidecar.py`

- [ ] **Step 1: Write failing tests for bridge HTTP calls using monkeypatched opener**

Append to `tests/test_hermes_sidecar.py`:

```python
import json
from urllib import request

from my_wxauto.hermes_sidecar import BridgeClient


class FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


def test_bridge_client_gets_health(monkeypatch) -> None:
    calls: list[request.Request] = []

    def fake_urlopen(req: request.Request, timeout: float):
        calls.append(req)
        return FakeResponse({"status": "ok", "listener_alive": True})

    monkeypatch.setattr("my_wxauto.hermes_sidecar.request.urlopen", fake_urlopen)

    payload = BridgeClient("http://127.0.0.1:8765").health()

    assert payload["status"] == "ok"
    assert calls[0].full_url == "http://127.0.0.1:8765/health"
    assert calls[0].get_method() == "GET"


def test_bridge_client_polls_events(monkeypatch) -> None:
    calls: list[request.Request] = []

    def fake_urlopen(req: request.Request, timeout: float):
        calls.append(req)
        return FakeResponse({"status": "ok", "count": 0, "events": []})

    monkeypatch.setattr("my_wxauto.hermes_sidecar.request.urlopen", fake_urlopen)

    payload = BridgeClient("http://127.0.0.1:8765/").poll_events(timeout=30, limit=5)

    assert payload == {"status": "ok", "count": 0, "events": []}
    assert calls[0].full_url == "http://127.0.0.1:8765/events?timeout=30&limit=5"


def test_bridge_client_sends_message(monkeypatch) -> None:
    calls: list[request.Request] = []

    def fake_urlopen(req: request.Request, timeout: float):
        calls.append(req)
        return FakeResponse({"status": "success"})

    monkeypatch.setattr("my_wxauto.hermes_sidecar.request.urlopen", fake_urlopen)

    payload = BridgeClient("http://127.0.0.1:8765").send("张三", "你好")

    body = json.loads(calls[0].data.decode("utf-8"))
    assert payload["status"] == "success"
    assert calls[0].full_url == "http://127.0.0.1:8765/send"
    assert calls[0].get_method() == "POST"
    assert body == {"who": "张三", "message": "你好"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: fail because `BridgeClient` is not defined.

- [ ] **Step 3: Implement `BridgeClient`**

Add to `src/my_wxauto/hermes_sidecar.py`:

```python
class BridgeClient:
    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", "/health")

    def poll_events(self, *, timeout: float, limit: int) -> dict[str, Any]:
        path = f"/events?timeout={timeout:g}&limit={int(limit)}"
        return self._request_json("GET", path, request_timeout=timeout + self.timeout)

    def send(self, who: str, message: str) -> dict[str, Any]:
        return self._request_json("POST", "/send", {"who": who, "message": message})

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with request.urlopen(req, timeout=request_timeout or self.timeout) as response:
            raw = response.read()
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("bridge response must be a JSON object")
        return decoded
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: all current tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src\my_wxauto\hermes_sidecar.py tests\test_hermes_sidecar.py
git commit -m "Add Hermes sidecar bridge client"
```

## Task 3: Hermes Runner And Event Processing

**Files:**
- Modify: `src/my_wxauto/hermes_sidecar.py`
- Modify: `tests/test_hermes_sidecar.py`

- [ ] **Step 1: Write failing tests for Hermes runner and event processing**

Append to `tests/test_hermes_sidecar.py`:

```python
import subprocess

from my_wxauto.hermes_sidecar import HermesRunner, HermesSidecar, SidecarConfig


class FakeBridge:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def health(self) -> dict[str, object]:
        return {"status": "ok", "listener_alive": True}

    def poll_events(self, *, timeout: float, limit: int) -> dict[str, object]:
        return {"status": "ok", "count": 0, "events": []}

    def send(self, who: str, message: str) -> dict[str, object]:
        self.sent.append((who, message))
        return {"status": "success"}


class FakeHermes:
    def __init__(self, reply: str = "收到") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def ask(self, prompt: str, *, session_name: str) -> str:
        self.calls.append((prompt, session_name))
        return self.reply


def test_hermes_runner_invokes_command(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, *, text, capture_output, timeout, check):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="好的\n", stderr="")

    monkeypatch.setattr("my_wxauto.hermes_sidecar.subprocess.run", fake_run)

    runner = HermesRunner(["wsl.exe", "hermes"], timeout=60)
    reply = runner.ask("prompt text", session_name="wxauto-abc")

    assert reply == "好的"
    assert calls == [
        [
            "wsl.exe",
            "hermes",
            "chat",
            "-q",
            "prompt text",
            "-Q",
            "--continue",
            "wxauto-abc",
            "--source",
            "tool",
        ]
    ]


def test_sidecar_processes_event_and_sends_reply() -> None:
    bridge = FakeBridge()
    hermes = FakeHermes("你好呀")
    sidecar = HermesSidecar(SidecarConfig(dry_run=False), bridge=bridge, hermes=hermes)

    sidecar.process_event(_event("张三"))

    assert len(hermes.calls) == 1
    assert "会话名：张三" in hermes.calls[0][0]
    assert bridge.sent == [("张三", "你好呀")]


def test_sidecar_dry_run_does_not_send_reply() -> None:
    bridge = FakeBridge()
    hermes = FakeHermes("你好呀")
    sidecar = HermesSidecar(SidecarConfig(dry_run=True), bridge=bridge, hermes=hermes)

    sidecar.process_event(_event("张三"))

    assert len(hermes.calls) == 1
    assert bridge.sent == []


def test_sidecar_empty_reply_does_not_send() -> None:
    bridge = FakeBridge()
    hermes = FakeHermes("   ")
    sidecar = HermesSidecar(SidecarConfig(dry_run=False), bridge=bridge, hermes=hermes)

    sidecar.process_event(_event("张三"))

    assert bridge.sent == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: fail because `HermesRunner`, `HermesSidecar`, or `SidecarConfig` is not defined.

- [ ] **Step 3: Implement runner and event processing**

Add to `src/my_wxauto/hermes_sidecar.py`:

```python
@dataclass(frozen=True)
class SidecarConfig:
    bridge_url: str = "http://127.0.0.1:8765"
    poll_timeout: float = 30.0
    poll_limit: int = 5
    hermes_command: tuple[str, ...] = ("wsl.exe", "hermes")
    hermes_timeout: float = 120.0
    dry_run: bool = False
    once: bool = False
    retry_delay: float = 3.0


class HermesRunner:
    def __init__(self, command: Sequence[str], *, timeout: float) -> None:
        self.command = tuple(command)
        self.timeout = timeout

    def ask(self, prompt: str, *, session_name: str) -> str:
        cmd = [
            *self.command,
            "chat",
            "-q",
            prompt,
            "-Q",
            "--continue",
            session_name,
            "--source",
            "tool",
        ]
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=True,
        )
        return result.stdout.strip()


class HermesSidecar:
    def __init__(
        self,
        config: SidecarConfig,
        *,
        bridge: BridgeClient | Any | None = None,
        hermes: HermesRunner | Any | None = None,
    ) -> None:
        self.config = config
        self.bridge = bridge or BridgeClient(config.bridge_url)
        self.hermes = hermes or HermesRunner(config.hermes_command, timeout=config.hermes_timeout)

    def check_health(self) -> dict[str, Any]:
        payload = self.bridge.health()
        if payload.get("status") != "ok":
            raise RuntimeError(f"bridge is not healthy: {payload}")
        return payload

    def process_event(self, event: dict[str, Any]) -> str | None:
        chat_name = str(event.get("chat_name") or "").strip()
        if not chat_name:
            return None
        prompt = format_prompt(event)
        reply = self.hermes.ask(prompt, session_name=session_name_for_chat(chat_name)).strip()
        if not reply:
            return None
        if self.config.dry_run:
            print(f"[dry-run] {chat_name}: {reply}")
            return reply
        self.bridge.send(chat_name, reply)
        return reply
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: all current tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src\my_wxauto\hermes_sidecar.py tests\test_hermes_sidecar.py
git commit -m "Add Hermes sidecar event processing"
```

## Task 4: Polling Loop And CLI

**Files:**
- Modify: `src/my_wxauto/hermes_sidecar.py`
- Modify: `tests/test_hermes_sidecar.py`

- [ ] **Step 1: Write failing tests for one-shot loop and CLI args**

Append to `tests/test_hermes_sidecar.py`:

```python
from my_wxauto import hermes_sidecar


class EventBridge(FakeBridge):
    def __init__(self, events: list[dict[str, object]]) -> None:
        super().__init__()
        self.events = events
        self.polls: list[tuple[float, int]] = []

    def poll_events(self, *, timeout: float, limit: int) -> dict[str, object]:
        self.polls.append((timeout, limit))
        events = self.events
        self.events = []
        return {"status": "ok", "count": len(events), "events": events}


def test_sidecar_run_once_processes_polled_events() -> None:
    bridge = EventBridge([_event("张三")])
    hermes = FakeHermes("自动回复")
    sidecar = HermesSidecar(
        SidecarConfig(once=True, poll_timeout=1, poll_limit=2),
        bridge=bridge,
        hermes=hermes,
    )

    sidecar.run()

    assert bridge.polls == [(1, 2)]
    assert bridge.sent == [("张三", "自动回复")]


def test_main_builds_sidecar_config(monkeypatch) -> None:
    configs: list[SidecarConfig] = []

    class FakeSidecarMain:
        def __init__(self, config: SidecarConfig):
            configs.append(config)

        def check_health(self):
            return {"status": "ok"}

        def run(self):
            return None

    monkeypatch.setattr(hermes_sidecar, "HermesSidecar", FakeSidecarMain)

    exit_code = hermes_sidecar.main(
        [
            "--bridge-url",
            "http://127.0.0.1:9999",
            "--poll-timeout",
            "2",
            "--poll-limit",
            "3",
            "--hermes-command",
            "wsl.exe hermes",
            "--hermes-timeout",
            "4",
            "--dry-run",
            "--once",
        ]
    )

    assert exit_code == 0
    assert configs[0].bridge_url == "http://127.0.0.1:9999"
    assert configs[0].poll_timeout == 2
    assert configs[0].poll_limit == 3
    assert configs[0].hermes_command == ("wsl.exe", "hermes")
    assert configs[0].hermes_timeout == 4
    assert configs[0].dry_run is True
    assert configs[0].once is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: fail because `run()` or `main()` is not defined.

- [ ] **Step 3: Implement `run()`, parser, and module entry point**

Add to `src/my_wxauto/hermes_sidecar.py`:

```python
    def run(self) -> None:
        while True:
            try:
                payload = self.bridge.poll_events(
                    timeout=self.config.poll_timeout,
                    limit=self.config.poll_limit,
                )
                events = payload.get("events") or []
                for event in events:
                    if isinstance(event, dict):
                        self.process_event(event)
            except (urlerror.URLError, TimeoutError, RuntimeError, subprocess.SubprocessError) as exc:
                print(f"[my-wxauto hermes-sidecar] error: {exc}")
                time.sleep(self.config.retry_delay)
            if self.config.once:
                return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the my-wxauto Hermes sidecar adapter")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--poll-timeout", type=float, default=30.0)
    parser.add_argument("--poll-limit", type=int, default=5)
    parser.add_argument("--hermes-command", default="wsl.exe hermes")
    parser.add_argument("--hermes-timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser


def _split_command(value: str) -> tuple[str, ...]:
    parts = [part for part in value.split(" ") if part]
    return tuple(parts)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = SidecarConfig(
        bridge_url=args.bridge_url,
        poll_timeout=args.poll_timeout,
        poll_limit=args.poll_limit,
        hermes_command=_split_command(args.hermes_command),
        hermes_timeout=args.hermes_timeout,
        dry_run=args.dry_run,
        once=args.once,
    )
    sidecar = HermesSidecar(config)
    sidecar.check_health()
    sidecar.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_hermes_sidecar.py -q
```

Expected: all current tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src\my_wxauto\hermes_sidecar.py tests\test_hermes_sidecar.py
git commit -m "Add Hermes sidecar CLI"
```

## Task 5: Documentation And Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add README usage section**

Add this section after the local HTTP bridge service section in `README.md`:

````markdown
## Hermes sidecar adapter

如果 WSL 中已经安装 Hermes，可以让 sidecar adapter 把微信事件交给 Hermes 思考，再把回复发回微信。

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
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:8765 --dry-run --once
```

第一版 sidecar 按会话顺序处理事件，并为每个微信会话使用独立 Hermes session。它还不支持同会话新消息到达时取消正在生成的旧回复；这个能力会在后续版本中补充。
````

- [ ] **Step 2: Run focused tests**

Run:

```powershell
pytest tests/test_hermes_sidecar.py tests/test_bridge_server.py tests/test_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run full test suite**

Run:

```powershell
pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Smoke test module help**

Run:

```powershell
python -m my_wxauto.hermes_sidecar --help
```

Expected: command exits `0` and prints `--bridge-url`, `--hermes-command`, `--dry-run`, and `--once`.

- [ ] **Step 5: Commit**

```powershell
git add README.md
git commit -m "Document Hermes sidecar adapter"
```

## Task 6: Real Dry-Run Verification

**Files:**
- No source files should be modified.

- [ ] **Step 1: Start bridge server in a background process**

Run from Windows PowerShell:

```powershell
python -m my_wxauto --bridge-server --bridge-host 127.0.0.1 --bridge-port 18765 --store-path .\.hermes-sidecar-smoke.sqlite3 --listen-max-chats 1
```

Expected: command keeps running. Do not close it during the next step.

- [ ] **Step 2: Run sidecar dry-run once**

In another PowerShell:

```powershell
python -m my_wxauto.hermes_sidecar --bridge-url http://127.0.0.1:18765 --dry-run --once
```

Expected:

- If no unread WeChat event is available, command exits after one `/events` poll without sending messages.
- If an unread event is available, command prints a `[dry-run] <chat>: <reply>` line and does not call `/send`.

- [ ] **Step 3: Clean up smoke files**

Stop the bridge process and remove `.hermes-sidecar-smoke.sqlite3` if it exists:

```powershell
Remove-Item .\.hermes-sidecar-smoke.sqlite3 -ErrorAction SilentlyContinue
```

- [ ] **Step 4: Confirm clean worktree**

Run:

```powershell
git status --short
```

Expected: no output.

## Self-Review Checklist

- Spec coverage: tasks cover prompt construction, session isolation, bridge polling, Hermes invocation, `/send`, dry-run, once mode, docs, and real dry-run verification.
- Non-goals preserved: no Hermes source changes, no native Hermes gateway adapter, no media support, no complex in-flight cancellation.
- TDD: every implementation task starts with failing tests before code.
- Boundary check: `hermes_sidecar.py` owns adapter behavior; existing bridge server remains unchanged.
