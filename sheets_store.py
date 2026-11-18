import json
import logging
import os
import threading
from datetime import datetime, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

log = logging.getLogger("youtube-agent.sheets")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_LOCK = threading.RLock()
_SERVICE = None

SCHEMAS = {
    "Users": ["telegram_user_id", "chat_id", "username", "first_name", "registered_at", "last_seen", "status"],
    "Accounts": ["telegram_user_id", "platform", "account_name", "connection_status", "connected_at", "updated_at", "channel_id", "refresh_token_enc"],
    "Settings": ["telegram_user_id", "reply_mode", "manual_reply_text", "comment_poll_seconds", "updated_at"],
    "Upload_History": ["telegram_user_id", "platform", "video_id", "video_url", "title", "uploaded_at", "status"],
    "Comments": ["telegram_user_id", "platform", "comment_id", "video_id", "comment_text", "reply_text", "reply_mode", "replied_at", "status"],
    "Comment_Reply_Ledger": ["channel_id", "comment_id", "status", "updated_at"],
    "Sessions": ["telegram_user_id", "platform", "session_ref", "encrypted", "status", "updated_at"],
    "Content_Drafts": ["telegram_user_id", "platform", "topic", "caption", "hashtags", "created_at", "status"],
}


def _config():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not raw or not sheet_id:
        raise RuntimeError("Google Sheets setup missing: set GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON must contain valid service-account JSON") from exc
    return info, sheet_id


def _api():
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    info, _ = _config()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _SERVICE = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _SERVICE


def _sheet_id():
    return _config()[1]


def _quote(name):
    return "'" + name.replace("'", "''") + "'"


def _ensure_tabs():
    api = _api()
    sid = _sheet_id()
    existing = api.spreadsheets().get(spreadsheetId=sid).execute()
    titles = {s["properties"]["title"] for s in existing.get("sheets", [])}
    requests_body = [{"addSheet": {"properties": {"title": name}}}
                     for name in SCHEMAS if name not in titles]
    if requests_body:
        api.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": requests_body}).execute()
    for name, headers in SCHEMAS.items():
        result = api.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{_quote(name)}!1:1"
        ).execute()
        current_headers = result.get("values", [[]])[0] if result.get("values") else []
        if current_headers != headers:
            api.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{_quote(name)}!A1",
                valueInputOption="RAW", body={"values": [headers]}
            ).execute()


def initialize():
    with _LOCK:
        _ensure_tabs()
    return True


def _append(tab, values):
    with _LOCK:
        _ensure_tabs()
        return _api().spreadsheets().values().append(
            spreadsheetId=_sheet_id(), range=f"{_quote(tab)}!A:Z",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": [[str(v) if v is not None else "" for v in values]]}
        ).execute()


def _upsert(tab, key_column, key, headers, record):
    with _LOCK:
        _ensure_tabs()
        api, sid = _api(), _sheet_id()
        rows = api.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{_quote(tab)}!A:A"
        ).execute().get("values", [])
        row_number = None
        for idx, row in enumerate(rows[1:], start=2):
            if row and str(row[0]) == str(key):
                row_number = idx
                break
        values = [[record.get(h, "") for h in headers]]
        if row_number:
            api.spreadsheets().values().update(
                spreadsheetId=sid, range=f"{_quote(tab)}!A{row_number}",
                valueInputOption="USER_ENTERED", body={"values": values}
            ).execute()
        else:
            api.spreadsheets().values().append(
                spreadsheetId=sid, range=f"{_quote(tab)}!A:Z",
                valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
                body={"values": values}
            ).execute()


def register_user(user_id, chat_id, username="", first_name=""):
    now = datetime.now(timezone.utc).isoformat()
    headers = SCHEMAS["Users"]
    record = {
        "telegram_user_id": user_id, "chat_id": chat_id, "username": username,
        "first_name": first_name, "registered_at": now, "last_seen": now, "status": "registered"
    }
    _upsert("Users", "telegram_user_id", user_id, headers, record)
    with _LOCK:
        _ensure_tabs()
        existing = _api().spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range="'Settings'!A:A"
        ).execute().get("values", [])
        already_registered = any(row and str(row[0]) == str(user_id) for row in existing[1:])
        if not already_registered:
            _append("Settings", [user_id, "ai", "", "240", now])
    return True


def record_account(user_id, platform, account_name="", status="connected"):
    now = datetime.now(timezone.utc).isoformat()
    _append("Accounts", [user_id, platform, account_name, status, now, now])


def record_upload(user_id, platform, video_id, video_url, title, status="uploaded"):
    _append("Upload_History", [user_id, platform, video_id, video_url, title,
                               datetime.now(timezone.utc).isoformat(), status])


def record_comment(user_id, platform, comment_id, video_id, comment_text,
                   reply_text="", reply_mode="ai", status="replied"):
    _append("Comments", [user_id, platform, comment_id, video_id, comment_text,
                         reply_text, reply_mode, datetime.now(timezone.utc).isoformat(), status])


def record_session_reference(user_id, platform, session_ref, status="stored"):
    # session_ref must be an opaque reference, never a password or raw cookie.
    _append("Sessions", [user_id, platform, session_ref, "yes", status,
                         datetime.now(timezone.utc).isoformat()])



