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
    "Sessions": ["telegram_user_id", "platform", "session_ref", "encrypted", "status", "updated_at"],
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
        if not result.get("values"):
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
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        _ensure_tabs()
        api, sid = _api(), _sheet_id()
        rows = api.spreadsheets().values().get(
            spreadsheetId=sid, range="'Accounts'!A:H"
        ).execute().get("values", [])
        row_number = None
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) > 1 and str(row[0]) == str(user_id) and row[1] == "youtube":
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
