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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Apple Messages")

# Apple Messages SQLite database path
CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Apple epoch offset: seconds between Unix epoch (1970-01-01) and Apple epoch (2001-01-01)
APPLE_EPOCH_OFFSET = 978307200


# ---------------------------------------------------------------------------
# Helpers — imessage-api REST client
# ---------------------------------------------------------------------------


class ImessageApiClient:
    """HTTP client for the imessage-api REST backend."""

    def __init__(self, base_url: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self._token: str | None = None
        self._chats_cache: list | None = None
        self._chats_cache_time: float = 0

    def _authenticate(self) -> None:
        body = json.dumps({"password": self.password}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        self._token = data["token"]

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> any:
        if self._token is None:
            self._authenticate()

        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        data = json.dumps(body).encode() if body else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        for attempt in range(2):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._authenticate()
                    headers["Authorization"] = f"Bearer {self._token}"
                    continue
                raise

    def _get(self, path: str, params: dict | None = None) -> any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict | None = None) -> any:
        return self._request("POST", path, body=body)

    def send_attachment(self, to: str, file_path: str, text: str = "", service: str = "iMessage") -> dict:
        """Send an attachment via multipart/form-data POST /send."""
        import mimetypes

        if self._token is None:
            self._authenticate()

        boundary = f"----MCP{int(time.time() * 1000)}"
        body_parts = []

        # "to" field
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"to\"\r\n\r\n{to}")
        # "service" field
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"service\"\r\n\r\n{service}")
        # optional "text" field
        if text:
            body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}")

        # file field
        filename = Path(file_path).name
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            file_data = f.read()

        file_header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"attachment\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        )

        # Build the full multipart body as bytes
        payload = b""
        for part in body_parts:
            payload += part.encode() + b"\r\n"
        payload += file_header.encode() + file_data + b"\r\n"
        payload += f"--{boundary}--\r\n".encode()

        url = f"{self.base_url}/send"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }

        for attempt in range(2):
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._authenticate()
                    headers["Authorization"] = f"Bearer {self._token}"
                    continue
                error_body = e.read().decode() if e.fp else ""
                raise RuntimeError(f"HTTP {e.code}: {error_body}") from e

    def get_chats(self, limit: int = 100) -> list:
        """Get chats, cached for 30 seconds."""
        now = time.time()
        if self._chats_cache is not None and (now - self._chats_cache_time) < 30:
            return self._chats_cache[:limit]
        chats = self._get("/chats", {"limit": str(min(limit, 500))})
        self._chats_cache = chats
        self._chats_cache_time = now
        return chats[:limit]

    def health(self) -> dict:
        """Check API health (unauthenticated)."""
        req = urllib.request.Request(f"{self.base_url}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())


_api_client: ImessageApiClient | None = None
_api_checked = False


def _get_api() -> ImessageApiClient | None:
    """Lazily initialize the API client from env vars, or return None."""
    global _api_client, _api_checked
    if _api_checked:
        return _api_client
    _api_checked = True
    url = os.environ.get("IMESSAGE_API_URL", "")
    password = os.environ.get("IMESSAGE_API_PASSWORD", "")
    if url and password:
        _api_client = ImessageApiClient(url, password)
    return _api_client


def _service_from_guid(guid: str) -> str:
    """Extract service type (iMessage/SMS) from a chat GUID."""
    if guid.startswith("iMessage;"):
        return "iMessage"
    elif guid.startswith("SMS;"):
        return "SMS"
    return "unknown"


def _unix_ms_to_iso(ts_ms: int | None) -> str:
    """Convert Unix milliseconds timestamp to ISO-like string (matches _apple_ts_to_iso format)."""
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return ""


def _find_chat_by_contact_api(chats: list, contact: str) -> dict | None:
    """Find a chat in the API chat list by contact name, handle, or GUID.

    Prefers 1:1 chats over group chats so that looking up a person returns
    the direct conversation rather than a group they happen to be in.
    """
    contact_lower = contact.lower().strip()
    normalized = contact_lower.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    best: dict | None = None
    best_is_direct = False

    for chat in chats:
        is_direct = len(chat.get("participants", [])) <= 1

        # Match by GUID or chatIdentifier
        guid = (chat.get("guid") or "").lower()
        chat_id = (chat.get("chatIdentifier") or "").lower()
        matched = normalized in guid or normalized in chat_id

        # Match by displayName
        if not matched:
            display = (chat.get("displayName") or "").lower()
            matched = contact_lower in display

        # Match by participant handleId or displayName
        if not matched:
            for p in chat.get("participants", []):
                h = (p.get("handleId") or "").lower()
                d = (p.get("displayName") or "").lower()
                if normalized in h or contact_lower in d:
                    matched = True
                    break

        if not matched:
            continue

        # Prefer direct (1:1) chats over group chats
        if best is None or (is_direct and not best_is_direct):
            best = chat
            best_is_direct = is_direct
            if best_is_direct:
                break  # Can't do better than a direct match

    return best


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
    Shows up to `limit` chats.

    Args:
        limit: Maximum number of chats to return (default 50).
    """
    api = _get_api()
    if api:
        try:
            chats = api.get_chats(limit)
            if not chats:
                return "No chats found."
            lines = []
            for chat in chats:
                guid = chat.get("guid", "")
                display = chat.get("displayName", "")
                service = chat.get("serviceName") or _service_from_guid(guid)
                parts = chat.get("participants", [])
                if display:
                    part_str = f" — {display}"
                elif parts:
                    part_str = " — " + ", ".join(
                        p.get("displayName") or p.get("handleId", "?") for p in parts
                    )
                else:
                    part_str = ""
                lines.append(f"- `{guid}` [{service}]{part_str}")
            return f"**{len(chats)} chats:**\n\n" + "\n".join(lines)
        except Exception as e:
            return f"API error: {e}"

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
        service = _service_from_guid(chat_id)
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
        lines.append(f"- `{chat_id}` [{service}]{part_str}")

    return f"**{len(data)} chats:**\n\n" + "\n".join(lines)


@mcp.tool()
def get_chat_participants(chat_id: str) -> str:
    """
    Get the participants of a specific iMessage chat.

    Args:
        chat_id: The chat ID (e.g. 'iMessage;-;+15551234567' or 'iMessage;+;chat12345').
    """
    api = _get_api()
    if api:
        try:
            chats = api.get_chats(500)
            chat = None
            for c in chats:
                if c.get("guid") == chat_id:
                    chat = c
                    break
            if not chat:
                return f"Chat not found: {chat_id}"
            display = chat.get("displayName") or chat_id
            lines = [f"**Chat:** {display}\n"]
            for p in chat.get("participants", []):
                name = p.get("displayName") or "Unknown"
                handle = p.get("handleId") or ""
                lines.append(f"- {name} ({handle})")
            return "\n".join(lines)
        except Exception as e:
            return f"API error: {e}"

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
    Read messages from a chat.

    Provide either a chat_id OR a contact name/phone number to look up messages.

    Args:
        chat_id: The chat ID (e.g. 'iMessage;-;+15551234567'). Optional if contact is given.
        contact: A contact name or phone/email to search for. Optional if chat_id is given.
        limit: Maximum number of messages to return (default 50, max 500).
        days_back: Only return messages from the last N days. 0 means no date filter.
    """
    if not chat_id and not contact:
        return "Please provide either a `chat_id` or `contact` to look up messages."

    limit = min(max(1, limit), 500)

    api = _get_api()
    if api:
        return _get_messages_api(api, chat_id, contact, limit, days_back)

    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process (Terminal, VS Code, etc.) needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access and add your terminal."
        )

    db = _get_db()

    try:
        if contact and not chat_id:
            chat_id = _find_chat_by_contact(db, contact)
            if not chat_id:
                return f'No chat found for contact "{contact}".'

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
            cutoff_ns = int(cutoff * 1_000_000_000)
            query += " AND m.date > ?"
            params.append(cutoff_ns)

        query += " ORDER BY m.date DESC LIMIT ?"
        params.append(limit)

        rows = db.execute(query, params).fetchall()

        if not rows:
            return f"No messages found in chat `{chat_id}`."

        chat_name = _get_chat_display_name(db, chat_id)
        service = _service_from_guid(chat_id)

        lines = [f"**Chat:** {chat_name} (`{chat_id}`) [{service}]"]
        lines.append(f"**Reply with:** `send_message(to=\"{chat_id}\", ...)`\n")
        lines.append("**Messages** (newest first):\n")

        for row in reversed(rows):
            text = row["text"] or ""
            is_from_me = row["is_from_me"]
            ts = _apple_ts_to_iso(row["date"])
            handle = row["handle_id"] or ""
            has_attachment = row["cache_has_attachments"]

            assoc_type = row["associated_message_type"]
            if assoc_type and assoc_type != 0:
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


