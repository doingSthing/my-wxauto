# WeChat Bridge Reliability Design

Date: 2026-05-09

## Goal

Build the reliability core for `my-wxauto` as a Windows-local WeChat bridge. The bridge should provide stable message capture and delivery primitives that can later be connected non-invasively to Hermes Agent or OpenClaw.

This design focuses on two problems:

1. Avoid duplicate robot processing for the same WeChat message.
2. Avoid missing messages while the robot or model is thinking.

It does not implement Hermes or OpenClaw integration yet.

References:

- Hermes Agent: https://github.com/NousResearch/hermes-agent
- OpenClaw: https://github.com/openclaw/openclaw

## Current Context

The project already has these core abilities:

- Open a WeChat conversation.
- Send a text message to a conversation.
- Detect new messages from taskbar/tray flashing.
- Open unread conversations and read visible messages.
- Read visible message sender information when possible.
- Mark whether a visible message appears to be sent by the current user.

The current listener implementation is still a direct callback flow. It should become a small local message pipeline before being connected to real robot frameworks.

## Key Decisions

### One Conversation Per Event

The bridge must not send several unrelated WeChat conversations to Hermes or OpenClaw as one mixed request.

Instead:

- Each opened conversation produces its own event.
- Each event contains one conversation's newly observed message batch.
- `max_chats_per_drain` controls how many conversations the WeChat UI reader opens in one drain cycle. It is not a model-side batch size.

Default:

```text
max_chats_per_drain = 5
```

### Read A Conversation, Then Emit Immediately

When a drain cycle sees multiple unread conversations, each conversation should be emitted as soon as it is read.

The bridge must not wait until all unread conversations are read before passing anything to the downstream robot layer.

Flow:

```text
detect unread signal
  -> scan visible unread conversations
  -> open unread conversation 1
  -> read visible messages
  -> emit conversation event immediately
  -> open unread conversation 2
  -> read visible messages
  -> emit conversation event immediately
  -> stop after N conversations or time budget
  -> rescan later
```

### Same Rules For Private Chats And Group Chats

The first version should not distinguish private chats and group chats.

All chats use the same batching and delivery rules:

- @ messages trigger processing.
- Non-@ messages also trigger processing.
- No group-specific reply filter is applied in the first version.

### Frozen Batch Is Not Mutated

The first version should avoid starvation in busy chats.

An open batch may continue collecting new messages until one of the batch cut conditions is met. Once a batch is frozen and submitted to downstream processing, later messages for the same conversation go into the next batch instead of mutating the frozen batch.

This means:

- Before freeze: messages are merged.
- After freeze: the batch is stable.
- Downstream responses are tied to the frozen batch they processed.

This prioritizes message capture reliability and forward progress over always answering the absolute latest message.

## Message Deduplication

WeChat UI Automation does not expose a stable message ID for the current target WeChat version. The bridge should generate a soft message key.

Recommended key fields:

```text
chat_name
sender
is_self
message_type
content
time_text
occurrence_index_in_snapshot
```

The resulting key should be hashed and stored in local state.

The key is intentionally best-effort. It should be stable enough to prevent duplicate processing when:

- The same unread conversation is scanned twice.
- WeChat flashes repeatedly for the same unread message.
- The bridge restarts and sees recently visible messages again.

The key may be imperfect for repeated identical messages in the same visible region. `occurrence_index_in_snapshot` reduces that risk.

## Local State

Use a small local SQLite database for durable state.

Initial tables:

```text
seen_messages
  message_key text primary key
  chat_name text not null
  first_seen_at real not null
  last_seen_at real not null
  payload_json text not null

conversation_batches
  batch_id text primary key
  chat_name text not null
  status text not null
  created_at real not null
  frozen_at real
  submitted_at real
  completed_at real
  message_count integer not null
  payload_json text not null

outgoing_echoes
  echo_key text primary key
  chat_name text not null
  content text not null
  sent_at real not null
  expires_at real not null
```

`seen_messages` prevents duplicate inbound processing.

`conversation_batches` gives observability and replay/debug capability.

`outgoing_echoes` helps avoid treating the robot's own sent messages as fresh inbound user messages.

## Batching Rules

Each conversation has at most one open batch.

A batch is frozen when any condition is met:

```text
quiet_window_seconds = 1.5
max_batch_wait_seconds = 8.0
max_batch_messages = 10
```

