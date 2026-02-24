#!/usr/bin/env python3
"""
apple_messages_mcp.py

MCP server that exposes Apple Messages (iMessage) to Claude.

Supports two data backends:
  1. JXA/AppleScript — list chats, participants, send messages (always available)
  2. SQLite (~/Library/Messages/chat.db) — read message history
     Requires Full Disk Access for the host process (Terminal, VS Code, etc.)

Platform: macOS only

Usage:
    python3 server.py

Claude Desktop / Claude Code config:
    {
      "mcpServers": {
        "apple-messages": {
          "command": "/path/to/apple-messages-mcp/.venv/bin/python",
          "args": ["/path/to/apple-messages-mcp/server.py"]
        }
      }
    }
"""

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Apple Messages")

# Apple Messages SQLite database path
CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Apple epoch offset: seconds between Unix epoch (1970-01-01) and Apple epoch (2001-01-01)
APPLE_EPOCH_OFFSET = 978307200

# ---------------------------------------------------------------------------
# Helpers — osascript
# ---------------------------------------------------------------------------


def _run_applescript(script: str, timeout: int = 30) -> str:
    """Run an AppleScript and return stdout."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript error")
    return result.stdout.strip()


def _run_jxa(script: str, timeout: int = 30) -> str:
    """Run a JavaScript for Automation (JXA) script via osascript."""
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "JXA error")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Helpers — SQLite
# ---------------------------------------------------------------------------


def _db_available() -> bool:
    """Check if the Messages SQLite database is accessible."""
    if not CHAT_DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB_PATH}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        conn.close()
        return True
    except Exception:
        return False


def _get_db() -> sqlite3.Connection:
    """Open a read-only connection to the Messages database."""
    conn = sqlite3.connect(f"file:{CHAT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _apple_ts_to_iso(ts: int | None) -> str:
    """Convert Apple Core Data timestamp (nanoseconds since 2001-01-01) to ISO string."""
    if not ts:
        return ""
    # Modern macOS stores timestamps in nanoseconds
    if ts > 1e15:
        ts = ts / 1_000_000_000
    elif ts > 1e12:
        ts = ts / 1_000_000
    unix_ts = ts + APPLE_EPOCH_OFFSET
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return ""


def _apple_ts_to_datetime(ts: int | None) -> datetime | None:
    """Convert Apple Core Data timestamp to a datetime object."""
    if not ts:
        return None
    if ts > 1e15:
        ts = ts / 1_000_000_000
    elif ts > 1e12:
        ts = ts / 1_000_000
    unix_ts = ts + APPLE_EPOCH_OFFSET
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_chats(limit: int = 50) -> str:
    """
    List recent iMessage conversations (chats).

    Returns chat ID, display name, and participant names/handles.
    Uses the Messages app via JXA. Shows up to `limit` chats.

    Args:
        limit: Maximum number of chats to return (default 50).
    """
    script = f"""
