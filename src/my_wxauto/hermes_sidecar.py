from __future__ import annotations

import argparse
import json
import os
import hashlib
import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error as urlerror, parse, request


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def session_name_for_chat(chat_name: Any) -> str:
    normalized_chat_name = _normalize_text(chat_name)
    digest = hashlib.sha1(normalized_chat_name.encode("utf-8")).hexdigest()[:16]
    return f"wxauto-{digest}"


class BridgeRequestError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", f"{self.base_url}/health")

    def poll_events(self, timeout: float, limit: int) -> dict[str, Any]:
        query = parse.urlencode({"timeout": timeout, "limit": limit})
        return self._request_json(
            "GET",
            f"{self.base_url}/events?{query}",
            timeout=timeout + self.timeout,
        )

    def send(self, who: str, message: str) -> dict[str, Any]:
        body = json.dumps({"who": who, "message": message}, ensure_ascii=False).encode("utf-8")
        return self._request_json(
            "POST",
            f"{self.base_url}/send",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=max(self.timeout, 60.0),
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        req = request.Request(url, data=data, headers=headers or {}, method=method)
        with request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not isinstance(result, dict):
            raise BridgeRequestError("bridge response must be a JSON object")

        return result


class BridgeLike(Protocol):
    def health(self) -> dict[str, Any]:
        ...

    def poll_events(self, timeout: float, limit: int) -> dict[str, Any]:
        ...

    def send(self, who: str, message: str) -> dict[str, Any]:
        ...


class HermesLike(Protocol):
    def ask(self, prompt: str, *, session_name: str) -> str:
        ...


@dataclass(frozen=True)
class SidecarConfig:
    bridge_url: str = "http://127.0.0.1:8765"
    poll_timeout: float = 30.0
    poll_limit: int = 5
    hermes_command: tuple[str, ...] = ("wsl.exe", "bash", "-lc", "hermes")
    hermes_timeout: float = 120.0
    dry_run: bool = False
    once: bool = False
    retry_delay: float = 3.0
    debug: bool = False
    session_file: str = ""



class SessionStore:
    def __init__(self, path):
        self._path, self._data = path, {}
    def get(self, name):
        if not self._data: self._load()
        return self._data.get(name)
    def set(self, name, sid):
        if not self._data: self._load()
        self._data[name] = sid
        self._save()
    def delete(self, name):
        if not self._data: self._load()
        self._data.pop(name, None)
        self._save()
    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as fh:
                self._data = json.load(fh)
        except: self._data = {}
    def _save(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)

class HermesRunner:
    def __init__(self, command, *, timeout, session_store=None):
        self.command = tuple(command)
        self.timeout = timeout
        self._session_store = session_store

    def ask(self, prompt, *, session_name):
        sid = None
        if self._session_store is not None:
            sid = self._session_store.get(session_name)
        if sid is not None:
            try:
                result = subprocess.run(
                    self._build_args(prompt, session_name, continue_id=sid),
                    text=True, encoding="utf-8", errors="replace",
                    capture_output=True, timeout=self.timeout, check=True,
                )
                return self._parse_reply(result, session_name)
            except subprocess.CalledProcessError as exc:
                if exc.stderr and "No session found" in str(exc.stderr):
                    if self._session_store is not None:
                        self._session_store.delete(session_name)
        result = subprocess.run(
            self._build_args(prompt, session_name),
            text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=self.timeout, check=True,
        )
        return self._parse_reply(result, session_name)

    def _build_args(self, prompt, session_name, continue_id=None):
        hermes_args = [
            "chat",
            "-q",
            prompt,
            "-Q",
            "--source",
            "tool",
        ]
        if continue_id is not None:
            hermes_args.extend(("--continue", continue_id))

        shell_index = self._bash_lc_index()
        if shell_index is None:
            return [*self.command, *hermes_args]

        shell_prefix = list(self.command[: shell_index + 2])
        base_command = list(self.command[shell_index + 2 :]) or ["hermes"]
        shell_command = " ".join(shlex.quote(part) for part in [*base_command, *hermes_args])
        return [*shell_prefix, shell_command]

    def _parse_reply(self, result, session_name):
        # Hermes outputs session_id on stderr in -Q mode
        session_id = None
        for source in (getattr(result, 'stderr', None) or "", result.stdout or ""):
            for line in source.split("\n"):
                if line.startswith("session_id: "):
                    session_id = line[len("session_id: "):].strip()
                    break
            if session_id is not None:
                break

        if session_id is not None and self._session_store is not None:
            self._session_store.set(session_name, session_id)

        return result.stdout.strip()

    def _bash_lc_index(self) -> int | None:
        for index in range(len(self.command) - 1):
            if self.command[index].lower() in {"bash", "bash.exe"} and self.command[index + 1] == "-lc":
                return index
        return None


class HermesSidecar:
    def __init__(
        self,
        config: SidecarConfig,
        bridge: BridgeLike | None = None,
        hermes: HermesLike | None = None,
    ) -> None:
        self.config = config
        self.bridge = bridge if bridge is not None else BridgeClient(config.bridge_url, timeout=30.0)
        if hermes is not None:
            self.hermes = hermes
        else:
            store = None
            if config.session_file:
                store = SessionStore(config.session_file)
            self.hermes = HermesRunner(
                config.hermes_command,
                timeout=config.hermes_timeout,
                session_store=store,
            )

    def check_health(self) -> dict[str, Any]:
        payload = self.bridge.health()
        if payload.get("status") != "ok":
            raise RuntimeError(f"bridge is not healthy: {payload}")
        return payload

    def process_event(self, event: dict[str, Any]) -> str | None:
        chat_name = _normalize_text(event.get("chat_name")).strip()
        if not chat_name:
            return None

        prompt = format_prompt(event)
        if self.config.debug:
            print(f"[debug] === Hermes prompt for [{chat_name}] ===")
            print(prompt)
            print(f"[debug] === end of prompt ===")
        reply = self.hermes.ask(prompt, session_name=session_name_for_chat(chat_name)).strip()
        if self.config.debug:
            print(f"[debug] Hermes reply for [{chat_name}]: {reply}")
        if not reply:
            if self.config.debug:
                print(f"[debug] empty reply for [{chat_name}], skipping send")
            return None

        if self.config.debug:
            store = getattr(self.hermes, '_session_store', None)
            if store is not None:
                sid = store.get(session_name_for_chat(chat_name))
                if sid:
                    print(f"[debug] session_id for [{chat_name}]: {sid}")
                else:
                    print(f"[debug] no stored session_id for [{chat_name}]")

        if self.config.dry_run:
            print(f"[dry-run] {chat_name}: {reply}")
            return reply

        self.bridge.send(chat_name, reply)
        return reply

    def run(self) -> None:
        while True:
            try:
                payload = self.bridge.poll_events(
                    timeout=self.config.poll_timeout,
                    limit=self.config.poll_limit,
                )
                events = payload.get("events") or []
                if self.config.debug and events:
                    print(f"[debug] polled {len(events)} event(s)")
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    self.process_event(event)
            except (urlerror.URLError, TimeoutError, BridgeRequestError, subprocess.SubprocessError) as exc:
                if isinstance(exc, subprocess.CalledProcessError):
                    cmd = getattr(exc, "cmd", None) or []
                    print(f"[my-wxauto hermes-sidecar] hermes exited with code {exc.returncode}")
                    if exc.stderr:
                        print(f"[my-wxauto hermes-sidecar] stderr: {exc.stderr.strip()}")
                    if exc.stdout:
                        print(f"[my-wxauto hermes-sidecar] stdout: {exc.stdout.strip()}")
                else:
                    print(f"[my-wxauto hermes-sidecar] error: {exc}")
                time.sleep(self.config.retry_delay)

            if self.config.once:
                return


def format_prompt(event: dict[str, Any]) -> str:
    chat_name = _normalize_text(event.get("chat_name"))
    lines = [
        "你正在作为微信机器人回复一个会话。",
        f"会话名：{chat_name}",
        "",
        "本次收到的新消息：",
    ]

    for message in event.get("messages", ()):
        if not isinstance(message, dict):
            continue

        content_value = message.get("content")
        if content_value is None:
            continue

        content = str(content_value).strip()
        if not content:
            continue

        sender = _normalize_text(message.get("sender")).strip()
        if not sender:
            sender = "我" if message.get("is_self") is True else "对方"

        time_text = _normalize_text(message.get("time_text")).strip()
        if time_text:
            lines.append(f"- {time_text} {sender}: {content}")
        else:
            lines.append(f"- {sender}: {content}")

    lines.append("")
    lines.append("请只输出要发送到微信的回复文本。不要解释，不要包含前后缀。")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765")
    parser.add_argument("--poll-timeout", type=float, default=30.0)
    parser.add_argument("--poll-limit", type=int, default=5)
    parser.add_argument("--hermes-command", default="wsl.exe bash -lc hermes")
    parser.add_argument("--hermes-timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--session-file", default=os.path.expanduser("~/.wxauto/hermes_sessions.json"), help="JSON file for persisting Hermes session IDs (empty to disable)")
    return parser


def _split_command(value: str) -> tuple[str, ...]:
    return tuple(shlex.split(value, posix=True))


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
        debug=args.debug,
        session_file=args.session_file,
    )
    sidecar = HermesSidecar(config)
    sidecar.check_health()
    sidecar.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
