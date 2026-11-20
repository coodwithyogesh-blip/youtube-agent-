import os, hmac, json, logging, secrets, urllib.parse, html, tempfile, threading, time
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, redirect, Response
from werkzeug.exceptions import HTTPException
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from upload_utils import parse_publish_at, build_video_metadata
from ai_utils import (generate_video_metadata, should_auto_reply, generate_content_with_fallback, clean_generated_metadata)
from sheets_store import (register_user, save_youtube_connection, list_youtube_connections,
                          get_youtube_connection, list_all_youtube_connections)
from youtube_insights import (comment_poll_interval, score_video_seo, weak_seo_videos, seo_score_distribution)
from social_drafts import build_social_draft
from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("youtube-agent")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "90")) * 1024 * 1024
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/yt-analytics.readonly"]
DONE = set()
COMMENT_PENDING_COUNTS = {}  # in-memory counts only; never stores comment text
# Telegram may retry a webhook update while a slow AI/video upload is running.
# Remember update IDs before processing so duplicate media updates are ignored.
DONE_TELEGRAM_UPDATES = set()
LAST_COMMENT_SCAN = {}
OAUTH_STATES = set()
MEDIA = {}
SEO_FIX_PENDING = {}
# Social drafts are deliberately process-memory only; never write them to Google Sheets.
SOCIAL_DRAFTS = {}
SOCIAL_MEDIA = {}  # temporary video files; never persisted
AUTO_THREAD_STARTED = False
def env(name, default=""):
    return os.getenv(name, default).strip()

TELEGRAM_API = env("TELEGRAM_API_BASE", "https://api.telegram.org/bot")
TELEGRAM_FILE_API = env("TELEGRAM_FILE_BASE", "https://api.telegram.org/file/bot")


def youtube_oauth_state_serializer():
    secret = env("OAUTH_STATE_SECRET")
    if not secret:
        raise RuntimeError("OAUTH_STATE_SECRET is not configured")
    return URLSafeTimedSerializer(secret, salt="youtube-multi-user-oauth")


def make_youtube_oauth_state(user_id):
    nonce = secrets.token_urlsafe(24)
    OAUTH_STATES.add(nonce)
    return youtube_oauth_state_serializer().dumps({"user_id": int(user_id), "nonce": nonce})


def read_youtube_oauth_state(value):
    try:
        return youtube_oauth_state_serializer().loads(value, max_age=600)
    except (BadSignature, SignatureExpired):
        return None