def _get_messages_api(api: ImessageApiClient, chat_id: str, contact: str, limit: int, days_back: int) -> str:
    """Get messages via the REST API."""
    try:
        if contact and not chat_id:
            chats = api.get_chats(500)
            found = _find_chat_by_contact_api(chats, contact)
            if not found:
                return f'No chat found for contact "{contact}".'
            chat_id = found["guid"]

        encoded_id = urllib.parse.quote(chat_id, safe="")
        messages = api._get(f"/chats/{encoded_id}/messages", {"limit": str(limit)})

        if days_back > 0:
            cutoff_ms = (datetime.now(timezone.utc).timestamp() - days_back * 86400) * 1000
            messages = [m for m in messages if (m.get("date") or 0) > cutoff_ms]

        if not messages:
            return f"No messages found in chat `{chat_id}`."

        # Get chat display name
        chat_name = chat_id
        try:
            chats = api.get_chats(500)
            for c in chats:
                if c.get("guid") == chat_id:
                    chat_name = c.get("displayName") or chat_id
                    break
        except Exception:
            pass

        service = _service_from_guid(chat_id)
        lines = [f"**Chat:** {chat_name} (`{chat_id}`) [{service}]"]
        lines.append(f"**Reply with:** `send_message(to=\"{chat_id}\", ...)`\n")
        lines.append("**Messages** (newest first):\n")

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

        for msg in messages:
            text = msg.get("text") or ""
            is_from_me = msg.get("isFromMe", False)
            ts = _unix_ms_to_iso(msg.get("date"))
            sender_name = msg.get("senderName") or ""
            has_attachment = bool(msg.get("attachments"))

            assoc_type = msg.get("associatedMessageType", 0)
            if assoc_type and assoc_type != 0:
                tapback = tapback_map.get(assoc_type, f"Reacted ({assoc_type}) to")
                sender = "You" if is_from_me else sender_name
                lines.append(f"  _[{ts}] {sender} {tapback} a message_")
                continue

            sender = "**You**" if is_from_me else f"**{sender_name}**"
            attachment_note = " 📎" if has_attachment else ""
            if text:
                lines.append(f"[{ts}] {sender}: {text}{attachment_note}")
            elif has_attachment:
                lines.append(f"[{ts}] {sender}: _(attachment)_ 📎")

        return "\n".join(lines)

    except Exception as e:
        return f"API error: {e}"


