# Ledger — plan: docs/superpowers/plans/2026-11-20-session-replies.md

Baseline: 584 passed, 2 existing warnings (pythonjsonlogger and pydantic-settings).
Ruling: use an isolated clone under /tmp because the live checkout has an unrelated plist modification; native worktree tools target the LibreChat repository, not telegram-mcp.
Read-only probe: Codex 0.171.0 current thread is loaded on the owner-only Unix WebSocket socket. Default `codex app-server proxy` hung, so use an explicit verified socket and no resume subprocess.
Pre-flight: store APIs are consumed by ingestion, tools and both adapters. Claude channel ACK must use the same durable delivery ID as the store and Codex prompt.
