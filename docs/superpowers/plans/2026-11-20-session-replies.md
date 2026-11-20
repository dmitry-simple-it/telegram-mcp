# Session reply subscriptions implementation plan

> Execute inline with superpowers:executing-plans; use TDD for each task.

**Goal:** Telegram replies automatically resume their owning Codex/Claude session.
**Architecture:** Independent SQLite event inbox, explicit subscriptions and durable delivery batches; local Codex Unix WebSocket dispatcher and a Claude stdio channel sharing the inbox.
**Tech Stack:** Python, sqlite3, existing MCP SDK, websockets.
**Spec:** docs/superpowers/specs/2026-11-20-session-replies-design.md

## Global constraints
- Preserve feed/harness, one Telethon connection; no client-message sends, global MCP config edits or permission changes.
- Metadata only; target explicit; debounce 6s; default TTL 48h, max 7 days; 0600 DB in owner-only state directory.
- No delivery ACK on transport write; persist before send; don't blindly retry uncertain RPC acceptance.

## Review focus
- Same message under two accounts and two subscriptions must remain independent.
- Restart/timeout between RPC acceptance and local status must not duplicate a continuation.
- Out-of-order IDs and late registration after the response must not lose replies.
- A busy, closed, archived or wrong thread must not be interrupted or silently replaced.
- Claude can silently drop channels: retain pending delivery until explicit ACK.

## Task 1: Durable inbox
Files: telegram_mcp/reply_store.py, tests/test_reply_store.py.
Produces ReplyStore.observe, subscribe, list_subscriptions, cancel, prepare, mark_sent, acknowledge, fail.
- [ ] Write failing tests for filters, ordinary replies, two consumers, reopen, duplicate events, expiry, coalescing, ACK, failures and ambiguous delivery.
- [ ] Implement SQLite transactions, independent subscription cursors and durable batches.
- [ ] Run focused tests, then full suite; commit own files.

## Task 2: Connect incoming events and MCP tools
Files: tools/events.py, tools/replies.py, tools/__init__.py, runner.py; tests/test_reply_integration.py.
Consumes ReplyStore; produces subscribe_reply/list_reply_subscriptions/cancel_reply_subscription/ack_reply_event.
- [ ] Test real incoming handler reaches store even for an unwatched subscribed group; preserve feed and ignored bots/self.
- [ ] Add account-aware observation and subscription tools, start/stop dispatcher with server lifecycle.
- [ ] Test tools through actual MCP registration, run focused and full suite; commit.

## Task 3: Client adapters
Files: reply_codex.py, reply_channel.py, reply_cli.py, pyproject.toml; tests/test_reply_adapters.py.
Consumes durable batches; produces local Codex delivery and Claude channel tools/notifications.
- [ ] Test Unix WebSocket RPC with a local fake server and Claude channel over real stdio: exact target, busy handling, stable delivery IDs, ACK and uncertain-send reconciliation.
- [ ] Implement adapters; add CLI for subscribe/status/cancel/doctor and an interactive Claude launch wrapper.
- [ ] Test no second Telethon process, no shell callback execution, correct channel capability and ACK-only consumption; commit.

## Task 4: Acceptance and delivery
Files: README.md, CLAUDE.md, docs/reply-subscriptions.md.
- [ ] Run all tests/lint; review whole branch and address material findings with regression tests.
- [ ] Install into live checkout preserving pre-existing plist edits, sync dependencies and restart singleton once after preparing rollback.
- [ ] Verify MCP tools, observe controlled incoming event, bind current Codex thread and verify accepted continuation. Confirm Claude stdio end-to-end; report interactive opt-in requirement.
- [ ] Commit/push only feature files; register pending Maria reply if still relevant.