@mcp.tool()
def search_messages(
    query: str,
    contact: str = "",
    limit: int = 30,
    days_back: int = 0,
) -> str:
    """
    Search message text across all chats or a specific contact.

    Args:
        query: Text to search for (case-insensitive).
        contact: Optional contact name/phone to restrict search to.
        limit: Maximum results to return (default 30, max 200).
        days_back: Only search the last N days. 0 means search all.
    """
    if not query.strip():
        return "Please provide a non-empty search query."

    limit = min(max(1, limit), 200)

    api = _get_api()
    if api:
        return _search_messages_api(api, query, contact, limit, days_back)

    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access."
        )

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

            if len(text) > 200:
                text = text[:200] + "..."
            lines.append(f"- [{ts}] **{sender}** in _{chat_name}_: {text}")

        return "\n".join(lines)

    finally:
        db.close()


def _search_messages_api(api: ImessageApiClient, query: str, contact: str, limit: int, days_back: int) -> str:
    """Search messages via the REST API."""
    try:
        results = api._get("/search", {"q": query, "limit": str(limit)})

        # Filter by contact if specified
        if contact:
            chats = api.get_chats(500)
            found = _find_chat_by_contact_api(chats, contact)
            if not found:
                return f'No chat found for contact "{contact}".'
            target_chat_id = found["id"]  # numeric id, matches searchResult.chatId
            target_guid = found["guid"]
            results = [r for r in results if r.get("chatId") == target_chat_id or r.get("chatGuid") == target_guid]

        # Filter by days_back
        if days_back > 0:
            cutoff_ms = (datetime.now(timezone.utc).timestamp() - days_back * 86400) * 1000
            results = [r for r in results if (r.get("date") or 0) > cutoff_ms]

        if not results:
            return f'No messages found matching "{query}".'

        lines = [f'**Found {len(results)} message(s) matching "{query}":**\n']
        for r in results:
            text = r.get("text") or ""
            is_from_me = r.get("isFromMe", False)
            ts = _unix_ms_to_iso(r.get("date"))
            sender = "You" if is_from_me else (r.get("senderName") or "")
            chat_name = r.get("chatDisplayName") or ""

            if len(text) > 200:
                text = text[:200] + "..."
            lines.append(f"- [{ts}] **{sender}** in _{chat_name}_: {text}")

        return "\n".join(lines)

    except Exception as e:
        return f"API error: {e}"


