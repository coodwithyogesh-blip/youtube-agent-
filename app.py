import os, hmac, json, logging, secrets, urllib.parse, html, tempfile, threading, time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect, Response
from werkzeug.exceptions import HTTPException
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from upload_utils import parse_publish_at, build_video_metadata
from ai_utils import generate_video_metadata, should_auto_reply, generate_content_with_fallback
from sheets_store import register_user

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("youtube-agent")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "90")) * 1024 * 1024
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.upload"]
DONE = set()
OAUTH_STATES = set()
MEDIA = {}
AUTO_THREAD_STARTED = False
TELEGRAM_API = "https://api.telegram.org/bot"

def env(name, default=""):
    return os.getenv(name, default).strip()

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
    fallback_model = env("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash")
    prompt = ("Write one short, natural, respectful YouTube creator reply. Do not invent facts, "
        "promise anything, request personal data, or include links. For medical, legal, financial "
        "or private requests, politely say the creator cannot advise.\n"
        f"Video: {video_title}\nComment: {comment}")
    data = generate_content_with_fallback(
        key, model,
        {"contents":[{"parts":[{"text":prompt}]}],
         "generationConfig":{"temperature":0.4,"maxOutputTokens":160}},
        timeout=35, fallback_model=fallback_model)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()[:900]

def authorized(user_id, chat_id):
    owner, group = env("TELEGRAM_OWNER_ID"), env("TELEGRAM_GROUP_ID")
    return bool(owner and str(user_id) == owner and (not group or str(chat_id) == group))

def telegram_download(file_id, suffix):
    info = tg("getFile", {"file_id":file_id})
    path = info.get("file_path","")
    size = int(info.get("file_size",0))
    if not path or size > 20 * 1024 * 1024:
        raise RuntimeError("Telegram Bot API download limit: file must be 20 MB or smaller")
    token = env("TELEGRAM_BOT_TOKEN")
    response = requests.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=90)
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(response.content)
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
        send_tg(chat_id, "🎬 Video aur thumbnail mil gaye. AI video analyse karke title, description, tags aur hashtags bana raha hai…")
        metadata = generate_video_metadata(env("GEMINI_API_KEY"),
            env("GEMINI_MODEL","gemini-3.8-flash"), item["video"], item.get("filename","video.mp4"),
            item.get("caption",""))
        body = build_video_metadata(metadata["title"], metadata["description"],
                                    json.dumps(metadata["tags"]), "public", None)
        media = MediaFileUpload(item["video"], chunksize=8*1024*1024,
                                resumable=True, mimetype=item.get("mime","video/mp4"))
        result = yt().videos().insert(part="snippet,status", body=body, media_body=media).execute()
        video_id = result["id"]
        if os.path.getsize(item["thumbnail"]) > 2*1024*1024:
            raise RuntimeError("Video upload ho gaya, lekin thumbnail 2 MB se bada hai. YouTube limit ke andar thumbnail bhejo.")
        yt().thumbnails().set(videoId=video_id,
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

def receive_media(message, chat_id):
    key = str(chat_id)
    item = MEDIA.setdefault(key,{})
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
        upload_telegram_pair(chat_id)
    return True

def comment_has_owner_reply(thread, channel_id):
    replies = (thread.get("replies") or {}).get("comments", [])
    return any(((reply.get("snippet") or {}).get("authorChannelId") or {}).get("value") == channel_id
               for reply in replies)

def list_recent_comments():
    service = yt()
    channel = service.channels().list(part="id,contentDetails", mine=True).execute()
    items = channel.get("items", [])
    if not items:
        raise RuntimeError("No YouTube channel found for this OAuth account")
    channel_id = items[0]["id"]
    if env("YOUTUBE_CHANNEL_ID") and channel_id != env("YOUTUBE_CHANNEL_ID"):
        raise RuntimeError("Connected YouTube channel does not match YOUTUBE_CHANNEL_ID")
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    playlist = service.playlistItems().list(part="snippet", playlistId=uploads, maxResults=10).execute()
    results = []
    for item in playlist.get("items", []):
        sn = item["snippet"]; video_id = sn["resourceId"]["videoId"]
        try:
            threads = service.commentThreads().list(part="snippet,replies", videoId=video_id,
                maxResults=50, order="time", textFormat="plainText").execute()
        except HttpError as exc:
            if getattr(exc.resp,"status",None) in (403,404): continue
            raise
        for thread in threads.get("items", []):
            if comment_has_owner_reply(thread, channel_id): continue
            top = thread["snippet"]["topLevelComment"]; c = top["snippet"]
            results.append({"id":top["id"],"video_id":video_id,"title":sn.get("title",""),
                            "text":c.get("textOriginal",c.get("textDisplay",""))})
    return results

def auto_reply_scan():
    count = 0
    for comment in list_recent_comments():
        cid = comment["id"]
        if cid in DONE or not should_auto_reply(comment["text"]):
            continue
        reply = ai_reply(comment["text"], comment["title"])
        yt().comments().insert(part="snippet",
            body={"snippet":{"parentId":cid,"textOriginal":reply}}).execute()
        DONE.add(cid); count += 1
        log.info("Auto-replied to comment %s", cid)
    return count

def auto_comment_worker():
    interval = max(300, int(env("COMMENT_POLL_SECONDS","900")))
    while True:
        try:
            if env("AUTO_REPLY_MODE","automatic").lower() == "automatic" and env("YOUTUBE_REFRESH_TOKEN"):
                count = auto_reply_scan()
                if count: log.info("Automatic replies posted: %s",count)
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

def main_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "▶️ YouTube", "callback_data": "platform_youtube"},
         {"text": "📸 Instagram", "callback_data": "platform_instagram"}],
        [{"text": "📘 Facebook", "callback_data": "platform_facebook"},
         {"text": "👻 Snapchat", "callback_data": "platform_snapchat"}],
        [{"text": "💬 Comment Replies", "callback_data": "comments"},
         {"text": "⚙️ Settings", "callback_data": "settings"}],
    ]}


