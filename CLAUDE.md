# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram MCP Server — a Model Context Protocol server providing Telegram integration for Claude, Cursor, and other MCP-compatible clients. Built with Telethon (Telegram API) and FastMCP (MCP protocol).

This is the **Simple IT fork** of `chigwell/telegram-mcp`, migrated onto upstream v2 (package architecture). Fork-specific additions are marked **[fork]** below.

## Commands

### Install dependencies
```bash
uv sync
```

### Run the MCP server
```bash
uv run main.py                                       # stdio transport (default)
MCP_TRANSPORT=http MCP_PORT=8765 uv run main.py      # Streamable HTTP transport, endpoint /mcp
MCP_TRANSPORT=sse MCP_PORT=8765 uv run main.py       # legacy SSE transport (HTTP), endpoint /sse  [fork]
uv run main.py ~/Downloads                           # positional args = server-side allowed roots
```

### Generate Telegram session string
```bash
uv run session_string_generator.py
```

### Linting and formatting
```bash
uv run black .              # Format code
uv run black --check .      # Check formatting
uv run flake8 .             # Lint code
```

### Tests
```bash
uv run pytest                                  # full suite in tests/
uv run pytest tests/test_runtime.py            # single file
uv run pytest --cov --cov-report=term-missing  # with coverage
```

### Docker
```bash
docker build -t telegram-mcp:latest .
docker compose up --build
```

## Architecture

The server is a Python package, `telegram_mcp/`. `main.py` at the repo root is a thin
compatibility entrypoint that re-exports from the package and calls `runner.main()`.

```
main.py                      # thin entrypoint (assert_safe_distribution → runner.main)
telegram_mcp/
├── runtime.py               # config, FastMCP instance, clients, helpers, decorators, error handling
├── runner.py                # _main()/main(): connect clients, warm caches, run transport  [SSE branch: fork]
├── client_identity.py       # configurable device identity for the Telethon client
├── install_guard.py         # PyPI name-collision guard (allows source checkouts)
└── tools/                   # 117 @mcp.tool functions, grouped by domain
    ├── accounts.py  chats.py  contacts.py  events.py  folders.py
    ├── groups.py    media.py  messages.py  profile.py
    └── __init__.py          # imports all submodules → decorators register the tools
```

### runtime.py (the core)

- **Config** — env vars (`TELEGRAM_API_ID/HASH`, `WHISPER_MODEL` [fork]), `load_dotenv()`.
- **`mcp = FastMCP("telegram")`** — the server instance; an annotation hook tags all results `audience=["user"]`.
- **Multi-account** — `_discover_accounts()` builds `clients: dict[label → TelegramClient]` from
  `TELEGRAM_SESSION_STRING[_<LABEL>]` / `TELEGRAM_SESSION_NAME[_<LABEL>]`. Unsuffixed → label `"default"`.
  `get_client(account)` and `@with_account(readonly=...)` give tools single- or multi-account behavior.
  StringSession takes priority over a file session when both are set.
- **Connection** — `ensure_connected()` / `_force_reconnect()` verify a live connection and reconnect on failure.
- **Timestamps [fork]** — every datetime leaving the server goes through `telegram_mcp/timefmt.py`
  (`to_local_iso()`), wired into both JSON serializers (`runtime.json_serializer`,
  `sanitize._json_default`) and the few explicit `.isoformat()` call sites. Telegram returns UTC;
  results carry the machine's local time with the offset kept in the string
  (`2026-09-04T15:07:35+03:00`), so a model reading them cannot mistake UTC for wall-clock time.
  `TELEGRAM_MCP_TZ` pins a zone explicitly. Never emit a raw Telethon datetime — use `to_local_iso()`.
- **Helpers** — `format_message()`, `get_sender_name()`, **`get_sender_info()` / `get_sender_username()` [fork]**
  (expose `@username` + numeric id in listings), `get_engagement_*`, `resolve_entity()`, `sanitize_*` (from `sanitize.py`).
- **File security** — allowed-roots model (`_resolve_readable_file_path` / `_resolve_writable_file_path`,
  `_ensure_allowed_roots`). Roots come from the MCP client; server-side CLI roots are an opt-in fallback
  (`TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=true` + positional CLI args). This replaces the old cowork path hack.

### Tool registration pattern

```python
@mcp.tool(annotations=ToolAnnotations(title="...", readOnlyHint=True, destructiveHint=False))
@with_account(readonly=True)
@validate_id("chat_id")
async def tool_name(chat_id: Union[int, str], ..., account: str = None) -> str:
    """Docstring with Args section."""
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        # ... logic ...
        return result_string_or_json
    except Exception as e:
        return log_and_format_error("tool_name", e, chat_id=chat_id)
```

### Error handling