@mcp.tool()
def send_message(to: str, text: str) -> str:
    """
    Send an iMessage/SMS to a phone number, email, or existing chat.

    IMPORTANT: This will actually send a message. Double-check the recipient and content.

    IMPORTANT: When replying to an existing conversation, ALWAYS use the full chat ID
    (e.g. 'iMessage;-;+15551234567' or 'SMS;-;+15551234567') to ensure the reply goes
    through the same service (iMessage vs SMS) as the original thread. Using a bare phone
    number defaults to iMessage and may create a separate conversation.

    Args:
        to: Chat ID (e.g. 'SMS;-;+15551234567' or 'iMessage;-;+15551234567'), phone number, or email.
             Prefer the full chat ID from list_chats/get_messages to preserve the correct service.
        text: The message text to send.
    """
    if not text.strip():
        return "Cannot send an empty message."

    safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
    safe_to = to.replace("\\", "\\\\").replace('"', '\\"')

    # Determine if sending to an existing chat or to a buddy
    # Chat IDs can start with "iMessage;", "SMS;", or "any;" (group chats)
    if ";" in safe_to and (";" + "+" + ";" in safe_to or ";" + "-" + ";" in safe_to):
        # Sending to an existing chat by chat ID — use AppleScript chat id
        script = f'''
tell application "Messages"
    send "{safe_text}" to chat id "{safe_to}"
end tell
'''
        try:
            _run_applescript(script, timeout=30)
            return f"Message sent to `{to}`: {text}"
        except RuntimeError as e:
            return f"Failed to send message: {e}"
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
def send_attachment(to: str, file_path: str, text: str = "") -> str:
    """
    Send a file attachment via iMessage/SMS.

    IMPORTANT: This will actually send a message with an attachment. Double-check the recipient and file.

    Args:
        to: Chat ID (e.g. 'SMS;-;+15551234567' or 'iMessage;-;+15551234567'), phone number, or email.
             Prefer the full chat ID from list_chats/get_messages to preserve the correct service.
        file_path: Absolute path to the file to send (e.g. '/Users/ben/Desktop/photo.jpg').
        text: Optional text message to send along with the attachment.
    """
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"File not found: {path}"
    if not path.is_file():
        return f"Not a file: {path}"

    posix_path = str(path)

    # Determine service from chat ID for API backend
    service = "iMessage"
    if to.startswith("SMS;"):
        service = "SMS"

    # Extract the recipient handle for the API (e.g. "+15551234567" from "iMessage;-;+15551234567")
    api_to = to
    if ";" in to:
        parts = to.split(";")
        if len(parts) >= 3:
            api_to = parts[-1]  # the handle portion

    # Try API backend first, fall through to AppleScript on failure
    api = _get_api()
    if api:
        try:
            api.send_attachment(to=api_to, file_path=posix_path, text=text, service=service)
            result = f"Attachment sent to `{to}`: {path.name}"
            if text.strip():
                result += f" with message: {text}"
            return result
        except Exception:
            pass  # fall through to AppleScript

    # AppleScript: copy file to Messages Attachments dir first (Messages
    # cannot transfer files from arbitrary paths — transfer_state stays 6).
    attach_dir = Path.home() / "Library" / "Messages" / "Attachments" / "outgoing"
    attach_dir.mkdir(parents=True, exist_ok=True)
    staged = attach_dir / path.name
    if staged.resolve() != path.resolve():
        import shutil
        shutil.copy2(path, staged)
    staged_posix = str(staged)

    safe_to = to.replace("\\", "\\\\").replace('"', '\\"')
    safe_path = staged_posix.replace("\\", "\\\\").replace('"', '\\"')

    is_chat_id = ";" in to and (";+;" in to or ";-;" in to)

    if is_chat_id:
        # Send to existing chat by ID — use alias for reliable transfer
        script = f'''
set theFile to POSIX file "{safe_path}" as alias
tell application "Messages"
    send theFile to chat id "{safe_to}"
end tell
'''
    else:
        # Send to phone/email
        script = f'''
set theFile to POSIX file "{safe_path}" as alias
tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant "{safe_to}" of targetService
    send theFile to targetBuddy
end tell
'''

    try:
        _run_applescript(script, timeout=30)
    except RuntimeError as e:
        error_str = str(e)
        # Fallback: try the other approach
        if not is_chat_id:
            try:
                fallback = f'''
set theFile to POSIX file "{safe_path}" as alias
tell application "Messages"
    send theFile to chat id "iMessage;-;{safe_to}"
end tell
'''
                _run_applescript(fallback, timeout=30)
            except RuntimeError:
                return f"Failed to send attachment: {error_str}"
        else:
            return f"Failed to send attachment: {error_str}"

    # Optionally send accompanying text (AppleScript can't send file+text in one go)
    if text.strip():
        send_message(to, text)

    result = f"Attachment sent to `{to}`: {path.name}"
    if text.strip():
        result += f" with message: {text}"
    return result


