# Ledger — plan: docs/superpowers/plans/2026-11-20-session-replies.md

Baseline: 584 passed, 2 existing warnings (pythonjsonlogger and pydantic-settings).
Ruling: use an isolated clone under /tmp because the live checkout has an unrelated plist modification; native worktree tools target the LibreChat repository, not telegram-mcp.
Read-only probe: Codex 0.171.0 current thread is loaded on the owner-only Unix WebSocket socket. Default `codex app-server proxy` hung, so use an explicit verified socket and no resume subprocess.
Pre-flight: store APIs are consumed by ingestion, tools and both adapters. Claude channel ACK must use the same durable delivery ID as the store and Codex prompt.

Task 1: complete — SQLite filters, independent consumers, durable batches; initial suite 592 passed.
Task 2: complete — real event handler + account/topic matching + MCP registration + startup backfill; suite 604 passed before follow-up tests.
Task 3: complete for Codex — real Unix WebSocket tests; real current thread/socket and queue/list probes. Owner deferred Claude, removed adapter/launcher.
Ruling: native queue/add + queue/start replaces turn/start to close the busy TOCTOU with an atomic server idle check. Missing queue support fails registration rather than silently steering.
Final review: fresh agent /root/reply_bridge_review. P1 duplicate-worker failure fixed by owned-state CAS and regression RED→GREEN. P2 busy TOCTOU fixed by native queue and regression RED→GREEN; cancellation cleanup tested RED→GREEN.
Real Claude test: org policy blocks Channels. No policy changes; owner then explicitly deferred Claude.

Final verification: 613 passed, 2 pre-existing warnings; local lint flags match project rules. No deferred material findings. Claude excluded from this release.
