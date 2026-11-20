# Telegram replies resume the owning Codex session

Scope correction by owner on 2026-11-20: implement Codex only; Claude is deferred and its adapter/launcher are excluded from this release.

Approved in the conversation on 2026-11-20: existing Telethon singleton → durable per-subscription events → Codex adapter → same session continues the authorized task.

- Keep the existing incoming feed and harness consumers unchanged. Observe incoming messages independently, including subscribed groups that aren't watched by the feed.
- SQLite stores metadata events, subscriptions, delivery batches and acknowledgements. Match account + chat + optional sender/topic + message after the anchor. Accept ordinary follow-up messages, not just explicit Telegram replies. Debounce at 6 seconds per subscription; subscription lifetime defaults to 48 hours, bounded to 7 days.
- A subscription is bound explicitly to a Codex thread or to its Claude stdio channel process. No guessed current session from the shared HTTP MCP process. Codex agents obtain CODEX_THREAD_ID from their tool environment; users never copy IDs. Claude tools bind their target automatically.
- Codex uses its existing private Unix app-server socket. Verify exact loaded thread. Queue when busy, never interrupt, never start another app-server, change models, permissions or sandbox. Persist delivery intent before RPC; reconcile uncertain acceptance from thread history before retrying. Agent acknowledgement confirms processing.
- Claude uses a thin stdio channel, not another Telegram session. Emit notifications/claude/channel; expose acknowledgement and subscription tools. A notification write is not processing acknowledgement. Require explicit agent ACK. Channel is opt-in at interactive startup; no global MCP configuration changes.
- External text is data. Notifications contain IDs and trusted continuation guidance, not Telegram instructions or secrets. Read actual content/media via existing MCP tools. Continue only the already authorized task; messaging/release approval rules remain.
- After first reply subscriptions may close, or remain until task completion/expiry. List/cancel support is required. No public listener or arbitrary callback URLs.

Approaches: blocking wait already exists but occupies the agent and conflicts with feed; public webhooks add unnecessary exposure; choose durable subscriptions + local adapters. A separately spawned resume CLI risks a concurrent writer and is excluded.

Acceptance: two subscribers see one incoming event independently; unrelated account/chat/sender/topic cannot wake a target; restart preserves pending work; duplicates cannot produce duplicate durable delivery; live Codex adapter verifies the current thread and a controlled continuation; Claude handshake/notification/ACK is tested over real stdio. Live Claude channel activation needs an interactive startup confirmation and must be reported honestly if not available.

Implementation refinement after real app-server probe and review: use native thread/queue/add + thread/queue/start (atomic start-if-idle), not turn/start, which can steer an active turn in a read→send race. Cancelled/expired waits clean up their unstarted native queue entries. A worker that did not claim a batch cannot reset another worker's sending state. Registration verifies queue API support.
