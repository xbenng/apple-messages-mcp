# apple-messages-mcp

An [MCP](https://modelcontextprotocol.io/) server that exposes Apple Messages (iMessage/SMS) to Claude or any MCP-compatible client.

The MCP itself has **no macOS dependency** — it's a thin Python client that talks to a separate [`imessage-api`](https://github.com/xbenng/imessage-api) REST server running on a Mac. So this MCP can run on Linux, in a container, or anywhere Python runs, as long as it can reach the API host.

## Features

| Tool | Description |
|------|-------------|
| `list_chats` | List recent conversations with participant names |
| `get_chat_participants` | Get participants of a specific chat |
| `send_message` | Send an iMessage/SMS to a phone, email, or chat |
| `send_attachment` | Send a file attachment |
| `get_messages` | Read message history from a chat (by ID or contact) |
| `search_messages` | Full-text search across all messages |
| `get_recent_messages` | Get the N most recent messages across all chats |
| `get_attachments` | List attachments from a chat |
| `fetch_attachment` | Download an attachment by id and save it locally |
| `check_db_access` | Check whether the API backend is reachable and authenticated |

## Requirements

- Python 3.10+
- [mcp](https://pypi.org/project/mcp/) (`pip install mcp`)
- A running [`imessage-api`](https://github.com/xbenng/imessage-api) on a Mac, reachable from this host

## Installation

```bash
git clone <repo-url>
cd apple-messages-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Set these env vars when launching the server:

```bash
IMESSAGE_API_URL="http://<mac-host>:3001"
IMESSAGE_API_PASSWORD="<the password that matches the API's AUTH_PASSWORD_HASH>"
```

If either is missing, every tool returns a clear error.

### Claude Code (CLI)

Add to `~/.claude.json` (or `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "apple-messages": {
      "type": "stdio",
      "command": "/path/to/apple-messages-mcp/.venv/bin/python",
      "args": ["/path/to/apple-messages-mcp/server.py"],
      "env": {
        "IMESSAGE_API_URL": "http://ng-macbook-pro.lan:3001",
        "IMESSAGE_API_PASSWORD": "your-password"
      }
    }
  }
}
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` with the same shape.

### VS Code (Copilot)

Add to `.vscode/mcp.json` or user settings with the same shape under `"servers"`.

## Examples

**List your recent chats:**
> "Show me my recent iMessage conversations"

**Read messages from a contact:**
> "Show me my last 20 messages with John Smith"

**Search all messages:**
> "Search my messages for 'dinner Friday'"

**Send a message:**
> "Send 'On my way!' to +15551234567"

## Notes

- `send_message` and `send_attachment` **actually send** — the AI will confirm first.
- Message timestamps are in UTC.
- Group chats use IDs like `any;+;<guid>`; 1:1 chats use `iMessage;-;+15551234567` or `SMS;-;+15551234567`. When sending into an existing thread, prefer the full chat ID so the message lands in the right conversation (and on the right service).
- The MCP only reads — all writes (`/send`) go through the API server, which is the only process touching `Messages.app` or `chat.db`.