def save_youtube_connection(user_id, account_name, channel_id, encrypted_refresh_token):
    """Upsert one channel per Telegram user; preserve other connected channels."""
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        _ensure_tabs()
        api, sid = _api(), _sheet_id()
        rows = api.spreadsheets().values().get(
            spreadsheetId=sid, range="'Accounts'!A:H"
        ).execute().get("values", [])
        row_number = None
        for idx, row in enumerate(rows[1:], start=2):
            if (len(row) > 6 and str(row[0]) == str(user_id)
                    and row[1] == "youtube" and row[6] == channel_id):
                row_number = idx
                break
        values = [[str(user_id), "youtube", account_name, "connected", now, now,
                   channel_id, encrypted_refresh_token]]
        if row_number:
            api.spreadsheets().values().update(
                spreadsheetId=sid, range=f"'Accounts'!A{row_number}",
                valueInputOption="RAW", body={"values": values}
            ).execute()
        else:
            api.spreadsheets().values().append(
                spreadsheetId=sid, range="'Accounts'!A:H",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": values}
            ).execute()
    return True


def list_youtube_connections(user_id):
    """Return this Telegram user's YouTube metadata, never refresh tokens."""
    with _LOCK:
        _ensure_tabs()
        rows = _api().spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range="'Accounts'!A:H"
        ).execute().get("values", [])
    accounts = []
    for row in rows[1:]:
        if len(row) >= 7 and str(row[0]) == str(user_id) and row[1] == "youtube":
            accounts.append({
                "account_name": row[2] if len(row) > 2 else "",
                "connection_status": row[3] if len(row) > 3 else "",
                "channel_id": row[6] if len(row) > 6 else "",
            })
    return accounts


def get_youtube_connection(user_id, channel_id):
    """Return one user's encrypted refresh token for the exact selected channel."""
    with _LOCK:
        _ensure_tabs()
        rows = _api().spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range="'Accounts'!A:H"
        ).execute().get("values", [])
    for row in rows[1:]:
        if (len(row) >= 8 and str(row[0]) == str(user_id)
                and row[1] == "youtube" and row[6] == channel_id
                and row[3] == "connected"):
            return {
                "account_name": row[2],
                "channel_id": row[6],
                "refresh_token_enc": row[7],
            }
    return None


def claim_comment_reply(channel_id, comment_id, stale_after_seconds=900):
    """Persist a short-lived claim without storing comment or reply text."""
    now = datetime.now(timezone.utc)
    with _LOCK:
        _ensure_tabs()
        api, sid = _api(), _sheet_id()
        rows = api.spreadsheets().values().get(
            spreadsheetId=sid, range="'Comment_Reply_Ledger'!A:D"
        ).execute().get("values", [])
        row_number = None
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) >= 2 and row[0] == str(channel_id) and row[1] == str(comment_id):
                row_number = idx
                status = row[2] if len(row) > 2 else ""
                updated = row[3] if len(row) > 3 else ""
                if status == "replied":
                    return False
                if status == "processing":
                    try:
                        age = (now - datetime.fromisoformat(updated)).total_seconds()
                    except (TypeError, ValueError):
                        age = stale_after_seconds + 1
                    if age < stale_after_seconds:
                        return False
                break
        values = [[str(channel_id), str(comment_id), "processing", now.isoformat()]]
        if row_number:
            api.spreadsheets().values().update(
                spreadsheetId=sid, range=f"'Comment_Reply_Ledger'!A{row_number}",
                valueInputOption="RAW", body={"values": values}
            ).execute()
        else:
            api.spreadsheets().values().append(
                spreadsheetId=sid, range="'Comment_Reply_Ledger'!A:D",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": values}
            ).execute()
        return True


def mark_comment_reply(channel_id, comment_id, status):
    if status not in ("replied", "pending"):
        raise ValueError("Invalid comment reply ledger status")
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        _ensure_tabs()
        api, sid = _api(), _sheet_id()
        rows = api.spreadsheets().values().get(
            spreadsheetId=sid, range="'Comment_Reply_Ledger'!A:D"
        ).execute().get("values", [])
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) >= 2 and row[0] == str(channel_id) and row[1] == str(comment_id):
                api.spreadsheets().values().update(
                    spreadsheetId=sid, range=f"'Comment_Reply_Ledger'!C{idx}",
                    valueInputOption="RAW", body={"values": [[status, now]]}
                ).execute()
                return True
        raise RuntimeError("Comment reply ledger claim not found")

def save_social_draft(user_id, draft):
    now = datetime.now(timezone.utc).isoformat()
    _append("Content_Drafts", [
        user_id, draft["platform"], draft["topic"], draft["caption"],
        draft["hashtags"], now, "draft"
    ])
    return True


def list_social_drafts(user_id, limit=5):
    with _LOCK:
        _ensure_tabs()
        rows = _api().spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range="'Content_Drafts'!A:G"
        ).execute().get("values", [])
    items = []
    for row in rows[1:]:
        if len(row) >= 7 and str(row[0]) == str(user_id):
            items.append({
                "platform": row[1], "topic": row[2], "caption": row[3],
                "hashtags": row[4], "created_at": row[5], "status": row[6],
            })
    return items[-max(1, min(int(limit), 20)):][::-1]


def list_all_youtube_connections():
    """Internal worker view of connected channels; tokens remain encrypted at rest."""
    with _LOCK:
        _ensure_tabs()
        rows = _api().spreadsheets().values().get(
            spreadsheetId=_sheet_id(), range="'Accounts'!A:H"
        ).execute().get("values", [])
    accounts = []
    for row in rows[1:]:
        if (len(row) >= 8 and row[1] == "youtube"
                and row[3] == "connected" and row[6] and row[7]):
            accounts.append({
                "telegram_user_id": str(row[0]),
                "account_name": row[2],
                "channel_id": row[6],
                "refresh_token_enc": row[7],
            })
    return accounts
