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
MCP_TRANSPORT=sse MCP_PORT=8765 uv run main.py       # SSE transport (HTTP)  [fork]
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
- **SSE** [fork] — `run_sse_async()` on `MCP_HOST:MCP_PORT`. **One long-lived process holds a single
  Telegram connection while multiple local MCP clients (Claude Code via mcp-remote, Claude Desktop)
  attach over HTTP.** This avoids one Telethon session per client, which Telegram throttles/flags.
  In production this runs under launchd (`com.telegram-mcp.server`, `:8765`).

## Fork-specific tools / features

- **`transcribe_voice_message`** (`tools/media.py`) — downloads a voice/audio message to a temp file
  and transcribes locally with `mlx-whisper` (Apple Silicon). Model from `WHISPER_MODEL`.
- **`@username` + `id` in listings** — `get_sender_info()` / `get_sender_username()` thread the sender's
  public `@username` and numeric `sender_id` through `message_to_dict`, `format_message_line`,
  `get_message_context`, search / pinned / date-range, and participant/admin/banned listings.
- **SSE transport** — see Transport modes above.

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
| `MCP_TRANSPORT` | No | `stdio` (default) or `sse` |
| `MCP_HOST` | No | SSE host (default `127.0.0.1`) |
| `MCP_PORT` | No | SSE port (default `8765`) |
| `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK` | No | Allow file tools to fall back to CLI-provided roots when the client declares none |

## Code Style

- Line length: 99 characters (Black configured in pyproject.toml)
- Python 3.10+ required
- Flake8 ignores: E203, E501, W503
- All tools are `async def`; `nest_asyncio` is applied for nested event loops
- Tools include `ToolAnnotations` with `readOnlyHint`/`destructiveHint`/`openWorldHint` flags

## Production deployment (Simple IT)

- Runs under launchd: `~/Library/LaunchAgents/com.telegram-mcp.server.plist`
  (`uv --directory ~/Projects/telegram-mcp run main.py ~/Downloads`, SSE on `:8765`, `KeepAlive=true`).
- Restart: `launchctl kickstart -k gui/$(id -u)/com.telegram-mcp.server`
  (full stop needs `launchctl bootout` because of `KeepAlive`).
- Claude Code connects through an `mcp-remote` proxy to the SSE endpoint; after a server restart,
  restart Claude Code so the proxy reconnects.