def encrypt_refresh_token(token):
    key = env("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    return Fernet(key.encode("utf-8")).encrypt(token.encode("utf-8")).decode("utf-8")

def decrypt_refresh_token(encrypted_token):
    key = env("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured")
    return Fernet(key.encode("utf-8")).decrypt(
        encrypted_token.encode("utf-8")
    ).decode("utf-8")


def youtube_credentials_for_token(refresh_token):
    return Credentials(None, refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"), scopes=SCOPES)


def youtube_service_for_user(user_id, channel_id):
    account = get_youtube_connection(user_id, channel_id)
    if not account:
        raise RuntimeError("Selected YouTube channel is not connected to this Telegram user")
    refresh_token = decrypt_refresh_token(account["refresh_token_enc"])
    return build("youtube", "v3",
        credentials=youtube_credentials_for_token(refresh_token),
        cache_discovery=False)


def youtube_upload_keyboard(accounts):
    rows = []
    for account in accounts:
        channel_id = str(account.get("channel_id") or "")
        if channel_id:
            name = str(account.get("account_name") or "YouTube channel")[:35]
            rows.append([{"text": "▶️ " + name, "callback_data": "ytselect:" + channel_id}])
    rows.append([{"text": "🔗 Connect another YouTube", "callback_data": "platform_youtube"}])
    rows.append([{"text": "⬅️ Main Menu", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def telegram_html(value):
    return html.escape(str(value or ""), quote=False)

def tg(method, payload):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    r = requests.post(f"{TELEGRAM_API}{token}/{method}", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data.get("result")

def send_tg(chat_id, message):
    tg("sendMessage", {"chat_id":chat_id,"text":message,"disable_web_page_preview":True})

def yt():
    required = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]
    missing = [k for k in required if not env(k)]
    if missing:
        raise RuntimeError("Missing YouTube settings: " + ", ".join(missing))
    creds = Credentials(None, refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token", client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"), scopes=SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def ai_reply(comment, video_title=""):
    key = env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is required for AI comment replies")
    model = env("GEMINI_MODEL", "gemini-3.8-flash")
    fallback_model = env("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    prompt = ("Write one short, natural, respectful YouTube creator reply. Do not invent facts, "
        "promise anything, request personal data, or include links. For medical, legal, financial "
        "or private requests, politely say the creator cannot advise.\n"
        f"Video: {video_title}\nComment: {comment}")
    data = generate_content_with_fallback(
        key, model,
        {"contents":[{"parts":[{"text":prompt}]}],
         "generationConfig":{"temperature":0.4,"maxOutputTokens":160}},
        timeout=35, fallback_model=fallback_model, max_attempts=6)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()[:900]

def authorized(user_id, chat_id):
    owner, group = env("TELEGRAM_OWNER_ID"), env("TELEGRAM_GROUP_ID")
    return bool(owner and str(user_id) == owner and (not group or str(chat_id) == group))

def telegram_download(file_id, suffix):
    info = tg("getFile", {"file_id":file_id})
    path = info.get("file_path","")
    size = int(info.get("file_size",0))
    # Standard Telegram Bot API downloads are capped at 20 MB. Local Bot API
    # mode is used for larger files; don't apply that limit in local mode.
    local_mode = bool(env("TELEGRAM_FILE_BASE"))
    if not path or (not local_mode and size > 20 * 1024 * 1024):
        raise RuntimeError("Standard Telegram API download limit is 20 MB. Deploy Local Bot API mode for larger videos.")
    token = env("TELEGRAM_BOT_TOKEN")
    file_url = f"{TELEGRAM_FILE_API}{token}/{path}"
    response = requests.get(file_url, timeout=(30, 300), stream=True)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        for chunk in response.iter_content(chunk_size=1024*1024):
            if chunk:
                tmp.write(chunk)
        return tmp.name

def remove_media(chat_id):
    item = MEDIA.pop(str(chat_id), {})
    for path in item.values():
        if isinstance(path, str) and os.path.isfile(path):
            try: os.remove(path)
            except OSError: pass

def upload_telegram_pair(chat_id):
    item = MEDIA.get(str(chat_id), {})
    if not item.get("video") or not item.get("thumbnail") or item.get("busy"):
        return
    item["busy"] = True
    try:
        send_tg(chat_id, "⏳ Gemini ko video aur thumbnail bhej raha hoon. Ismein thoda samay lag sakta hai…")
        metadata = generate_video_metadata(env("GEMINI_API_KEY"),
            env("GEMINI_MODEL","gemini-3.8-flash"), item["video"], item.get("filename","video.mp4"),
            item.get("caption",""), thumbnail_path=item["thumbnail"])
        body = build_video_metadata(metadata["title"], metadata["description"],
                                    json.dumps(metadata["tags"]), "public", None)
        media = MediaFileUpload(item["video"], chunksize=8*1024*1024,
                                resumable=True, mimetype=item.get("mime","video/mp4"))
        service = youtube_service_for_user(item["user_id"], item["selected_channel_id"])
        result = service.videos().insert(part="snippet,status", body=body, media_body=media).execute()
        video_id = result["id"]
        if os.path.getsize(item["thumbnail"]) > 2*1024*1024:
            raise RuntimeError("Video upload ho gaya, lekin thumbnail 2 MB se bada hai. YouTube limit ke andar thumbnail bhejo.")
        service.thumbnails().set(videoId=video_id,
            media_body=MediaFileUpload(item["thumbnail"], resumable=False,
                                       mimetype=item.get("thumb_mime","image/jpeg"))).execute()
        send_tg(chat_id, "✅ YouTube par upload ho gaya!\n\n"
            f"Title: {metadata['title']}\nTags: {', '.join(metadata['tags'][:12])}\n"
            f"Hashtags: {' '.join(metadata['hashtags'])}\n"
            f"Video: https://youtu.be/{video_id}")
    except Exception as exc:
        log.exception("Telegram media publish failed")
        send_tg(chat_id, "❌ Upload nahi hua: " + str(exc)[:700])
    finally:
        remove_media(chat_id)

def receive_media(message, chat_id, user_id):
    key = str(chat_id)
    item = MEDIA.setdefault(key,{})
    if not item.get("selected_channel_id"):
        send_tg(chat_id, "Pehle Upload Video dabao aur apna YouTube channel select karo.")
        return False
    item["user_id"] = str(user_id)
    if message.get("video") or (message.get("document") and
            (message["document"].get("mime_type") or "").startswith("video/")):
        obj = message.get("video") or message["document"]
        if item.get("video") and os.path.isfile(item["video"]):
            os.remove(item["video"])
        item["video"] = telegram_download(obj["file_id"], ".mp4")
        item["filename"] = obj.get("file_name") or "telegram-video.mp4"
        item["mime"] = obj.get("mime_type") or "video/mp4"
        item["caption"] = message.get("caption","")
        send_tg(chat_id,"✅ Video mil gaya. Ab uska thumbnail photo ke roop mein bhejo.")
    elif message.get("photo"):
        photo = message["photo"][-1]
        if item.get("thumbnail") and os.path.isfile(item["thumbnail"]):
            os.remove(item["thumbnail"])
        item["thumbnail"] = telegram_download(photo["file_id"], ".jpg")
        item["thumb_mime"] = "image/jpeg"
        send_tg(chat_id,"✅ Thumbnail mil gaya.")
    else:
        return False
    if item.get("video") and item.get("thumbnail"):
        # Run slow Gemini processing outside the Telegram webhook request.
        threading.Thread(target=upload_telegram_pair, args=(chat_id,), daemon=True).start()
    return True

def remove_social_media(chat_id):
    item = SOCIAL_MEDIA.pop(str(chat_id), {})
    path = item.get("video")
    if path and os.path.isfile(path):
        try: os.remove(path)
        except OSError: pass


def receive_social_video(message, chat_id, user_id):
    item = SOCIAL_MEDIA.get(str(chat_id))
    if not item or item.get("user_id") != str(user_id):
        return False
    obj = message.get("video") or message.get("document")
    if not obj or (message.get("document") and not (obj.get("mime_type") or "").startswith("video/")):
        send_tg(chat_id, "Video file bhejo (Local Bot API mode mein 20 MB se bade videos bhi supported hain, hosting limits ke adheen).")
        return True
    path = telegram_download(obj["file_id"], ".mp4")
    item.update({"video": path, "filename": obj.get("file_name") or "video.mp4",
                 "caption": message.get("caption", "")})
    try:
        send_tg(chat_id, "🎬 Video mil gaya. AI visuals aur audio/speech analyse kar raha hai…")
        metadata = generate_video_metadata(env("GEMINI_API_KEY"),
            env("GEMINI_MODEL", "gemini-3.8-flash"), path, item["filename"],
            item["caption"], platform=item["platform"])
        item["social_metadata"] = metadata
        send_tg(chat_id, "✅ " + item["platform"].title() + " ready-to-post content:\n\n"
            + metadata["description"][:1800] + "\n\nHashtags: "
            + " ".join(metadata["hashtags"])
            + "\n\nDirect Instagram/Facebook/Snapchat posting official API ke bina supported nahi hai. "
              "Kya isi video ko YouTube par bhi upload karna hai?")
        tg("sendMessage", {"chat_id": chat_id, "text": "YouTube upload karna hai?",
            "reply_markup": {"inline_keyboard": [
                [{"text": "✅ Haan", "callback_data": "socialyt_yes"},
                 {"text": "❌ Nahi", "callback_data": "socialyt_no"}]]}})
    except Exception as exc:
        log.exception("Social video analysis failed")
        remove_social_media(chat_id)
        send_tg(chat_id, "❌ Video analyse nahi hua: " + str(exc)[:350])
    return True


def upload_social_video_to_youtube(chat_id, user_id, channel_id):
    item = SOCIAL_MEDIA.get(str(chat_id), {})
    if not item.get("video") or item.get("user_id") != str(user_id):
        raise RuntimeError("Video session expire ho gaya. Video dobara bhejo.")
    metadata = generate_video_metadata(env("GEMINI_API_KEY"),
        env("GEMINI_MODEL", "gemini-3.8-flash"), item["video"],
        item.get("filename", "video.mp4"), item.get("caption", ""), platform="youtube")
    body = build_video_metadata(metadata["title"], metadata["description"],
        json.dumps(metadata["tags"]), "public", None)
    service = youtube_service_for_user(user_id, channel_id)
    media = MediaFileUpload(item["video"], chunksize=8*1024*1024,
        resumable=True, mimetype="video/mp4")
    video_id = service.videos().insert(part="snippet,status",
        body=body, media_body=media).execute()["id"]
    send_tg(chat_id, "✅ YouTube Public upload ho gaya!\nTitle: " + metadata["title"]
        + "\nTags: " + ", ".join(metadata["tags"][:10])
        + "\nHashtags: " + " ".join(metadata["hashtags"])
        + "\nhttps://youtu.be/" + video_id)
    remove_social_media(chat_id)


def comment_has_owner_reply(thread, channel_id):
    replies = (thread.get("replies") or {}).get("comments", [])
    return any(((reply.get("snippet") or {}).get("authorChannelId") or {}).get("value") == channel_id
               for reply in replies)

def list_recent_comments(service, channel_id):
    # Fetch every page from YouTube's channel comment threads. There is
    # deliberately no date cutoff: old unanswered comments must be eligible too.
    results = []
    page_token = None
    while True:
        response = service.commentThreads().list(
            part="snippet,replies",
            allThreadsRelatedToChannelId=channel_id,
            maxResults=100, order="time", textFormat="plainText",
            pageToken=page_token
        ).execute()
        for thread in response.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            comment = top["snippet"]
            if ((comment.get("authorChannelId") or {}).get("value") == channel_id
                    or comment_has_owner_reply(thread, channel_id)):
                continue
            results.append({
                "id": top["id"], "video_id": comment.get("videoId", ""),
                "title": "", "text": comment.get("textOriginal", comment.get("textDisplay", "")),
            })
        if not response.get("nextPageToken"):
            break
        page_token = response["nextPageToken"]
    # Do not advance the cursor here. The caller advances it only after every
    # eligible comment has been handled; exceptions leave this batch retryable.
    return results

def auto_reply_scan_for_service(service, channel_id, telegram_user_id=None):
    count = 0
    failed_ids = []
    notification_chat_id = telegram_user_id or env("TELEGRAM_OWNER_ID")
    comments = list_recent_comments(service, channel_id)
    for comment in comments:
        cid = comment["id"]
        if cid in DONE or not should_auto_reply(comment["text"]):
            continue
        try:
            reply = ai_reply(comment["text"], comment["title"])
            service.comments().insert(part="snippet",
                body={"snippet": {"parentId": cid, "textOriginal": reply}}).execute()
        except Exception:
            # Keep this comment pending, but do not let one Gemini quota error
            # block other eligible comments in the same channel scan.
            failed_ids.append(cid)
            log.exception("Comment %s remains pending; it will be retried on the next scan", cid)
            continue

        # Mark complete only after YouTube confirms that the reply was posted.
        DONE.add(cid)
        count += 1
        log.info("Auto-replied to YouTube comment %s on channel %s", cid, channel_id)
        if notification_chat_id:
            try:
                send_tg(notification_chat_id,
                    "✅ Replied to your comment successfully!\n"
                    "Channel: " + str(channel_id) + "\n"
                    "Comment: " + str(comment["text"])[:180])
            except Exception:
                log.exception("Telegram success notification failed for comment %s", cid)

    # Store only a count, never comment/reply text. Every scan fetches all pages,
    # so failed comments remain eligible for retry without a Sheets write.
    COMMENT_PENDING_COUNTS[channel_id] = len(failed_ids)
    if failed_ids:
        log.warning("%s YouTube comments remain pending for channel %s",
                    len(failed_ids), channel_id)
    LAST_COMMENT_SCAN[channel_id] = datetime.now(timezone.utc)
    return count

def auto_reply_scan():
    service = yt()
    channels = service.channels().list(part="id", mine=True).execute().get("items", [])
    if not channels:
        raise RuntimeError("No YouTube channel found for legacy owner OAuth")
    return auto_reply_scan_for_service(
        service, channels[0]["id"], telegram_user_id=env("TELEGRAM_OWNER_ID"))


def scan_all_connected_channels(raise_errors=False):
    total = 0
    failures = []
    accounts = list_all_youtube_connections()
    if not accounts and env("YOUTUBE_REFRESH_TOKEN"):
        return auto_reply_scan()
    for account in accounts:
        try:
            token = decrypt_refresh_token(account["refresh_token_enc"])
            service = build("youtube", "v3",
                credentials=youtube_credentials_for_token(token), cache_discovery=False)
            total += auto_reply_scan_for_service(
                service, account["channel_id"],
                telegram_user_id=account.get("telegram_user_id") or env("TELEGRAM_OWNER_ID"))
        except Exception as exc:
            log.exception("Comment scan failed for connected channel %s", account.get("channel_id"))
            failures.append(str(exc)[:180])
    if raise_errors and failures:
        raise RuntimeError("Comment scan failed: " + " | ".join(failures))
    return total


def auto_comment_worker():
    interval = comment_poll_interval(120)  # Fixed two-minute scan cadence
    while True:
        try:
            if env("AUTO_REPLY_MODE", "automatic").lower() == "automatic":
                try:
                    count = scan_all_connected_channels()
                    if count:
                        log.info("Automatic replies posted across connected channels: %s", count)
                except Exception:
                    log.exception("Per-user connected-channel scan unavailable")
                    if env("YOUTUBE_REFRESH_TOKEN"):
                        auto_reply_scan()
        except Exception:
            log.exception("Automatic comment scan failed")
        time.sleep(interval)

def start_auto_worker():
    global AUTO_THREAD_STARTED
    if not AUTO_THREAD_STARTED:
        threading.Thread(target=auto_comment_worker, daemon=True, name="youtube-comments").start()
        AUTO_THREAD_STARTED = True

@app.before_request
def ensure_worker():
    start_auto_worker()

def build_channel_seo_report(user_id, channel_id):
    credentials = youtube_credentials_for_token(
        decrypt_refresh_token(get_youtube_connection(user_id, channel_id)["refresh_token_enc"])
    )
    service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    channel_items = service.channels().list(
        part="snippet,statistics,contentDetails", id=channel_id
    ).execute().get("items", [])
    if not channel_items:
        raise RuntimeError("YouTube channel not found")
    channel = channel_items[0]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    video_ids = []
    page_token = None
    while True:
        playlist = service.playlistItems().list(
            part="snippet", playlistId=uploads, maxResults=50,
            pageToken=page_token
        ).execute()
        video_ids.extend(
            x["snippet"]["resourceId"]["videoId"]
            for x in playlist.get("items", [])
            if x.get("snippet", {}).get("resourceId", {}).get("videoId")
        )
        page_token = playlist.get("nextPageToken")
        if not page_token:
            break
    videos = []
    for offset in range(0, len(video_ids), 50):
        data = service.videos().list(
            part="snippet,statistics", id=",".join(video_ids[offset:offset + 50]),
            maxResults=50
        ).execute()
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            seo = score_video_seo({
                "title": sn.get("title"), "description": sn.get("description"),
                "tags": sn.get("tags", []), "thumbnail": bool(sn.get("thumbnails")),
            })
            videos.append({
                "id": item["id"],
                "title": sn.get("title", "Untitled"),
                "views": int(item.get("statistics", {}).get("viewCount", 0)),
                "score": seo["score"], "suggestions": seo["suggestions"],
            })

    analytics_rows = []
    analytics_error = ""
    try:
        analytics = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=27)
        result = analytics.reports().query(
            ids="channel==MINE", startDate=start_date.isoformat(),
            endDate=end_date.isoformat(),
            metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
            dimensions="day", sort="day"
        ).execute()
        analytics_rows = result.get("rows", [])
    except Exception as exc:
        analytics_error = "Analytics trend unavailable. Google account ko dobara connect karke YouTube Analytics permission allow karo."
        log.info("YouTube Analytics API unavailable: %s", type(exc).__name__)

    stats = channel.get("statistics", {})
    return {
        "channel": channel.get("snippet", {}).get("title", "YouTube channel"),
        "subscribers": int(stats.get("subscriberCount", 0)),
        "views": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "videos": sorted(videos, key=lambda x: x["score"])[:10],
        "weak_videos": sorted(weak_seo_videos(videos), key=lambda x: x["score"]),
        "score_distribution": seo_score_distribution(videos),
        "videos_analyzed": len(videos),
        "average_seo": round(sum(v["score"] for v in videos) / len(videos)) if videos else 0,
        "analytics_rows": analytics_rows,
        "analytics_error": analytics_error,
    }


def send_weak_seo_video_actions(chat_id, channel_id, videos):
    if not videos:
        return
    for offset in range(0, len(videos), 20):
        batch = videos[offset:offset + 20]
        rows = []
        lines = ["🛠 WEAK SEO VIDEOS — " + str(offset + 1) + " to " + str(offset + len(batch))]
        for video in batch:
            score = int(video["score"])
            lines.append(f"\n{score}/100 — {video['title'][:65]}")
            for suggestion in video.get("suggestions", [])[:2]:
                lines.append("• " + suggestion)
            rows.append([{"text": "🛠 Review & Fix: " + video["title"][:28],
                          "callback_data": "seofix:" + channel_id + ":" + video["id"]}])
        tg("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines)[:3900],
                           "reply_markup": {"inline_keyboard": rows}})


def generate_seo_fix_metadata(snippet):
    key = env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing; SEO fix cannot be generated.")
    old_title = str(snippet.get("title") or "")
    old_description = str(snippet.get("description") or "")
    old_tags = snippet.get("tags") or []
    prompt = (
        "Improve YouTube SEO metadata for this existing video. You do NOT see the video itself. "
        "Use only facts explicitly supported by the current title, description and tags. "
        "Do not invent gameplay, events, people, claims, links, dates or promises. If context is "
        "unclear, keep the wording conservative and close to the original. Preserve the original "
        "language and topic. Create a clear natural title <=100 characters, useful description "
        "and 5-15 relevant tags; no keyword stuffing. Return ONLY JSON with title, description, tags.\n"
        + json.dumps({"title": old_title, "description": old_description, "tags": old_tags},
                     ensure_ascii=False)
    )
    result = generate_content_with_fallback(
        key, env("GEMINI_MODEL", "gemini-3.8-flash"),
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": 0.25, "responseMimeType": "application/json"}},
        timeout=60, fallback_model=env("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash"))
    raw = json.loads(result["candidates"][0]["content"]["parts"][0]["text"])
    metadata = clean_generated_metadata(raw)
    if not metadata["title"] or not metadata["description"]:
        raise RuntimeError("AI ne valid title/description nahi banaya.")
    return {"title": metadata["title"], "description": metadata["description"],
            "tags": metadata["tags"][:30]}


def get_seo_fix_video(service, video_id):
    items = service.videos().list(part="snippet", id=video_id).execute().get("items", [])
    if not items:
        raise RuntimeError("Video nahi mila ya channel se remove ho chuka hai.")
    return items[0]


def seo_fix_preview(user_id, channel_id, video_id):
    service = youtube_service_for_user(user_id, channel_id)
    item = get_seo_fix_video(service, video_id)
    snippet = item.get("snippet", {})
    metadata = generate_seo_fix_metadata(snippet)
    key = f"{user_id}:{channel_id}:{video_id}"
    SEO_FIX_PENDING[key] = {
        "metadata": metadata,
        "original": {"title": snippet.get("title", ""),
                     "description": snippet.get("description", ""),
                     "tags": snippet.get("tags", [])},
        "created_at": time.time(),
    }
    return metadata


def apply_confirmed_seo_fix(user_id, channel_id, video_id):
    key = f"{user_id}:{channel_id}:{video_id}"
    pending = SEO_FIX_PENDING.get(key)
    if not pending or time.time() - pending["created_at"] > 900:
        SEO_FIX_PENDING.pop(key, None)
        raise RuntimeError("Preview expire ho gaya. SEO report se dobara Review & Fix dabao.")
    service = youtube_service_for_user(user_id, channel_id)
    item = get_seo_fix_video(service, video_id)
    current = item.get("snippet", {})
    original = pending["original"]
    if (current.get("title", "") != original["title"]
            or current.get("description", "") != original["description"]
            or current.get("tags", []) != original["tags"]):
        SEO_FIX_PENDING.pop(key, None)
        raise RuntimeError("Video metadata preview ke baad change hua. Naya preview banao.")
    updated_snippet = {
        "title": pending["metadata"]["title"],
        "description": pending["metadata"]["description"],
        "tags": pending["metadata"]["tags"],
        "categoryId": current.get("categoryId", "22"),
    }
    for field in ("defaultLanguage", "defaultAudioLanguage"):
        if current.get(field):
            updated_snippet[field] = current[field]
    service.videos().update(
        part="snippet", body={"id": video_id, "snippet": updated_snippet}
    ).execute()
    SEO_FIX_PENDING.pop(key, None)
    return pending["metadata"]


def format_channel_seo_report(report):
    lines = [
        "📊 CHANNEL SEO & ANALYTICS",
        report["channel"], "",
        "👥 Subscribers: " + format(report["subscribers"], ","),
        "👁 Lifetime views: " + format(report["views"], ","),
        "🎬 Total videos: " + format(report["video_count"], ","),
        "🔎 Videos SEO checked: " + format(report["videos_analyzed"], ","),
        "⭐ Average SEO score: " + str(report["average_seo"]) + "/100",
        "📉 Weak SEO videos (below 80): " + str(len(report["weak_videos"])),
        "",
        "📊 SEO SCORE DISTRIBUTION",
    ]
    bands = report["score_distribution"]
    max_band = max(bands.values() or [1]) or 1
    for label, key in (("Poor 0–39", "poor"), ("Fair 40–69", "fair"),
                       ("Good 70–89", "good"), ("Excellent 90–100", "excellent")):
        count = bands[key]
        lines.append(f"{label}: {'█' * round(12 * count / max_band)} {count}")
    lines.append("")
    rows = report["analytics_rows"]
    if rows:
        max_views = max([int(row[1]) for row in rows] or [1]) or 1
        lines.append("📈 Daily views — last 28 days")
        for row in rows[-14:]:
            value = int(row[1])
            bars = "▰" * max(1, round(12 * value / max_views))
            lines.append(f"{str(row[0])[-5:]} {bars} {value:,}")
        total_minutes = sum(int(row[2]) for row in rows)
        gained = sum(int(row[3]) for row in rows)
        lost = sum(int(row[4]) for row in rows)
        lines.extend([
            "", f"⏱ Watch time: {total_minutes:,} minutes",
            f"➕ Subscribers gained: {gained:,}",
            f"➖ Subscribers lost: {lost:,}",
        ])
    elif report["analytics_error"]:
        lines.extend(["📈 Watch-time/growth graph:", report["analytics_error"]])
    lines.extend(["", "🔎 Lowest SEO scores"])
    for video in report["videos"][:5]:
        bars = "█" * (video["score"] // 10) + "░" * (10 - video["score"] // 10)
        lines.append(f"{bars} {video['score']}/100 — {video['title'][:42]}")
    if report["weak_videos"]:
        lines.append("\n🛠 Neeche har weak video ke liye alag Fix button milega.")
    else:
        lines.append("\n✅ Koi video 80 se kam SEO score par nahi hai.")
    lines.extend([
        "",
        "SEO score metadata completeness hai, YouTube ranking ki guarantee nahi.",
        "Har edit se pehle preview aur tumhari confirmation zaroori hai.",
    ])
    return "\n".join(lines)[:3900]


def main_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "▶️ YouTube", "callback_data": "platform_youtube"},
         {"text": "👤 My Accounts", "callback_data": "my_accounts"}],
        [{"text": "⬆️ Upload Video", "callback_data": "upload_video"}],
        [{"text": "📸 Instagram", "callback_data": "platform_instagram"},
         {"text": "📘 Facebook", "callback_data": "platform_facebook"}],
        [{"text": "👻 Snapchat", "callback_data": "platform_snapchat"},
         {"text": "💬 Comment Replies", "callback_data": "comments"}],
        [{"text": "📊 Channel SEO & Analytics", "callback_data": "seo_analytics"}],
        [{"text": "⚙️ Settings", "callback_data": "settings"}],
    ]}


