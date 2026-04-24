# apple-messages-mcp

An [MCP](https://modelcontextprotocol.io/) server that exposes Apple Messages (iMessage/SMS) to Claude or any MCP-compatible client.

**macOS only** — uses AppleScript/JXA and the Messages SQLite database.

> This is the **`api-backend`** branch. It can optionally delegate database reads and message sending to a running [`imessage-api`](https://github.com/xbenng/imessage-api) REST server (see [Optional: REST backend](#optional-rest-backend)). If `IMESSAGE_API_URL` / `IMESSAGE_API_PASSWORD` are not set, it behaves identically to `main` — direct SQLite + JXA.

## Features

| Tool | Description | Requires DB? |
|------|-------------|:---:|
| `list_chats` | List recent conversations with participant names | No |
| `get_chat_participants` | Get participants of a specific chat | No |
| `send_message` | Send an iMessage/SMS to a phone, email, or chat | No |
| `get_messages` | Read message history from a chat (by ID or contact) | **Yes** |
| `search_messages` | Full-text search across all messages | **Yes** |
| `get_recent_messages` | Get the N most recent messages across all chats | **Yes** |
| `get_attachments` | List attachments from a chat | **Yes** |
| `check_db_access` | Check whether the Messages DB is accessible | No |

## Requirements

- macOS
- Python 3.10+
- [mcp](https://pypi.org/project/mcp/) (`pip install mcp`)
- Apple Messages app configured with an iMessage account

### Full Disk Access (for message reading)

Tools that read message history query `~/Library/Messages/chat.db` directly, which requires **Full Disk Access** for the host process.

To enable:
1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click '+' and add your terminal (Terminal.app, iTerm2, VS Code, etc.)
3. Restart the terminal

Without Full Disk Access, `list_chats`, `get_chat_participants`, and `send_message` still work — they use AppleScript which only requires Automation access.

## Installation

```bash
git clone <repo-url>
cd apple-messages-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Claude Code (CLI)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "apple-messages": {
      "command": "/path/to/apple-messages-mcp/.venv/bin/python",
      "args": ["/path/to/apple-messages-mcp/server.py"]
    }
  }
}
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-messages": {
      "command": "/path/to/apple-messages-mcp/.venv/bin/python",
      "args": ["/path/to/apple-messages-mcp/server.py"]
    }
  }
}
```

### VS Code (Copilot)

Add to `.vscode/mcp.json` or user settings:

```json
{
  "servers": {
    "apple-messages": {
      "command": "/path/to/apple-messages-mcp/.venv/bin/python",
      "args": ["/path/to/apple-messages-mcp/server.py"]
    }
  }
}
```

## Examples

**List your recent chats:**
> "Show me my recent iMessage conversations"

**Read messages from a contact:**
> "Show me my last 20 messages with John Smith"

**Search all messages:**
> "Search my messages for 'dinner Friday'"

**Send a message:**
> "Send 'On my way!' to +15551234567"

## Optional: REST backend

Instead of reading `chat.db` and invoking JXA locally, the server can delegate to [`imessage-api`](https://github.com/xbenng/imessage-api) — a Hono-based REST server that owns the database and send pipeline. This is useful when the MCP host (e.g. a remote Claude Code session, a container, a different user account) doesn't itself have Full Disk Access or can't reach the Messages app, but a separate macOS process does.

Set these environment variables before launching the server:

```bash
export IMESSAGE_API_URL="http://<host>:3001"
export IMESSAGE_API_PASSWORD="<the password that matches AUTH_PASSWORD_HASH>"
```

When both are set, the MCP server:
- Authenticates against `POST /auth/login` and caches the JWT
- Routes `list_chats`, `get_messages`, `search_messages`, `get_recent_messages`, `get_attachments`, and `send_message` through the API
- Still falls back to local JXA/SQLite for tools the API doesn't cover

If either var is unset, the REST client is not initialized and everything runs locally.

## Notes

- The `send_message` tool **actually sends messages** — the AI will confirm before sending.
- Message timestamps are in UTC.
- Group chats are identified by their chat ID (e.g. `iMessage;+;chat12345`). Individual chats use the format `iMessage;-;+15551234567`.
- The server opens the Messages database in **read-only** mode. It never modifies your message history.
- Apple Messages must be authorized for automation in **System Settings → Privacy & Security → Automation**.