const app = Application("Messages");
const chats = app.chats();
const result = [];
const lim = Math.min({limit}, chats.length);
for (var i = 0; i < lim; i++) {{
    try {{
        var c = chats[i];
        var p = c.properties();
        var participants = [];
        try {{
            var parts = c.participants();
            for (var j = 0; j < parts.length; j++) {{
                var pp = parts[j].properties();
                participants.push({{name: pp.fullName || pp.name || "", handle: pp.handle || ""}});
            }}
        }} catch(e) {{}}
        result.push({{
            id: p.id || "",
            name: p.name || "",
            participants: participants
        }});
    }} catch(e) {{
        // skip
    }}
}}
JSON.stringify(result);
"""
    data = json.loads(_run_jxa(script, timeout=60))

    if not data:
        return "No chats found."

    lines = []
    for chat in data:
        chat_id = chat["id"]
        name = chat.get("name") or ""
        parts = chat.get("participants", [])
        if name:
            part_str = f" — {name}"
        elif parts:
            part_str = " — " + ", ".join(
                p.get("name") or p.get("handle", "?") for p in parts
            )
        else:
            part_str = ""
        lines.append(f"- `{chat_id}`{part_str}")

    return f"**{len(data)} chats:**\n\n" + "\n".join(lines)


@mcp.tool()
def get_chat_participants(chat_id: str) -> str:
    """
    Get the participants of a specific iMessage chat.

    Args:
        chat_id: The chat ID (e.g. 'iMessage;-;+15551234567' or 'iMessage;+;chat12345').
    """
    safe_id = chat_id.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
const app = Application("Messages");
var chats = app.chats.whose({{id: "{safe_id}"}})();
if (chats.length === 0) {{
    JSON.stringify({{error: "Chat not found: {safe_id}"}});
}} else {{
    var c = chats[0];
    var participants = [];
    try {{
        var parts = c.participants();
        for (var j = 0; j < parts.length; j++) {{
            var pp = parts[j].properties();
            participants.push({{
                name: pp.fullName || pp.name || "",
                handle: pp.handle || "",
                firstName: pp.firstName || "",
                lastName: pp.lastName || ""
            }});
        }}
    }} catch(e) {{}}
    JSON.stringify({{
        id: c.properties().id,
        name: c.properties().name || "",
        participants: participants
    }});
}}
"""
    data = json.loads(_run_jxa(script))
    if "error" in data:
        return data["error"]

    lines = [f"**Chat:** {data.get('name') or data['id']}\n"]
    for p in data.get("participants", []):
        name = p.get("name") or "Unknown"
        handle = p.get("handle") or ""
        lines.append(f"- {name} ({handle})")

    return "\n".join(lines)


@mcp.tool()
def get_messages(
    chat_id: str = "",
    contact: str = "",
    limit: int = 50,
    days_back: int = 0,
) -> str:
    """
    Read messages from the Messages database (requires Full Disk Access).

    Provide either a chat_id OR a contact name/phone number to look up messages.

    Args:
        chat_id: The chat ID (e.g. 'iMessage;-;+15551234567'). Optional if contact is given.
        contact: A contact name or phone/email to search for. Optional if chat_id is given.
        limit: Maximum number of messages to return (default 50, max 500).
        days_back: Only return messages from the last N days. 0 means no date filter.
    """
    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process (Terminal, VS Code, etc.) needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access and add your terminal."
        )

    if not chat_id and not contact:
        return "Please provide either a `chat_id` or `contact` to look up messages."

    limit = min(max(1, limit), 500)
    db = _get_db()

    try:
        if contact and not chat_id:
            # Find chat by contact name or handle
            chat_id = _find_chat_by_contact(db, contact)
            if not chat_id:
                return f'No chat found for contact "{contact}".'

        # Query messages
        query = """
            SELECT
                m.ROWID,
                m.text,
                m.is_from_me,
                m.date,
                m.date_delivered,
                m.date_read,
                m.associated_message_type,
                m.associated_message_guid,
                h.id AS handle_id,
                m.cache_has_attachments
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE c.guid = ?
        """
        params: list = [chat_id]

        if days_back > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - APPLE_EPOCH_OFFSET - (days_back * 86400)
            # Convert to nanoseconds
            cutoff_ns = int(cutoff * 1_000_000_000)
            query += " AND m.date > ?"
            params.append(cutoff_ns)

        query += " ORDER BY m.date DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()

        if not rows:
            return f"No messages found in chat `{chat_id}`."

        # Get chat name
        chat_name = _get_chat_display_name(db, chat_id)

        lines = [f"**Chat:** {chat_name} (`{chat_id}`)\n**Messages** (newest first):\n"]

        for row in reversed(rows):  # Show oldest first
            text = row["text"] or ""
            is_from_me = row["is_from_me"]
            ts = _apple_ts_to_iso(row["date"])
            handle = row["handle_id"] or ""
            has_attachment = row["cache_has_attachments"]

            # Skip reactions/tapbacks (associated_message_type != 0)
            assoc_type = row["associated_message_type"]
            if assoc_type and assoc_type != 0:
                # Format tapback
                tapback_map = {
                    2000: "❤️ Loved",
                    2001: "👍 Liked",
                    2002: "😂 Laughed at",
                    2003: "‼️ Emphasized",
                    2004: "❓ Questioned",
                    2005: "👎 Disliked",
                    3000: "Removed ❤️ from",
                    3001: "Removed 👍 from",
                    3002: "Removed 😂 from",
                    3003: "Removed ‼️ from",
                    3004: "Removed ❓ from",
                    3005: "Removed 👎 from",
                }
                tapback = tapback_map.get(assoc_type, f"Reacted ({assoc_type}) to")
                sender = "You" if is_from_me else handle
                lines.append(f"  _[{ts}] {sender} {tapback} a message_")
                continue

            sender = "**You**" if is_from_me else f"**{handle}**"
            attachment_note = " 📎" if has_attachment else ""
            if text:
                lines.append(f"[{ts}] {sender}: {text}{attachment_note}")
            elif has_attachment:
                lines.append(f"[{ts}] {sender}: _(attachment)_ 📎")

        return "\n".join(lines)

    finally:
        db.close()