def main_menu_text(first_name=""):
    greeting = f" {first_name}" if first_name else ""
    return ("👋 Welcome" + greeting + " to Social Media Agent!\n\n"
            "Apna platform choose karo. Account connections aur per-user automation "
            "abhi development mein hain; koi account bina authorization access nahi hoga.")


def callback_response(data):
    messages = {
        "platform_youtube": ("▶️ YouTube\n\nAbhi existing YouTube upload feature owner ke "
            "configured channel par kaam karta hai. Har user ka alag YouTube OAuth connection "
            "abhi ready nahi hai."),
        "platform_instagram": ("📸 Instagram\n\nInstagram account connection abhi available "
            "nahi hai. Secure official login/API integration next step mein banegi."),
        "platform_facebook": ("📘 Facebook\n\nFacebook account connection abhi available "
            "nahi hai. Secure official login/API integration next step mein banegi."),
        "platform_snapchat": ("👻 Snapchat\n\nSnapchat account connection abhi available "
            "nahi hai. Platform access/approval requirements check karne honge."),
        "comments": ("💬 Comment Replies\n\nAI/Manual reply selection abhi per-user "
            "workflow mein active nahi hai. Current automatic YouTube replies legacy owner "
            "configuration use karte hain."),
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
            elif data in ("platform_youtube", "platform_instagram", "platform_facebook",
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
    if not authorized(user_id,chat_id):
        if message.get("video") or message.get("photo") or message.get("document"):
            send_tg(chat_id,"⛔ Owner-only bot. Telegram owner ID / group ID check failed.")
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
        try: send_tg(chat_id,f"✅ Auto replies posted: {auto_reply_scan()}")
        except Exception as exc: send_tg(chat_id,"❌ "+str(exc)[:500])
        return
    if message.get("video") or message.get("photo") or message.get("document"):
        try: receive_media(message,chat_id)
        except Exception as exc:
            log.exception("Telegram media download failed")
            send_tg(chat_id,"❌ Media receive error: "+str(exc)[:500])

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
        metadata=generate_video_metadata(env("GEMINI_API_KEY"),env("GEMINI_MODEL","gemini-2.5-flash"),vp,video.filename)
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
    try: process_update(update)
    except Exception: log.exception("Telegram update failed")
    return jsonify(ok=True)

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