`log_and_format_error()` logs full tracebacks and returns user-facing codes like `CHAT-ERR-042`
(category prefix + hash). Categories live in the `ErrorCategory` enum (`CHAT`, `MSG`, `CONTACT`,
`GROUP`, `MEDIA`, `PROFILE`, `AUTH`, `ADMIN`).

### @validate_id() decorator

Validates ID params before the tool runs. Accepts integer, numeric string, or username
(`@name`/`name`). Multiple params: `@validate_id("chat_id", "user_id")`.

### Transport modes

- **stdio** (default) — each MCP client spawns its own process → its own Telegram session.
- **Streamable HTTP** (`MCP_TRANSPORT=http`, endpoint `/mcp`) — **production default.**
  `run_streamable_http_async()` on `MCP_HOST:MCP_PORT`, `FastMCP(..., stateless_http=True)`. One
  long-lived process holds a single Telegram connection while multiple local MCP clients (Claude
  Code natively, Claude Desktop via `mcp-remote`) attach over HTTP — avoids one Telethon session per
  client, which Telegram throttles/flags. `stateless_http=True` means a client's next call after a
  server restart just works instead of failing with "No valid session ID provided" — this is what
  fixed the SSE transport's long-standing "clients need a manual `/mcp` reconnect after every daemon
  restart" pain point (SSE keeps per-connection session state; streamable HTTP with
  `stateless_http=True` doesn't). In production this runs under launchd (`com.telegram-mcp.server`,
  `:8765`).
- **SSE** [fork] (`MCP_TRANSPORT=sse`, endpoint `/sse`) — `run_sse_async()`, kept only for clients
  that can't speak streamable HTTP. Same single-shared-connection design as above, but each client
  reconnect after a server restart needs a fresh `session_id` (client-side reconnect), which is why
  it was replaced as the default.

## Fork-specific tools / features

- **`transcribe_voice_message`** (`tools/media.py`) — downloads a voice/audio message to a temp file
  and transcribes locally with `mlx-whisper` (Apple Silicon). Model from `WHISPER_MODEL`.
- **`@username` + `id` in listings** — `get_sender_info()` / `get_sender_username()` thread the sender's
  public `@username` and numeric `sender_id` through `message_to_dict`, `format_message_line`,
  `get_message_context`, search / pinned / date-range, and participant/admin/banned listings.
- **SSE transport** (legacy fallback) — see Transport modes above; production default is Streamable HTTP.

## Configuration

Environment variables (via `.env`):

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_API_ID` | Yes | API ID from my.telegram.org/apps |
| `TELEGRAM_API_HASH` | Yes | API hash from my.telegram.org/apps |
| `TELEGRAM_SESSION_STRING` | One of two | Session string (preferred, avoids DB lock issues) |
| `TELEGRAM_SESSION_NAME` | One of two | File-based session name (alternative) |
| `TELEGRAM_SESSION_STRING_<LABEL>` | No | Per-account session for multi-account mode |
| `WHISPER_MODEL` | No | mlx-whisper model for transcription (default `mlx-community/whisper-small-mlx`) [fork] |
| `MCP_TRANSPORT` | No | `stdio` (default), `http` (production, Streamable HTTP, endpoint `/mcp`), or `sse` (legacy, endpoint `/sse`) |
| `MCP_HOST` | No | HTTP/SSE host (default `127.0.0.1`) |
| `MCP_PORT` | No | HTTP/SSE port (default `8765`) |
| `TELEGRAM_MCP_TZ` | No | IANA timezone for timestamps in results (default: the machine's local zone) [fork] |
| `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK` | No | Allow file tools to fall back to CLI-provided roots when the client declares none |

## Code Style

- Line length: 99 characters (Black configured in pyproject.toml)
- Python 3.10+ required
- Flake8 ignores: E203, E501, W503
- All tools are `async def`; `nest_asyncio` is applied for nested event loops
- Tools include `ToolAnnotations` with `readOnlyHint`/`destructiveHint`/`openWorldHint` flags

## Production deployment (Simple IT)

- Runs under launchd: `~/Library/LaunchAgents/com.telegram-mcp.server.plist`
  (`uv --directory ~/Projects/telegram-mcp run main.py ~/Downloads`, Streamable HTTP on `:8765`
  (`MCP_TRANSPORT=http`), `KeepAlive=true`). Managed via `./telegram-mcp.sh
  install|uninstall|start|stop|restart|status|logs|health`.
- Restart: `launchctl kickstart -k gui/$(id -u)/com.telegram-mcp.server` or `./telegram-mcp.sh
  restart` (full stop needs `launchctl bootout` because of `KeepAlive`).
- Claude Code connects natively (`"type": "http"`, `url` ending in `/mcp`, in `~/.claude.json`);
  Claude Desktop connects through an `mcp-remote` proxy pointed at the same `/mcp` URL. Because the
  server is `stateless_http=True`, clients survive a server restart without reconnecting — this
  replaced the legacy SSE transport specifically to remove the "reconnect every client after every
  daemon restart" step.