@mcp.tool()
def search_messages(
    query: str,
    contact: str = "",
    limit: int = 30,
    days_back: int = 0,
) -> str:
    """
    Search message text across all chats or a specific contact (requires Full Disk Access).

    Args:
        query: Text to search for (case-insensitive).
        contact: Optional contact name/phone to restrict search to.
        limit: Maximum results to return (default 30, max 200).
        days_back: Only search the last N days. 0 means search all.
    """
    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access."
        )

    if not query.strip():
        return "Please provide a non-empty search query."

    limit = min(max(1, limit), 200)
    db = _get_db()

    try:
        sql = """
            SELECT
                m.text,
                m.is_from_me,
                m.date,
                h.id AS handle_id,
                c.guid AS chat_id,
                c.display_name AS chat_name
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text LIKE ?
        """
        params: list = [f"%{query}%"]

        if contact:
            chat_id = _find_chat_by_contact(db, contact)
            if chat_id:
                sql += " AND c.guid = ?"
                params.append(chat_id)
            else:
                return f'No chat found for contact "{contact}".'

        if days_back > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - APPLE_EPOCH_OFFSET - (days_back * 86400)
            cutoff_ns = int(cutoff * 1_000_000_000)
            sql += " AND m.date > ?"
            params.append(cutoff_ns)

        sql += " ORDER BY m.date DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(sql, params).fetchall()

        if not rows:
            return f'No messages found matching "{query}".'

        lines = [f'**Found {len(rows)} message(s) matching "{query}":**\n']
        for row in rows:
            text = row["text"] or ""
            is_from_me = row["is_from_me"]
            ts = _apple_ts_to_iso(row["date"])
            handle = row["handle_id"] or ""
            chat_name = row["chat_name"] or row["chat_id"]
            sender = "You" if is_from_me else handle

            # Truncate long messages
            if len(text) > 200:
                text = text[:200] + "..."
            lines.append(f"- [{ts}] **{sender}** in _{chat_name}_: {text}")

        return "\n".join(lines)

    finally:
        db.close()