def main_menu_text(first_name=""):
    greeting = f" {first_name}" if first_name else ""
    return ("👋 Welcome" + greeting + " to Social Media Agent!\n\n"
            "Apna platform choose karo. YouTube account connect karke My Accounts mein dekh sakte ho.\n\n"
            "🔒 Security: Instagram/Facebook password ya cookies kabhi nahi maange, collect ya save nahi kiye jaate.\n"
            "📝 Instagram/Facebook/Snapchat drafts temporary memory mein rahenge; Google Sheets mein save nahi honge.")


def format_youtube_accounts(accounts):
    if not accounts:
        return "👤 My Accounts\n\nAbhi connected nahi hai. YouTube button se account connect karo."
    lines = ["👤 My Accounts", "", "✅ Connected YouTube account(s):"]
    for index, account in enumerate(accounts, 1):
        name = telegram_html(account.get("account_name") or "YouTube channel")
        channel_id = telegram_html(account.get("channel_id") or "ID unavailable")
        lines.append(f"{index}. {name}\n   Channel ID: {channel_id}")
    return "\n".join(lines)


def callback_response(data):
    messages = {
        "platform_youtube": ("▶️ YouTube\n\nAbhi existing YouTube upload feature owner ke "
            "configured channel par kaam karta hai. Har user ka alag YouTube OAuth connection "
            "abhi ready nahi hai."),
        "platform_instagram": "📸 Instagram selected. Ab video bhejo; AI visuals/audio analyse karke caption banayega. Posting manually hogi; Password/cookies nahi maange jaate.",
        "platform_facebook": "📘 Facebook selected. Ab video bhejo; AI visuals/audio analyse karke post banayega. Posting manually hogi.",
        "platform_snapchat": "👻 Snapchat selected. Ab video bhejo; AI visuals/audio analyse karke Spotlight caption banayega. Posting manually hogi.",
        "comments": ("💬 YouTube Auto Comment Replies\n\nConnected channels ke comments "
            "har 2 minute mein check honge. AI relevant replies post karega; spam/links "
            "skip honge. Comment/reply text Google Sheets mein save nahi hota."),
        "seo_analytics": "📊 Channel SEO & Analytics — connected channel select karo.",
        "settings": ("⚙️ Settings\n\nTumhari registration aur default settings Google "
            "Sheets mein save hoti hain. Per-user reply mode aur polling controls abhi "
            "implement nahi hue hain."),
    }
    return messages.get(data, "Unknown option. /menu bhejkar dobara try karo.")