Meaning:

- If no new message arrives for 1.5 seconds, freeze the batch.
- If messages keep arriving, freeze after 8 seconds from the first message.
- If 10 messages arrive quickly, freeze immediately.

After freeze:

- The batch is submitted to downstream robot processing.
- New messages for the same chat start a new open batch.

## Drain Loop

The listener should use a drain loop rather than a single probe callback.

Recommended default limits:

```text
max_chats_per_drain = 5
max_ui_busy_seconds = 15.0
rescan_after_each_drain = true
```

Behavior:

- Detect new-message wakeup from taskbar/tray flashing.
- Restore WeChat.
- Scan the visible session list.
- Select up to `max_chats_per_drain` unread sessions.
- Open and read each selected session one by one.
- Emit each conversation's new messages immediately after reading that conversation.
- Stop the drain cycle once the chat count or UI time budget is reached.
- Rescan in a later cycle if more unread messages remain.

The first version only guarantees visible unread sessions. Scrolling through a very large unread list can be added later.

## Concurrency Model

All direct WeChat UI operations must be serialized.

Use a single UI operation queue or global UI lock for:

- Restoring WeChat.
- Scanning sessions.
- Opening conversations.
- Reading messages.
- Sending messages.

Model or robot processing must not hold the UI lock.

Threads or tasks:

```text
wakeup listener
  detects flashing and schedules drains

ui worker
  owns WeChat UI operations

batcher
  deduplicates messages and freezes conversation batches

robot dispatcher
  submits frozen batches to Hermes/OpenClaw/shim later

send worker
  serializes outgoing WeChat sends through the same UI queue/lock
```

## Downstream Integration Shape

The bridge should expose normalized conversation batches, not Hermes-specific payloads.

Recommended event shape:

```json
{
  "event_id": "wechat-event-...",
  "batch_id": "wechat-batch-...",
  "platform": "wechat_desktop",
  "chat_id": "wechat:alice",
  "chat_name": "alice",
  "messages": [
    {
      "message_key": "...",
      "sender": "alice",
      "is_self": false,
      "message_type": "text",
      "content": "hello",
      "time_text": "15:41"
    }
  ]
}
```

Later Hermes integration should send each conversation batch as a separate Hermes event/session. Multiple WeChat conversations should not be combined into a single Hermes prompt.

## Reply Loop Prevention

The bridge should avoid triggering the robot from its own outgoing messages.

Rules:

- Ignore messages where `is_self` is clearly true.
- After sending a robot reply, write an `outgoing_echoes` record with a short TTL.
- If a later visible message matches a recent outgoing echo, suppress it from inbound processing.

The echo cache is a safeguard because `is_self` detection is best-effort.

## Error Handling

The first version should favor continuing the listener over perfect recovery.

Recommended behavior:

- If one conversation fails to open, record the error and continue the drain cycle.
- If message reading fails for one conversation, record the error and continue.
- If WeChat restore fails, back off and retry on the next wakeup.
- If UI operation time exceeds `max_ui_busy_seconds`, stop the current drain and rescan later.
- If SQLite write fails, do not submit the affected event downstream because dedup safety is unknown.

All failures should be logged with enough context:

- chat name
- operation
- duration
- exception type/message
- current drain id

## Testing Strategy

Unit tests should cover:

- Soft message key generation.
- Dedup across repeated snapshots.
- Batch freeze by quiet window.
- Batch freeze by maximum wait.
- Batch freeze by maximum message count.
- `max_chats_per_drain` limiting.
- Immediate per-conversation emission during a drain.
- Outgoing echo suppression.
- UI lock serialization through fake workers.

Integration probes should remain manual-friendly because WeChat UI behavior is environment-dependent.

## Non-Goals For First Version

These are intentionally out of scope:

- Scrolling the full session list to find every unread conversation.
- Distinguishing group/private chat behavior.
- Hermes source-code adapter implementation.
- OpenClaw connector implementation.
- Reading historical messages that are not currently rendered by WeChat.
- Perfect message IDs from WeChat internals.

## Open Follow-Up

After this reliability core is implemented, the next design should decide the bridge interface:

- local HTTP API
- SSE or WebSocket event stream
- webhook delivery mode
- optional MCP tool server

The recommended path remains a Windows-local bridge with a thin non-invasive Hermes/OpenClaw shim.