@mcp.tool()
def get_recent_messages(limit: int = 30, days_back: int = 1) -> str:
    """
    Get the most recent messages across ALL chats.

    Useful for catching up on recent conversations.

    Args:
        limit: Maximum number of messages (default 30, max 200).
        days_back: Only include messages from the last N days (default 1).
    """
    limit = min(max(1, limit), 200)

    api = _get_api()
    if api:
        return _get_recent_messages_api(api, limit, days_back)

    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access."
        )

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
            chat_guid = row["chat_id"]
            chat_name = row["chat_name"] or chat_guid
            if chat_name != current_chat:
                service = _service_from_guid(chat_guid)
                lines.append(f"\n**--- {chat_name} [{service}] (`{chat_guid}`) ---**")
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


def _get_recent_messages_api(api: ImessageApiClient, limit: int, days_back: int) -> str:
    """Get recent messages across all chats via the REST API."""
    try:
        chats = api.get_chats(50)

        if not chats:
            return "No recent messages found."

        cutoff_ms = (datetime.now(timezone.utc).timestamp() - days_back * 86400) * 1000 if days_back > 0 else 0

        # Filter chats to those with recent activity
        if cutoff_ms > 0:
            chats = [c for c in chats if (c.get("lastMessageDate") or 0) > cutoff_ms]

        if not chats:
            return "No recent messages found."

        # Fetch messages from each active chat
        # Distribute limit across chats, minimum 5 per chat
        per_chat = max(5, limit // len(chats)) if chats else limit
        all_messages = []

        for chat in chats:
            guid = chat.get("guid", "")
            service = chat.get("serviceName") or _service_from_guid(guid)
            display = chat.get("displayName") or guid
            encoded = urllib.parse.quote(guid, safe="")
            try:
                msgs = api._get(f"/chats/{encoded}/messages", {"limit": str(per_chat)})
            except Exception:
                continue
            for msg in msgs:
                msg["_chat_display"] = display
                msg["_chat_service"] = service
                msg["_chat_guid"] = guid
            all_messages.extend(msgs)

        # Filter by date and non-tapbacks
        if cutoff_ms > 0:
            all_messages = [m for m in all_messages if (m.get("date") or 0) > cutoff_ms]
        all_messages = [
            m for m in all_messages
            if m.get("text") and (m.get("associatedMessageType", 0) or 0) == 0
        ]

        # Sort chronologically (newest first), then truncate
        all_messages.sort(key=lambda m: m.get("date") or 0, reverse=True)
        all_messages = all_messages[:limit]

        if not all_messages:
            return "No recent messages found."

        lines = [f"**{len(all_messages)} most recent messages:**\n"]
        current_chat = None
        for msg in reversed(all_messages):
            chat_name = msg.get("_chat_display", "")
            if chat_name != current_chat:
                chat_svc = msg.get("_chat_service", "")
                chat_guid = msg.get("_chat_guid", "")
                lines.append(f"\n**--- {chat_name} [{chat_svc}] (`{chat_guid}`) ---**")
                current_chat = chat_name

            text = msg.get("text") or ""
            is_from_me = msg.get("isFromMe", False)
            ts = _unix_ms_to_iso(msg.get("date"))
            sender = "You" if is_from_me else (msg.get("senderName") or "")

            if len(text) > 300:
                text = text[:300] + "..."
            lines.append(f"[{ts}] **{sender}**: {text}")

        return "\n".join(lines)

    except Exception as e:
        return f"API error: {e}"


@mcp.tool()
def get_attachments(chat_id: str = "", contact: str = "", limit: int = 20) -> str:
    """
    List attachments (images, files, etc.) from a specific chat.

    Args:
        chat_id: The chat ID. Optional if contact is provided.
        contact: Contact name/phone to look up. Optional if chat_id is provided.
        limit: Maximum number of attachments (default 20, max 100).
    """
    if not chat_id and not contact:
        return "Please provide either a `chat_id` or `contact`."

    limit = min(max(1, limit), 100)

    api = _get_api()
    if api:
        return _get_attachments_api(api, chat_id, contact, limit)

    if not _db_available():
        return (
            "ERROR: Cannot access the Messages database.\n"
            "The host process needs **Full Disk Access**.\n"
            "Go to System Settings → Privacy & Security → Full Disk Access."
        )

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
              AND (a.transfer_state = 0 OR a.transfer_state = 5)
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
                expanded = path.replace("~", str(Path.home()))
                lines.append(f"  Path: `{expanded}`")

        return "\n".join(lines)

    finally:
        db.close()


def _get_attachments_api(api: ImessageApiClient, chat_id: str, contact: str, limit: int) -> str:
    """Get attachments via the REST API."""
    try:
        if contact and not chat_id:
            chats = api.get_chats(500)
            found = _find_chat_by_contact_api(chats, contact)
            if not found:
                return f'No chat found for contact "{contact}".'
            chat_id = found["guid"]

        # Fetch enough messages to find attachments
        encoded_id = urllib.parse.quote(chat_id, safe="")
        messages = api._get(f"/chats/{encoded_id}/messages", {"limit": "200"})

        # Get chat display name
        chat_name = chat_id
        try:
            chats = api.get_chats(500)
            for c in chats:
                if c.get("guid") == chat_id:
                    chat_name = c.get("displayName") or chat_id
                    break
        except Exception:
            pass

        # Collect attachments (iterate newest-first so we get the most recent)
        attachment_entries = []
        for msg in reversed(messages):
            attachments = msg.get("attachments", [])
            if not attachments:
                continue
            for att in attachments:
                att_id = att.get("id")
                if att_id is None:
                    continue
                attachment_entries.append({
                    "id": att_id,
                    "filename": att.get("transferName") or att.get("filename") or "unknown",
                    "mime": att.get("mimeType") or "unknown",
                    "size": att.get("totalBytes") or 0,
                    "is_sticker": att.get("isSticker", False),
                    "ts": _unix_ms_to_iso(msg.get("date")),
                    "sender": "You" if msg.get("isFromMe") else (msg.get("senderName") or "?"),
                })
                if len(attachment_entries) >= limit:
                    break
            if len(attachment_entries) >= limit:
                break

        if not attachment_entries:
            return f"No attachments found in chat `{chat_id}`."

        lines = [f"**Attachments in {chat_name}:**\n"]
        for att in attachment_entries:
            size_str = _format_bytes(att["size"]) if att["size"] else "?"
            sticker = " (sticker)" if att["is_sticker"] else ""
            lines.append(f"- [{att['ts']}] **{att['sender']}**: {att['filename']} ({att['mime']}, {size_str}){sticker}")
            lines.append(f"  Download: `GET /attachments/{att['id']}`")

        return "\n".join(lines)

    except Exception as e:
        return f"API error: {e}"


@mcp.tool()
def check_db_access() -> str:
    """
    Check whether message data is accessible (via API or direct database).

    If not, provides instructions for enabling access.
    Tools that read message history require either API or database access.
    """
    api = _get_api()
    if api:
        try:
            health = api.health()
            status = health.get("status", "unknown")
            # Also try an authenticated request to verify credentials
            chats = api.get_chats(1)
            return (
                f"imessage-api is connected.\n"
                f"- API status: {status}\n"
                f"- API URL: {api.base_url}\n"
                f"- Authentication: OK\n"
                f"- Backend: REST API (no local FDA required)"
            )
        except Exception as e:
            return (
                f"imessage-api is configured but not reachable.\n"
                f"- API URL: {api.base_url}\n"
                f"- Error: {e}\n\n"
                f"Make sure the API is running: cd /path/to/imessage-api && npm start"
            )

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
            "Alternatively, set IMESSAGE_API_URL and IMESSAGE_API_PASSWORD env vars\n"
            "to use the imessage-api REST backend (no FDA required for this process).\n\n"
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