def process_update(update):
    if "callback_query" in update:
        query = update.get("callback_query") or {}
        data = query.get("data", "")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        tg("answerCallbackQuery", {"callback_query_id": query.get("id", "")})
        if chat_id and chat.get("type") == "private":
            if data == "menu":
                tg("editMessageText", {"chat_id": chat_id,
                    "message_id": message.get("message_id"),
                    "text": main_menu_text((query.get("from") or {}).get("first_name", "")),
                    "reply_markup": main_menu_keyboard()})

            elif data == "my_accounts":
                try:
                    accounts = list_youtube_connections(query.get("from", {}).get("id"))
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": format_youtube_accounts(accounts),
                        "reply_markup": {"inline_keyboard": [
                            [{"text": "🔗 Connect YouTube", "callback_data": "platform_youtube"}],
                            [{"text": "⬆️ Upload Video", "callback_data": "upload_video"}],
                            [{"text": "⬅️ Main Menu", "callback_data": "menu"}]]}})
                except Exception:
                    log.exception("Could not load connected accounts")
                    send_tg(chat_id, "❌ Accounts load nahi ho paaye. Google Sheet connection check karo.")
            elif data == "seo_analytics":
                try:
                    accounts = list_youtube_connections(query.get("from", {}).get("id"))
                    rows = [[{"text": "📊 " + (a.get("account_name") or "YouTube channel")[:32],
                              "callback_data": "seo:" + a["channel_id"]}]
                            for a in accounts if a.get("channel_id")]
                    rows.append([{"text": "⬅️ Main Menu", "callback_data": "menu"}])
                    if not rows or len(rows) == 1:
                        send_tg(chat_id, "Pehle YouTube account connect karo.")
                    else:
                        tg("sendMessage", {"chat_id": chat_id,
                            "text": "SEO report ke liye apna channel select karo:",
                            "reply_markup": {"inline_keyboard": rows}})
                except Exception:
                    log.exception("SEO channel list failed")
                    send_tg(chat_id, "❌ Channel list load nahi hui.")
            elif data.startswith("seofix:"):
                parts = data.split(":", 2)
                if len(parts) != 3:
                    send_tg(chat_id, "Invalid SEO fix request.")
                else:
                    channel_id, video_id = parts[1], parts[2]
                    user_id = str((query.get("from") or {}).get("id"))
                    try:
                        accounts = list_youtube_connections(user_id)
                        if not any(a.get("channel_id") == channel_id for a in accounts):
                            raise RuntimeError("Ye channel tumhare account se connected nahi hai.")
                        metadata = seo_fix_preview(user_id, channel_id, video_id)
                        pending = SEO_FIX_PENDING[f"{user_id}:{channel_id}:{video_id}"]
                        preview = (
                            "🔍 SEO FIX PREVIEW\n\n"
                            "Current title:\n" + pending["original"]["title"][:180] +
                            "\n\nProposed title:\n" + metadata["title"][:180] +
                            "\n\nProposed description:\n" + metadata["description"][:900] +
                            "\n\nProposed tags:\n" + ", ".join(metadata["tags"][:15]) +
                            "\n\n⚠️ Abhi YouTube par kuch change nahi hua. Kya is video ko update karna hai?"
                        )
                        tg("sendMessage", {"chat_id": chat_id, "text": preview[:3500],
                            "reply_markup": {"inline_keyboard": [
                                [{"text": "✅ Confirm & Update This Video",
                                  "callback_data": "seoapply:" + channel_id + ":" + video_id}],
                                [{"text": "❌ Cancel", "callback_data": "seocancel:" + channel_id + ":" + video_id}]]}})
                    except Exception as exc:
                        log.exception("SEO fix preview failed")
                        send_tg(chat_id, "❌ SEO preview nahi ban paya: " + str(exc)[:250])
            elif data.startswith("seoapply:"):
                parts = data.split(":", 2)
                if len(parts) == 3:
                    channel_id, video_id = parts[1], parts[2]
                    user_id = str((query.get("from") or {}).get("id"))
                    try:
                        accounts = list_youtube_connections(user_id)
                        if not any(a.get("channel_id") == channel_id for a in accounts):
                            raise RuntimeError("Ye channel tumhare account se connected nahi hai.")
                        metadata = apply_confirmed_seo_fix(user_id, channel_id, video_id)
                        send_tg(chat_id, "✅ Is video ka SEO update ho gaya.\n\nNew title: "
                            + metadata["title"] + "\nTags: " + ", ".join(metadata["tags"][:10]))
                    except Exception as exc:
                        log.exception("Confirmed SEO update failed")
                        send_tg(chat_id, "❌ SEO update nahi hua: " + str(exc)[:250])
            elif data.startswith("seocancel:"):
                parts = data.split(":", 2)
                if len(parts) == 3:
                    key = f"{(query.get('from') or {}).get('id')}:{parts[1]}:{parts[2]}"
                    SEO_FIX_PENDING.pop(key, None)
                    send_tg(chat_id, "❎ Is video ka SEO edit cancel kar diya. Koi change nahi hua.")
            elif data.startswith("seo:"):
                channel_id = data.split(":", 1)[1]
                try:
                    accounts = list_youtube_connections(query.get("from", {}).get("id"))
                    if not any(a.get("channel_id") == channel_id for a in accounts):
                        raise RuntimeError("Ye channel tumhare account se connected nahi hai.")
                    report = build_channel_seo_report(query.get("from", {}).get("id"), channel_id)
                    send_tg(chat_id, format_channel_seo_report(report))
                    send_weak_seo_video_actions(chat_id, channel_id, report["weak_videos"])
                except Exception as exc:
                    log.exception("SEO report failed")
                    send_tg(chat_id, "❌ SEO report nahi ban payi: " + str(exc)[:300])
            elif data in ("platform_instagram", "platform_facebook", "platform_snapchat"):
                platform = data.replace("platform_", "")
                remove_social_media(chat_id)
                SOCIAL_MEDIA[str(chat_id)] = {"platform": platform,
                    "user_id": str((query.get("from") or {}).get("id"))}
                send_tg(chat_id, callback_response(data) + "\n\n20 MB tak ka video bhejo.")
            elif data == "socialyt_no":
                remove_social_media(chat_id)
                send_tg(chat_id, "Theek hai. YouTube upload nahi kiya; temporary video delete kar di.")
            elif data == "socialyt_yes":
                user_id = str((query.get("from") or {}).get("id"))
                item = SOCIAL_MEDIA.get(str(chat_id), {})
                if not item.get("video") or item.get("user_id") != user_id:
                    send_tg(chat_id, "Video session expire ho gaya. Dobara video bhejo.")
                else:
                    accounts = list_youtube_connections(user_id)
                    rows = [[{"text": "▶️ " + (a.get("account_name") or "YouTube channel")[:30],
                              "callback_data": "socialytselect:" + a["channel_id"]}]
                            for a in accounts if a.get("channel_id")]
                    if rows:
                        tg("sendMessage", {"chat_id": chat_id,
                            "text": "YouTube channel select karo:",
                            "reply_markup": {"inline_keyboard": rows}})
                    else:
                        send_tg(chat_id, "Pehle YouTube account connect karo. Video session temporary rahega.")
            elif data.startswith("socialytselect:"):
                channel_id = data.split(":", 1)[1]
                user_id = str((query.get("from") or {}).get("id"))
                try:
                    if not any(a.get("channel_id") == channel_id
                               for a in list_youtube_connections(user_id)):
                        raise RuntimeError("Connected channel nahi mila.")
                    upload_social_video_to_youtube(chat_id, user_id, channel_id)
                except Exception as exc:
                    log.exception("Social-to-YouTube upload failed")
                    send_tg(chat_id, "❌ YouTube upload nahi hua: " + str(exc)[:350])
            elif data == "upload_video":
                try:
                    accounts = list_youtube_connections(query.get("from", {}).get("id"))
                    if not accounts:
                        send_tg(chat_id, "Pehle YouTube account connect karo.")
                    else:
                        tg("sendMessage", {"chat_id": chat_id,
                            "text": "Upload kis YouTube channel par karna hai? Channel select karo:",
                            "reply_markup": youtube_upload_keyboard(accounts)})
                except Exception:
                    log.exception("Could not prepare YouTube upload")
                    send_tg(chat_id, "❌ Connected channels load nahi ho paaye.")
            elif data.startswith("ytselect:"):
                channel_id = data.split(":", 1)[1]
                try:
                    accounts = list_youtube_connections(query.get("from", {}).get("id"))
                    selected = next((x for x in accounts if x.get("channel_id") == channel_id), None)
                    if not selected:
                        raise RuntimeError("Ye channel tumhare Telegram account se connected nahi hai.")
                    item = MEDIA.setdefault(str(chat_id), {})
                    item["user_id"] = str(query.get("from", {}).get("id"))
                    item["selected_channel_id"] = channel_id
                    send_tg(chat_id, "✅ Selected: " + selected.get("account_name", "YouTube channel")
                        + "\nAb video bhejo, phir uska thumbnail photo bhejo.")
                except Exception as exc:
                    send_tg(chat_id, "❌ " + str(exc)[:250])
            elif data == "platform_youtube":
                try:
                    state = make_youtube_oauth_state(query.get("from", {}).get("id"))
                    base = env("RENDER_EXTERNAL_URL").rstrip("/")
                    if not base or not env("YOUTUBE_CLIENT_ID") or not env("YOUTUBE_CLIENT_SECRET"):
                        raise RuntimeError("Google OAuth client settings are missing in Render")
                    connect_url = base + "/oauth/youtube/start?state=" + urllib.parse.quote(state)
                    tg("sendMessage", {"chat_id": chat_id,
                        "text": "▶️ YouTube account connect karne ke liye neeche button dabao. Google par login/permission ke baad channel tumhare Telegram account se link hoga.",
                        "reply_markup": {"inline_keyboard": [
                            [{"text": "🔗 Connect YouTube", "url": connect_url}],
                            [{"text": "⬅️ Main Menu", "callback_data": "menu"}]]}})
                except Exception as exc:
                    log.exception("YouTube OAuth link creation failed")
                    send_tg(chat_id, "❌ YouTube connect setup error: " + str(exc)[:300])
            elif data in ("platform_instagram", "platform_facebook",
                          "platform_snapchat", "comments", "settings"):
                tg("sendMessage", {"chat_id": chat_id, "text": callback_response(data),
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "⬅️ Main Menu", "callback_data": "menu"}]]}})
        return
    message = update.get("message") or {}
    chat, user = message.get("chat") or {}, message.get("from") or {}
    chat_id, user_id = chat.get("id"), user.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id: return
    # Public onboarding: registration is per Telegram user and stored in Google Sheets.
    if text.startswith("/start") or text.startswith("/help"):
        if chat.get("type") != "private":
            send_tg(chat_id, "Please mujhe private chat mein /start bhejo, taaki tumhara account alag register ho sake.")
            return
        try:
            register_user(user_id, chat_id, user.get("username", ""), user.get("first_name", ""))
            tg("sendMessage", {"chat_id": chat_id,
                "text": main_menu_text(user.get("first_name", "")) + "\n\n✅ Telegram account Google Sheets mein registered.",
                "reply_markup": main_menu_keyboard()})
        except Exception:
            log.exception("Google Sheets user registration failed")
            send_tg(chat_id, "❌ Registration abhi complete nahi hua. Bot owner ko Google Sheets credentials aur Sheet ID configure karni hogi.")
        return
    if text.startswith("/menu"):
        tg("sendMessage", {"chat_id": chat_id, "text": main_menu_text(user.get("first_name", "")),
            "reply_markup": main_menu_keyboard()})
        return
    if text.startswith("/drafts"):
        drafts = SOCIAL_DRAFTS.get(str(user_id), [])[-5:][::-1]
        if not drafts:
            send_tg(chat_id, "Abhi koi temporary social draft nahi hai. /draft instagram | topic bhejo.")
        else:
            lines = ["📝 Latest temporary social drafts (Google Sheets mein save nahi):"]
            for index, draft in enumerate(drafts, 1):
                lines.append(
                    f"\n{index}. {draft['platform'].title()} — {draft['topic']}\n"
                    f"{draft['caption']}\n{draft['hashtags']}"
                )
            send_tg(chat_id, "\n".join(lines)[:3900])
        return
    if text.startswith("/draft "):
        try:
            payload = text[len("/draft "):]
            if "|" not in payload:
                raise ValueError("Format: /draft instagram | apna topic")
            platform, topic = payload.split("|", 1)
            draft = build_social_draft(platform, topic)
            drafts = SOCIAL_DRAFTS.setdefault(str(user_id), [])
            drafts.append(draft)
            del drafts[:-20]
            send_tg(chat_id,
                "✅ Draft temporary memory mein rakha hai (Google Sheets mein save nahi hua).\n"
                f"Platform: {draft['platform'].title()}\n\n"
                f"{draft['caption']}\n\n{draft['hashtags']}\n\n"
                "🔒 Password/cookies collect ya save nahi hote. "
                "Copy karke platform app par manually post karo. /drafts se dekho. "
                "Bot restart hone par temporary drafts clear ho sakte hain.")
        except ValueError as exc:
            send_tg(chat_id, "❌ " + str(exc))
        return
    if message.get("video") or message.get("photo") or message.get("document"):
        if chat.get("type") == "private" and str(chat_id) in SOCIAL_MEDIA:
            try:
                if receive_social_video(message, chat_id, user_id):
                    return
            except Exception as exc:
                log.exception("Social video receive failed")
                send_tg(chat_id, "❌ Media receive error: " + str(exc)[:350])
                return
        if chat.get("type") != "private":
            send_tg(chat_id, "Security ke liye upload private Telegram chat mein karo.")
            return
        try:
            receive_media(message, chat_id, user_id)
        except Exception as exc:
            log.exception("Telegram media download failed")
            send_tg(chat_id, "❌ Media receive error: " + str(exc)[:500])
        return
    if not authorized(user_id,chat_id):
        return
    if text.startswith(("/menu", "/help", "/start")):
        tg("sendMessage", {"chat_id": chat_id, "text": main_menu_text(user.get("first_name", "")),
            "reply_markup": main_menu_keyboard()})
        return
    if text.startswith("/status"):
        send_tg(chat_id,"YouTube configured: "+("yes" if env("YOUTUBE_REFRESH_TOKEN") else "no")+
            "\nGemini configured: "+("yes" if env("GEMINI_API_KEY") else "no")+
            "\nAuto replies: "+env("AUTO_REPLY_MODE","automatic"))
        return
    if text.startswith("/comments"):
        try:
            # Manually scan all accessible unanswered comment threads, not only
            # the last 24 hours. Successful owner replies are filtered by YouTube.
            LAST_COMMENT_SCAN.clear()
            COMMENT_PENDING_COUNTS.clear()
            count = scan_all_connected_channels(raise_errors=True)
            pending = sum(COMMENT_PENDING_COUNTS.values())
            send_tg(chat_id, f"✅ Full unanswered-comment scan complete. New replies posted: {count}")
            if pending:
                send_tg(chat_id, f"⏳ {pending} comment(s) could not be replied to because of a temporary AI/YouTube error. They remain pending for the next scan.")
            elif count == 0:
                send_tg(chat_id, "ℹ️ Koi eligible unanswered comment nahi mila.")
        except Exception as exc:
            send_tg(chat_id, "❌ Comment scan/retry failed: " + str(exc)[:500])
        return

