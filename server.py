#!/usr/bin/env python3
"""
apple_messages_mcp.py

MCP server that exposes Apple Messages (iMessage) to Claude.

Backend: REST API (`imessage-api`) running on a macOS host. This MCP makes no
direct macOS calls (no AppleScript, no chat.db) so it can run on Linux/any host.

Required env vars:
    IMESSAGE_API_URL       e.g. http://ng-macbook-pro.lan:3001
    IMESSAGE_API_PASSWORD  shared-secret password for /auth/login
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Apple Messages")


# ---------------------------------------------------------------------------
# REST client
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

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
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

    def _get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict | None = None):
        return self._request("POST", path, body=body)

    # --- public API ---

    def health(self) -> dict:
        req = urllib.request.Request(f"{self.base_url}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def get_chats(self, limit: int = 100) -> list:
        """Get chats, cached for 30 seconds."""
        now = time.time()
        if self._chats_cache is not None and (now - self._chats_cache_time) < 30:
            return self._chats_cache[:limit]
        chats = self._get("/chats", {"limit": str(min(limit, 500))})
        self._chats_cache = chats
        self._chats_cache_time = now
        return chats[:limit]

    def get_chat(self, chat_id_or_guid: str) -> dict | None:
        """Get a single chat by numeric id or GUID."""
        encoded = urllib.parse.quote(chat_id_or_guid, safe="")
        try:
            return self._get(f"/chats/{encoded}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def get_messages_by_chat(self, chat_guid: str, limit: int = 50, before: int | None = None) -> list:
        encoded = urllib.parse.quote(chat_guid, safe="")
        params = {"limit": str(limit)}
        if before is not None:
            params["before"] = str(before)
        return self._get(f"/chats/{encoded}/messages", params)

    def search(self, query: str, limit: int = 30) -> list:
        return self._get("/search", {"q": query, "limit": str(limit)})

    def send_text(self, to: str, text: str, service: str = "iMessage") -> dict:
        return self._post("/send", {"to": to, "text": text, "service": service})

    def download_attachment(self, attachment_id: int | str) -> tuple[bytes, str, str]:
        """Download an attachment by id. Returns (data, content_type, filename)."""
        if self._token is None:
            self._authenticate()

        url = f"{self.base_url}/attachments/{urllib.parse.quote(str(attachment_id), safe='')}"
        headers = {"Authorization": f"Bearer {self._token}"}

        for attempt in range(2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    content_type = resp.headers.get("Content-Type", "application/octet-stream")
                    disposition = resp.headers.get("Content-Disposition", "") or ""
                    filename = ""
                    for part in disposition.split(";"):
                        part = part.strip()
                        if part.lower().startswith("filename="):
                            filename = part.split("=", 1)[1].strip().strip('"')
                            break
                    return data, content_type, filename
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._authenticate()
                    headers["Authorization"] = f"Bearer {self._token}"
                    continue
                error_body = e.read().decode() if e.fp else ""
                raise RuntimeError(f"HTTP {e.code}: {error_body}") from e
        raise RuntimeError("download_attachment: unreachable")

    def send_attachment(self, to: str, file_path: str, text: str = "", service: str = "iMessage") -> dict:
        """Send an attachment via multipart/form-data POST /send."""
        import mimetypes

        if self._token is None:
            self._authenticate()

        boundary = f"----MCP{int(time.time() * 1000)}"
        body_parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"to\"\r\n\r\n{to}",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"service\"\r\n\r\n{service}",
        ]
        if text:
            body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}")

        filename = Path(file_path).name
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            file_data = f.read()

        file_header = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"attachment\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        )

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


_api_client: ImessageApiClient | None = None
_api_checked = False


def _get_api() -> ImessageApiClient | None:
    global _api_client, _api_checked
    if _api_checked:
        return _api_client
    _api_checked = True
    url = os.environ.get("IMESSAGE_API_URL", "").strip()
    password = os.environ.get("IMESSAGE_API_PASSWORD", "").strip()
    if url and password:
        _api_client = ImessageApiClient(url, password)
    return _api_client


def _api_or_error() -> tuple[ImessageApiClient | None, str | None]:
    api = _get_api()
    if api is None:
        return None, (
            "ERROR: imessage-api is not configured.\n"
            "Set IMESSAGE_API_URL and IMESSAGE_API_PASSWORD env vars in your MCP config."
        )
    return api, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TAPBACK_MAP = {
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


def _service_from_guid(guid: str) -> str:
    if guid.startswith("iMessage;"):
        return "iMessage"
    if guid.startswith("SMS;"):
        return "SMS"
    if guid.startswith("any;"):
        return "iMessage"
    return "unknown"


def _unix_ms_to_iso(ts_ms: int | None) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return ""


def _find_chat_by_contact(chats: list, contact: str) -> dict | None:
    """Find a chat by contact name, handle, or GUID. Prefers 1:1 over groups."""
    contact_lower = contact.lower().strip()
    normalized = contact_lower.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    best: dict | None = None
    best_is_direct = False

    for chat in chats:
        is_direct = len(chat.get("participants", [])) <= 1

        guid = (chat.get("guid") or "").lower()
        chat_id = (chat.get("chatIdentifier") or "").lower()
        matched = normalized in guid or normalized in chat_id

        if not matched:
            display = (chat.get("displayName") or "").lower()
            matched = contact_lower in display

        if not matched:
            for p in chat.get("participants", []):
                h = (p.get("handleId") or "").lower()
                d = (p.get("displayName") or "").lower()
                if normalized in h or contact_lower in d:
                    matched = True
                    break

        if not matched:
            continue

        if best is None or (is_direct and not best_is_direct):
            best = chat
            best_is_direct = is_direct

    return best


def _format_bytes(size: int) -> str:
    s: float = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(s) < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"


def _chat_display(chat: dict) -> str:
    return chat.get("displayName") or chat.get("chatIdentifier") or chat.get("guid", "")


def _format_message_line(msg: dict) -> str | None:
    """Format a single message dict to a display line. Returns None to skip."""
    text = msg.get("text") or ""
    is_from_me = msg.get("isFromMe", False)
    ts = _unix_ms_to_iso(msg.get("date"))
    sender_name = msg.get("senderName") or ""
    has_attachment = bool(msg.get("attachments"))

    assoc_type = msg.get("associatedMessageType", 0) or 0
    if assoc_type:
        tapback = TAPBACK_MAP.get(assoc_type, f"Reacted ({assoc_type}) to")
        sender = "You" if is_from_me else sender_name
        return f"  _[{ts}] {sender} {tapback} a message_"

    sender = "**You**" if is_from_me else f"**{sender_name}**"
    attachment_note = " 📎" if has_attachment else ""
    if text:
        return f"[{ts}] {sender}: {text}{attachment_note}"
    if has_attachment:
        return f"[{ts}] {sender}: _(attachment)_ 📎"
    return None


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_chats(limit: int = 50) -> str:
    """
    List recent iMessage conversations (chats).

    Returns chat ID, display name, and participant names/handles.

    Args:
        limit: Maximum number of chats to return (default 50).
    """
    api, err = _api_or_error()
    if err:
        return err
    try:
        chats = api.get_chats(limit)
    except Exception as e:
        return f"API error: {e}"

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
            part_str = " — " + ", ".join(p.get("displayName") or p.get("handleId", "?") for p in parts)
        else:
            part_str = ""
        lines.append(f"- `{guid}` [{service}]{part_str}")

    return f"**{len(chats)} chats:**\n\n" + "\n".join(lines)


@mcp.tool()
def get_chat_participants(chat_id: str) -> str:
    """
    Get the participants of a specific iMessage chat.

    Args:
        chat_id: The chat ID (e.g. 'iMessage;-;+15551234567' or 'iMessage;+;chat12345').
    """
    api, err = _api_or_error()
    if err:
        return err
    try:
        chat = api.get_chat(chat_id)
    except Exception as e:
        return f"API error: {e}"

    if not chat:
        return f"Chat not found: {chat_id}"

    lines = [f"**Chat:** {_chat_display(chat)}\n"]
    for p in chat.get("participants", []):
        name = p.get("displayName") or "Unknown"
        handle = p.get("handleId") or ""
        lines.append(f"- {name} ({handle})")
    return "\n".join(lines)


@mcp.tool()
def get_messages(chat_id: str = "", contact: str = "", limit: int = 50, days_back: int = 0) -> str:
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

    api, err = _api_or_error()
    if err:
        return err

    limit = min(max(1, limit), 500)

    try:
        if contact and not chat_id:
            chats = api.get_chats(500)
            found = _find_chat_by_contact(chats, contact)
            if not found:
                return f'No chat found for contact "{contact}".'
            chat_id = found["guid"]

        messages = api.get_messages_by_chat(chat_id, limit)
    except Exception as e:
        return f"API error: {e}"

    if days_back > 0:
        cutoff_ms = (datetime.now(timezone.utc).timestamp() - days_back * 86400) * 1000
        messages = [m for m in messages if (m.get("date") or 0) > cutoff_ms]

    if not messages:
        return f"No messages found in chat `{chat_id}`."

    chat_name = chat_id
    try:
        chat = api.get_chat(chat_id)
        if chat:
            chat_name = _chat_display(chat)
    except Exception:
        pass

    service = _service_from_guid(chat_id)
    lines = [
        f"**Chat:** {chat_name} (`{chat_id}`) [{service}]",
        f"**Reply with:** `send_message(to=\"{chat_id}\", ...)`\n",
        "**Messages** (newest first):\n",
    ]
    for msg in messages:
        line = _format_message_line(msg)
        if line:
            lines.append(line)

    return "\n".join(lines)


@mcp.tool()
def search_messages(query: str, contact: str = "", limit: int = 30, days_back: int = 0) -> str:
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

    api, err = _api_or_error()
    if err:
        return err

    limit = min(max(1, limit), 200)

    try:
        results = api.search(query, limit)

        if contact:
            chats = api.get_chats(500)
            found = _find_chat_by_contact(chats, contact)
            if not found:
                return f'No chat found for contact "{contact}".'
            target_id = found["id"]
            target_guid = found["guid"]
            results = [r for r in results if r.get("chatId") == target_id or r.get("chatGuid") == target_guid]
    except Exception as e:
        return f"API error: {e}"

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


@mcp.tool()
def send_message(to: str, text: str) -> str:
    """
    Send an iMessage/SMS to a phone number, email, or existing chat.

    IMPORTANT: This will actually send a message. Double-check the recipient and content.

    IMPORTANT: When replying to an existing conversation, ALWAYS use the full chat ID
    (e.g. 'iMessage;-;+15551234567', 'SMS;-;+15551234567', or 'any;+;<guid>') so the
    reply lands in the same thread.

    Args:
        to: Chat ID, phone number, or email. Prefer the full chat ID from list_chats.
        text: The message text to send.
    """
    if not text.strip():
        return "Cannot send an empty message."

    api, err = _api_or_error()
    if err:
        return err

    service = "SMS" if to.startswith("SMS;") else "iMessage"

    try:
        api.send_text(to=to, text=text, service=service)
        return f"Message sent to `{to}`: {text}"
    except Exception as e:
        return f"Failed to send message: {e}"


@mcp.tool()
def send_attachment(to: str, file_path: str, text: str = "") -> str:
    """
    Send a file attachment via iMessage/SMS.

    IMPORTANT: This will actually send. Double-check the recipient and file.

    Args:
        to: Chat ID, phone number, or email.
        file_path: Absolute path to the file to send.
        text: Optional accompanying text.
    """
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return f"File not found: {path}"
    if not path.is_file():
        return f"Not a file: {path}"

    api, err = _api_or_error()
    if err:
        return err

    service = "SMS" if to.startswith("SMS;") else "iMessage"

    try:
        api.send_attachment(to=to, file_path=str(path), text=text, service=service)
    except Exception as e:
        return f"Failed to send attachment: {e}"

    result = f"Attachment sent to `{to}`: {path.name}"
    if text.strip():
        result += f" with message: {text}"
    return result


@mcp.tool()
def get_recent_messages(limit: int = 30, days_back: int = 1) -> str:
    """
    Get the most recent messages across ALL chats.

    Args:
        limit: Maximum number of messages (default 30, max 200).
        days_back: Only include messages from the last N days (default 1).
    """
    api, err = _api_or_error()
    if err:
        return err

    limit = min(max(1, limit), 200)

    try:
        chats = api.get_chats(50)
    except Exception as e:
        return f"API error: {e}"

    if not chats:
        return "No recent messages found."

    cutoff_ms = (datetime.now(timezone.utc).timestamp() - days_back * 86400) * 1000 if days_back > 0 else 0
    if cutoff_ms > 0:
        chats = [c for c in chats if (c.get("lastMessageDate") or 0) > cutoff_ms]

    if not chats:
        return "No recent messages found."

    per_chat = max(5, limit // len(chats))
    all_messages = []

    for chat in chats:
        guid = chat.get("guid", "")
        service = chat.get("serviceName") or _service_from_guid(guid)
        display = _chat_display(chat)
        try:
            msgs = api.get_messages_by_chat(guid, per_chat)
        except Exception:
            continue
        for msg in msgs:
            msg["_chat_display"] = display
            msg["_chat_service"] = service
            msg["_chat_guid"] = guid
        all_messages.extend(msgs)

    if cutoff_ms > 0:
        all_messages = [m for m in all_messages if (m.get("date") or 0) > cutoff_ms]
    all_messages = [
        m for m in all_messages
        if m.get("text") and (m.get("associatedMessageType", 0) or 0) == 0
    ]
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


@mcp.tool()
def get_attachments(chat_id: str = "", contact: str = "", limit: int = 20) -> str:
    """
    List attachments from a specific chat.

    Args:
        chat_id: The chat ID. Optional if contact is provided.
        contact: Contact name/phone to look up. Optional if chat_id is provided.
        limit: Maximum number of attachments (default 20, max 100).
    """
    if not chat_id and not contact:
        return "Please provide either a `chat_id` or `contact`."

    api, err = _api_or_error()
    if err:
        return err

    limit = min(max(1, limit), 100)

    try:
        if contact and not chat_id:
            chats = api.get_chats(500)
            found = _find_chat_by_contact(chats, contact)
            if not found:
                return f'No chat found for contact "{contact}".'
            chat_id = found["guid"]

        messages = api.get_messages_by_chat(chat_id, 200)
        chat = api.get_chat(chat_id)
        chat_name = _chat_display(chat) if chat else chat_id
    except Exception as e:
        return f"API error: {e}"

    entries = []
    for msg in messages:
        atts = msg.get("attachments") or []
        for att in atts:
            att_id = att.get("id")
            if att_id is None:
                continue
            entries.append({
                "id": att_id,
                "filename": att.get("transferName") or att.get("filename") or "unknown",
                "mime": att.get("mimeType") or "unknown",
                "size": att.get("totalBytes") or 0,
                "is_sticker": att.get("isSticker", False),
                "ts": _unix_ms_to_iso(msg.get("date")),
                "sender": "You" if msg.get("isFromMe") else (msg.get("senderName") or "?"),
            })
            if len(entries) >= limit:
                break
        if len(entries) >= limit:
            break

    if not entries:
        return f"No attachments found in chat `{chat_id}`."

    lines = [f"**Attachments in {chat_name}:**\n"]
    for att in entries:
        size_str = _format_bytes(att["size"]) if att["size"] else "?"
        sticker = " (sticker)" if att["is_sticker"] else ""
        lines.append(
            f"- [{att['ts']}] **{att['sender']}**: {att['filename']} "
            f"({att['mime']}, {size_str}){sticker}"
        )
        lines.append(f"  Download: `GET /attachments/{att['id']}`")

    return "\n".join(lines)


@mcp.tool()
def fetch_attachment(attachment_id: int, save_dir: str = "/tmp/imessage-attachments") -> str:
    """
    Download an attachment from the imessage-api backend and save it locally.

    Use `get_attachments` first to find the attachment id.

    Args:
        attachment_id: The numeric attachment id (from get_attachments).
        save_dir: Directory to save the file into (default /tmp/imessage-attachments).
                  Created if it doesn't exist.
    """
    api, err = _api_or_error()
    if err:
        return err

    try:
        data, content_type, filename = api.download_attachment(attachment_id)
    except Exception as e:
        return f"Failed to download attachment {attachment_id}: {e}"

    if not filename:
        import mimetypes
        ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
        filename = f"attachment_{attachment_id}{ext}"

    out_dir = Path(save_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_bytes(data)

    return (
        f"Saved attachment {attachment_id} → `{out_path}`\n"
        f"- Size: {_format_bytes(len(data))}\n"
        f"- Content-Type: {content_type}"
    )


@mcp.tool()
def check_db_access() -> str:
    """
    Check whether the imessage-api backend is reachable and authenticated.

    Tools require IMESSAGE_API_URL and IMESSAGE_API_PASSWORD env vars.
    """
    api, err = _api_or_error()
    if err:
        return err
    try:
        health = api.health()
        api.get_chats(1)  # forces auth round-trip
        return (
            f"imessage-api is connected.\n"
            f"- API status: {health.get('status', 'unknown')}\n"
            f"- API URL: {api.base_url}\n"
            f"- Authentication: OK"
        )
    except Exception as e:
        return (
            f"imessage-api is configured but not reachable.\n"
            f"- API URL: {api.base_url}\n"
            f"- Error: {e}\n\n"
            f"Make sure the API server is running on the macOS host."
        )


if __name__ == "__main__":
    mcp.run()