@mcp.tool()
def send_message(to: str, text: str) -> str:
    """
    Send an iMessage/SMS to a phone number, email, or existing chat.

    IMPORTANT: This will actually send a message. Double-check the recipient and content.

    Args:
        to: Phone number (e.g. '+15551234567'), email, or chat ID.
        text: The message text to send.
    """
    if not text.strip():
        return "Cannot send an empty message."

    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    safe_to = to.replace("\\", "\\\\").replace('"', '\\"')

    # Determine if sending to an existing chat or to a buddy
    if safe_to.startswith("iMessage;") or safe_to.startswith("SMS;"):
        # Sending to an existing chat by chat ID
        script = f'''
tell application "Messages"
    set targetChat to a reference to chat id "{safe_to}"
    send "{safe_text}" to targetChat
end tell
'''
    else:
        # Sending to a phone/email — find the right service
        script = f'''
tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant "{safe_to}" of targetService
    send "{safe_text}" to targetBuddy
end tell
'''

    try:
        _run_applescript(script, timeout=30)
        return f"Message sent to `{to}`: {text}"
    except RuntimeError as e:
        error_str = str(e)
        # Fallback: try sending via the chat route
        if "participant" in error_str.lower() or "buddy" in error_str.lower():
            try:
                fallback_script = f'''
tell application "Messages"
    send "{safe_text}" to chat id "iMessage;-;{safe_to}"
end tell
'''
                _run_applescript(fallback_script, timeout=30)
                return f"Message sent to `{to}`: {text}"
            except RuntimeError:
                pass
        return f"Failed to send message: {error_str}"


@mcp.tool()
def get_recent_messages(limit: int = 30, days_back: int = 1) -> str:
    """
    Get the most recent messages across ALL chats (requires Full Disk Access).

    Useful for catching up on recent conversations.

    Args:
        limit: Maximum number of messages (default 30, max 200).
        days_back: Only include messages from the last N days (default 1).
    """
    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access."
        )

    limit = min(max(1, limit), 200)
    db = _get_db()

    try:
        sql = """
            SELECT
                m.text,
                m.is_from_me,
                m.date,
                m.associated_message_type,
                h.id AS handle_id,
                c.guid AS chat_id,
                c.display_name AS chat_name
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != ''
              AND (m.associated_message_type = 0 OR m.associated_message_type IS NULL)
        """
        params: list = []

        if days_back > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - APPLE_EPOCH_OFFSET - (days_back * 86400)
            cutoff_ns = int(cutoff * 1_000_000_000)
            sql += " AND m.date > ?"
            params.append(cutoff_ns)

        sql += " ORDER BY m.date DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(sql, params).fetchall()

        if not rows:
            return "No recent messages found."

        lines = [f"**{len(rows)} most recent messages:**\n"]
        current_chat = None
        for row in reversed(rows):
            chat_name = row["chat_name"] or row["chat_id"]
            if chat_name != current_chat:
                lines.append(f"\n**--- {chat_name} ---**")
                current_chat = chat_name

            text = row["text"] or ""
            is_from_me = row["is_from_me"]
            ts = _apple_ts_to_iso(row["date"])
            handle = row["handle_id"] or ""
            sender = "You" if is_from_me else handle

            if len(text) > 300:
                text = text[:300] + "..."
            lines.append(f"[{ts}] **{sender}**: {text}")

        return "\n".join(lines)

    finally:
        db.close()


@mcp.tool()
def get_attachments(chat_id: str = "", contact: str = "", limit: int = 20) -> str:
    """
    List attachments (images, files, etc.) from a specific chat (requires Full Disk Access).

    Args:
        chat_id: The chat ID. Optional if contact is provided.
        contact: Contact name/phone to look up. Optional if chat_id is provided.
        limit: Maximum number of attachments (default 20, max 100).
    """
    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access."
        )

    if not chat_id and not contact:
        return "Please provide either a `chat_id` or `contact`."

    limit = min(max(1, limit), 100)
    db = _get_db()

    try:
        if contact and not chat_id:
            chat_id = _find_chat_by_contact(db, contact)
            if not chat_id:
                return f'No chat found for contact "{contact}".'

        sql = """
            SELECT
                a.filename,
                a.mime_type,
                a.total_bytes,
                a.transfer_name,
                a.created_date,
                m.is_from_me,
                m.date,
                h.id AS handle_id
            FROM attachment a
            JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            JOIN message m ON m.ROWID = maj.message_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE c.guid = ?
            ORDER BY m.date DESC
            LIMIT ?
        """
        rows = db.execute(sql, [chat_id, limit]).fetchall()

        if not rows:
            return f"No attachments found in chat `{chat_id}`."

        chat_name = _get_chat_display_name(db, chat_id)
        lines = [f"**Attachments in {chat_name}:**\n"]

        for row in rows:
            filename = row["transfer_name"] or row["filename"] or "unknown"
            mime = row["mime_type"] or "unknown"
            size = row["total_bytes"] or 0
            ts = _apple_ts_to_iso(row["date"])
            sender = "You" if row["is_from_me"] else (row["handle_id"] or "?")
            path = row["filename"] or ""

            size_str = _format_bytes(size) if size else "?"
            lines.append(f"- [{ts}] **{sender}**: {filename} ({mime}, {size_str})")
            if path:
                # Expand ~ in path
                expanded = path.replace("~", str(Path.home()))
                lines.append(f"  Path: `{expanded}`")

        return "\n".join(lines)

    finally:
        db.close()