@app.get("/")
def home():
    return Response("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Agent</title><style>body{font:16px system-ui;max-width:760px;margin:24px auto;padding:0 16px;background:#101827;color:#f4f7fb}
section{background:#1b2940;padding:18px;border-radius:14px;margin:14px 0}input,textarea,select,button{box-sizing:border-box;width:100%;padding:12px;margin:7px 0;border-radius:8px;border:1px solid #52627b;background:#0e1726;color:white}button{background:#2874f0;border:0;font-weight:700}</style></head><body>
<h2>🎬 YouTube Agent</h2><p>Telegram par video aur apna thumbnail bhejo. AI metadata generate karega; thumbnail AI nahi banayega.</p>
<section><label>Manager API token<input id="token" type="password"></label><form id="upload">
<label>Video<input name="video" type="file" accept="video/*" required></label><label>Your thumbnail<input name="thumbnail" type="file" accept="image/jpeg,image/png,image/gif,image/bmp" required></label>
<button type="submit">AI generate metadata and upload</button></form><pre id="result">Ready</pre></section>
<script>document.getElementById('upload').addEventListener('submit',async e=>{e.preventDefault();const out=document.getElementById('result');out.textContent='Uploading and analysing…';const data=new FormData(e.target);
try{const r=await fetch('/api/videos/upload-ai',{method:'POST',headers:{'X-Manager-Token':document.getElementById('token').value},body:data});const j=await r.json();out.textContent=JSON.stringify(j,null,2);}catch(err){out.textContent=err.message;}});</script></body></html>""",mimetype="text/html")

@app.post("/api/videos/upload-ai")
def upload_ai_api():
    if not env("MANAGER_API_TOKEN") or not hmac.compare_digest(request.headers.get("X-Manager-Token",""),env("MANAGER_API_TOKEN")):
        return jsonify(error="unauthorized"),401
    video, thumbnail = request.files.get("video"), request.files.get("thumbnail")
    if not video or not video.filename or not (video.mimetype or "").startswith("video/"):
        return jsonify(error="video file is required"),400
    if not thumbnail or thumbnail.mimetype not in ("image/jpeg","image/png","image/gif","image/bmp"):
        return jsonify(error="your thumbnail image is required"),400
    paths=[]
    try:
        with tempfile.NamedTemporaryFile(delete=False,suffix=os.path.splitext(video.filename)[1][:10] or ".mp4") as f:
            video.save(f); vp=f.name
        paths.append(vp)
        with tempfile.NamedTemporaryFile(delete=False,suffix=os.path.splitext(thumbnail.filename)[1][:10] or ".jpg") as f:
            thumbnail.save(f); tp=f.name
        paths.append(tp)
        metadata=generate_video_metadata(env("GEMINI_API_KEY"),env("GEMINI_MODEL","gemini-2.5-flash"),vp,video.filename,thumbnail_path=tp)
        body=build_video_metadata(metadata["title"],metadata["description"],json.dumps(metadata["tags"]),"public",None)
        vid=yt().videos().insert(part="snippet,status",body=body,media_body=MediaFileUpload(vp,chunksize=8*1024*1024,resumable=True,mimetype=video.mimetype)).execute()["id"]
        if os.path.getsize(tp)>2*1024*1024: return jsonify(error="Video uploaded but thumbnail exceeds YouTube 2 MB limit",video_id=vid),400
        yt().thumbnails().set(videoId=vid,media_body=MediaFileUpload(tp,resumable=False,mimetype=thumbnail.mimetype)).execute()
        return jsonify(ok=True,video_id=vid,video_url="https://youtu.be/"+vid,metadata=metadata),201
    except Exception as exc:
        log.exception("AI upload failed"); return jsonify(error=str(exc)[:500]),500
    finally:
        for path in paths:
            try: os.remove(path)
            except OSError: pass

@app.get("/healthz")
def health():
    return jsonify(ok=True,service="youtube-agent",storage="memory-only",
        auto_reply=env("AUTO_REPLY_MODE","automatic"),processed_comments=len(DONE))

@app.post("/telegram/webhook")
def telegram_webhook():
    secret=env("TELEGRAM_WEBHOOK_SECRET")
    if not secret or not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token",""),secret):
        return jsonify(error="unauthorized"),401
    update=request.get_json(silent=True) or {}
    if not isinstance(update,dict) or "update_id" not in update: return jsonify(error="invalid update"),400
    update_id = update.get("update_id")
    # Telegram retries when a webhook takes too long. Do not process the same
    # photo/video twice after the first run has already consumed its MEDIA state.
    if update_id in DONE_TELEGRAM_UPDATES:
        return jsonify(ok=True, duplicate=True)
    DONE_TELEGRAM_UPDATES.add(update_id)
    if len(DONE_TELEGRAM_UPDATES) > 5000:
        DONE_TELEGRAM_UPDATES.clear()
        DONE_TELEGRAM_UPDATES.add(update_id)
    try: process_update(update)
    except Exception: log.exception("Telegram update failed")
    return jsonify(ok=True)

@app.get("/oauth/youtube/start")
def youtube_multi_oauth_start():
    state = request.args.get("state", "")
    data = read_youtube_oauth_state(state)
    if not data:
        return "Invalid or expired YouTube connection link. Telegram mein dobara Connect YouTube dabao.", 400
    client_id, client_secret = env("YOUTUBE_CLIENT_ID"), env("YOUTUBE_CLIENT_SECRET")
    base = env("RENDER_EXTERNAL_URL").rstrip("/")
    if not client_id or not client_secret or not base:
        return "Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and RENDER_EXTERNAL_URL in Render.", 400
    params = {
        "client_id": client_id,
        "redirect_uri": base + "/oauth/youtube/callback",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))


@app.get("/oauth/youtube/callback")
def youtube_multi_oauth_callback():
    data = read_youtube_oauth_state(request.args.get("state", ""))
    if not data:
        return "Invalid, expired, or already-used OAuth state. Telegram mein dobara Connect YouTube dabao.", 400
    if request.args.get("error"):
        return "Google authorization cancelled: " + html.escape(request.args["error"]), 400
    base = env("RENDER_EXTERNAL_URL").rstrip("/")
    client_id, client_secret = env("YOUTUBE_CLIENT_ID"), env("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret or not base:
        return "Google OAuth settings missing in Render.", 400
    response = requests.post("https://oauth2.googleapis.com/token", data={
        "code": request.args.get("code", ""),
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": base + "/oauth/youtube/callback",
        "grant_type": "authorization_code",
    }, timeout=30)
    if response.status_code != 200:
        log.error("Per-user YouTube token exchange failed: %s", response.text[:500])
        return "Google token exchange failed. Check OAuth client and redirect URI, then retry.", 400
    token_data = response.json()
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        return "Google did not return a refresh token. Remove this app from Google Account access and connect again.", 400
    credentials = Credentials(
        token=token_data.get("access_token"),
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    try:
        service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        channel_response = service.channels().list(part="id,snippet", mine=True).execute()
        channels = channel_response.get("items", [])
        if not channels:
            return "Is Google account par YouTube channel nahi mila.", 400
        channel = channels[0]
        channel_id = channel["id"]
        channel_name = channel.get("snippet", {}).get("title", "YouTube channel")
        encrypted = encrypt_refresh_token(refresh_token)
        save_youtube_connection(data["user_id"], channel_name, channel_id, encrypted)
    except Exception:
        log.exception("Saving per-user YouTube connection failed")
        return "YouTube channel verify/save nahi ho paya. Render logs aur Google Sheet permissions check karo.", 500

    try:
        tg("sendMessage", {"chat_id": data["user_id"],
            "text": "✅ YouTube successfully connect ho gaya!\n\nChannel: " + telegram_html(channel_name)
                + "\n\nAb My Accounts button se channel dekh sakte ho.",
            "reply_markup": {"inline_keyboard": [
                [{"text": "👤 My Accounts", "callback_data": "my_accounts"}],
                [{"text": "⬅️ Main Menu", "callback_data": "menu"}]]}})
    except Exception:
        log.exception("Telegram connection confirmation could not be sent")
    return ("<h2>YouTube connected successfully!</h2><p>Channel: "
        + html.escape(channel_name) + "</p><p>Telegram bot mein confirmation bhej diya gaya. "
        "My Accounts button se channel dekh sakte hain.</p><p>Upload/comment workflows ko per-user token par switch karna abhi baaki hai.</p>")


@app.get("/oauth/start")
def oauth_start():
    client_id=env("YOUTUBE_CLIENT_ID"); base=env("RENDER_EXTERNAL_URL").rstrip("/")
    if not client_id or not env("YOUTUBE_CLIENT_SECRET") or not base:
        return "Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and RENDER_EXTERNAL_URL first.",400
    state=secrets.token_urlsafe(24); OAUTH_STATES.add(state)
    params={"client_id":client_id,"redirect_uri":base+"/oauth/callback","response_type":"code",
        "scope":" ".join(SCOPES),"access_type":"offline","prompt":"consent","state":state}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?"+urllib.parse.urlencode(params))

@app.get("/oauth/callback")
def oauth_callback():
    state=request.args.get("state","")
    if state not in OAUTH_STATES: return "Invalid/expired OAuth state. Start again from /oauth/start.",400
    OAUTH_STATES.discard(state)
    if request.args.get("error"): return "Google authorization cancelled: "+request.args["error"],400
    r=requests.post("https://oauth2.googleapis.com/token",data={"code":request.args.get("code",""),
        "client_id":env("YOUTUBE_CLIENT_ID"),"client_secret":env("YOUTUBE_CLIENT_SECRET"),
        "redirect_uri":env("RENDER_EXTERNAL_URL").rstrip("/")+"/oauth/callback",
        "grant_type":"authorization_code"},timeout=30)
    r.raise_for_status(); token=r.json().get("refresh_token")
    if not token: return "Google did not return a refresh token. Revoke app access and authorize again.",400
    return ("<h2>YouTube authorization successful</h2><p>Copy refresh token into Render as "
        "<b>YOUTUBE_REFRESH_TOKEN</b>. Never share it.</p><textarea style='width:100%;height:120px'>"+html.escape(token)+"</textarea>")

@app.post("/admin/set-webhook")
def set_webhook():
    if not env("MANAGER_API_TOKEN") or not hmac.compare_digest(request.headers.get("X-Manager-Token",""),env("MANAGER_API_TOKEN")):
        return jsonify(error="unauthorized"),401
    base=env("RENDER_EXTERNAL_URL").rstrip("/")
    result=tg("setWebhook",{"url":base+"/telegram/webhook","secret_token":env("TELEGRAM_WEBHOOK_SECRET"),
        "allowed_updates":["message","callback_query"],"drop_pending_updates":False})
    return jsonify(ok=True,result=result)

@app.errorhandler(Exception)
def errors(exc):
    if isinstance(exc,HTTPException): return exc
    log.exception("Unhandled request error"); return jsonify(error="internal_error"),500

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(env("PORT","10000")))