@mcp.tool()
def check_db_access() -> str:
    """
    Check whether the Messages database is accessible.

    If not, provides instructions for enabling Full Disk Access.
    Tools that read message history require database access.
    """
    if _db_available():
        db = _get_db()
        try:
            count = db.execute("SELECT COUNT(*) FROM message").fetchone()[0]
            chat_count = db.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
            return (
                f"Messages database is accessible.\n"
                f"- {count:,} messages\n"
                f"- {chat_count:,} chats"
            )
        finally:
            db.close()
    else:
        return (
            "Messages database is NOT accessible.\n\n"
            "To enable message reading, grant **Full Disk Access** to the process "
            "running this server:\n\n"
            "1. Open **System Settings → Privacy & Security → Full Disk Access**\n"
            "2. Click the '+' button\n"
            "3. Add your terminal app (Terminal.app, iTerm2, VS Code, etc.)\n"
            "4. Restart the terminal and re-run the server\n\n"
            "Note: `list_chats`, `get_chat_participants`, and `send_message` work "
            "without database access (they use AppleScript)."
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_chat_by_contact(db: sqlite3.Connection, contact: str) -> str | None:
    """Find a chat GUID by contact name or handle (phone/email)."""
    contact_lower = contact.lower().strip()

    # First try matching by handle (phone or email)
    # Normalize phone: remove spaces, dashes, parens
    normalized = contact_lower.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # Try exact handle match in chat guid
    rows = db.execute(
        "SELECT guid FROM chat WHERE guid LIKE ? ORDER BY ROWID DESC",
        [f"%{normalized}%"],
    ).fetchall()
    if rows:
        return rows[0]["guid"]

    # Try matching via handle table
    rows = db.execute(
        """
        SELECT c.guid
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE h.id LIKE ?
        ORDER BY c.ROWID DESC
        """,
        [f"%{normalized}%"],
    ).fetchall()
    if rows:
        return rows[0]["guid"]

    # Try matching by display_name
    rows = db.execute(
        "SELECT guid FROM chat WHERE LOWER(display_name) LIKE ? ORDER BY ROWID DESC",
        [f"%{contact_lower}%"],
    ).fetchall()
    if rows:
        return rows[0]["guid"]

    return None


def _get_chat_display_name(db: sqlite3.Connection, chat_guid: str) -> str:
    """Get a human-readable name for a chat."""
    row = db.execute(
        "SELECT display_name FROM chat WHERE guid = ?", [chat_guid]
    ).fetchone()
    if row and row["display_name"]:
        return row["display_name"]

    # Fallback: get handles from the chat
    rows = db.execute(
        """
        SELECT h.id
        FROM handle h
        JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
        JOIN chat c ON c.ROWID = chj.chat_id
        WHERE c.guid = ?
        """,
        [chat_guid],
    ).fetchall()
    if rows:
        return ", ".join(r["id"] for r in rows)

    return chat_guid


def _format_bytes(size: int) -> str:
    """Format bytes into human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore
    return f"{size:.1f} TB"


if __name__ == "__main__":
    mcp.run()
