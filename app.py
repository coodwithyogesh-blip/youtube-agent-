import os, hmac, json, logging, secrets, urllib.parse, html, tempfile
from datetime import datetime, timezone
from flask import Flask, request, jsonify, redirect, Response
from werkzeug.exceptions import HTTPException
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from upload_utils import parse_publish_at, build_video_metadata

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("youtube-agent")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "90")) * 1024 * 1024
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.upload"]
# Volatile memory only: no SQLite, no Supabase, no local database.
PENDING = {}
DONE = set()
OAUTH_STATES = set()
TELEGRAM_API = "https://api.telegram.org/bot"

def telegram_html(value):
    return html.escape(str(value or ""), quote=False)

def comment_has_owner_reply(thread, channel_id):
    replies = (thread.get("replies") or {}).get("comments", [])
    return any(((reply.get("snippet") or {}).get("authorChannelId") or {}).get("value") == channel_id for reply in replies)

def env(name, default=""):
    return os.getenv(name, default).strip()

def tg(method, payload):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
    r = requests.post(f"{TELEGRAM_API}{token}/{method}", json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram API error"))
    return data.get("result")

def yt():
    required = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"]
    missing = [k for k in required if not env(k)]
    if missing:
        raise RuntimeError("Missing YouTube settings: " + ", ".join(missing))
    creds = Credentials(
        None, refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"), scopes=SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def ai_reply(comment, video_title=""):
    key = env("GEMINI_API_KEY")
    if not key:
        return "Thanks for watching! We appreciate your comment."
    model = env("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    prompt = ("Write one short, natural, respectful YouTube creator reply. "
              "Do not invent facts, promise anything, request personal data, or include links. "
              "If the comment needs sensitive, medical, legal, financial, or private help, "
              "reply politely that the creator cannot advise on that.\n"
              f"Video: {video_title}\nComment: {comment}")
    r = requests.post(url, json={"contents":[{"parts":[{"text":prompt}]}],
                                 "generationConfig":{"temperature":0.5,"maxOutputTokens":180}},
                     timeout=35)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()[:1000]

def authorized(user_id, chat_id):
    owner = env("TELEGRAM_OWNER_ID")
    group = env("TELEGRAM_GROUP_ID")
    return bool(owner and str(user_id) == owner and (not group or str(chat_id) == group))

def send_comment_card(chat_id, comment_id, video_title, author, comment, draft):
    text = (f"💬 <b>YouTube comment for approval</b>\n"
            f"<b>Video:</b> {telegram_html(video_title[:150])}\n<b>From:</b> {telegram_html(author[:100])}\n"
            f"<b>Comment:</b> {telegram_html(comment[:700])}\n\n<b>AI draft:</b> {telegram_html(draft[:900])}\n"
            f"<code>{telegram_html(comment_id)}</code>\n\nReply will publish only after your approval.")
    keyboard = {"inline_keyboard":[[
        {"text":"✅ Approve & reply","callback_data":"approve:"+comment_id},
        {"text":"⏭ Skip","callback_data":"skip:"+comment_id}
    ]]}
    tg("sendMessage", {"chat_id":chat_id,"text":text,"parse_mode":"HTML",
                       "reply_markup":keyboard,"disable_web_page_preview":True})

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
        sn = item["snippet"]
        video_id = sn["resourceId"]["videoId"]
        title = sn.get("title", "YouTube video")
        try:
            threads = service.commentThreads().list(part="snippet,replies", videoId=video_id,
                        maxResults=50, order="time", textFormat="plainText").execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) in (403, 404):
                continue
            raise
        for thread in threads.get("items", []):
            if comment_has_owner_reply(thread, channel_id):
                continue
            top = thread["snippet"]["topLevelComment"]
            c = top["snippet"]
            results.append({"id":top["id"],"video_id":video_id,"title":title,
                            "author":c.get("authorDisplayName","Viewer"),
                            "text":c.get("textOriginal",c.get("textDisplay",""))})
    return results

def scan_and_send(chat_id):
    comments = list_recent_comments()
    sent = 0
    for c in comments:
        cid = c["id"]
        if cid in PENDING or cid in DONE:
            continue
        draft = ai_reply(c["text"], c["title"])
        PENDING[cid] = {"draft":draft,"video_id":c["video_id"],"created":datetime.now(timezone.utc).isoformat()}
        send_comment_card(chat_id,cid,c["title"],c["author"],c["text"],draft)
        sent += 1
    return sent

def handle_callback(q):
    user = (q.get("from") or {}).get("id")
    msg = q.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    data = q.get("data","")
    if not authorized(user, chat_id):
        tg("answerCallbackQuery",{"callback_query_id":q["id"],"text":"Owner access required","show_alert":True})
        return
    action, sep, cid = data.partition(":")
    if not sep or action not in ("approve","skip") or cid not in PENDING:
        tg("answerCallbackQuery",{"callback_query_id":q["id"],"text":"This approval expired. Run /comments again.","show_alert":True})
        return
    try:
        if action == "approve":
            draft = PENDING[cid]["draft"]
            yt().comments().insert(part="snippet",body={"snippet":{"parentId":cid,"textOriginal":draft}}).execute()
            tg("editMessageText",{"chat_id":chat_id,"message_id":msg["message_id"],
                "text":"✅ <b>Approved and published</b>\n"+draft,"parse_mode":"HTML"})
        else:
            tg("editMessageText",{"chat_id":chat_id,"message_id":msg["message_id"],
                "text":"⏭ <b>Skipped</b> — no reply was published.","parse_mode":"HTML"})
        PENDING.pop(cid,None)
        DONE.add(cid)
        tg("answerCallbackQuery",{"callback_query_id":q["id"],"text":"Done"})
    except Exception as exc:
        log.exception("Comment action failed")
        tg("answerCallbackQuery",{"callback_query_id":q["id"],"text":str(exc)[:180],"show_alert":True})

def process_update(update):
    if "callback_query" in update:
        handle_callback(update["callback_query"]); return
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    chat_id, user_id = chat.get("id"), user.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    if text.startswith("/start") or text.startswith("/help"):
        tg("sendMessage",{"chat_id":chat_id,"text":
            "🎬 <b>YouTube Agent</b>\n/comments — fetch recent comments and create AI reply drafts.\n"
            "Replies are NEVER posted automatically: owner must press Approve.\n"
            "/status — connection status.",
            "parse_mode":"HTML"})
        return
    if text.startswith("/status"):
        tg("sendMessage",{"chat_id":chat_id,"text":
            "YouTube configured: "+("yes" if all(env(k) for k in ("YOUTUBE_CLIENT_ID","YOUTUBE_CLIENT_SECRET","YOUTUBE_REFRESH_TOKEN")) else "no")+
            "\nGemini configured: "+("yes" if env("GEMINI_API_KEY") else "no")+
            "\nPending approvals in memory: "+str(len(PENDING))})
        return
    if text.startswith("/comments"):
        if not authorized(user_id,chat_id):
            tg("sendMessage",{"chat_id":chat_id,"text":"⛔ Owner-only command."}); return
        tg("sendMessage",{"chat_id":chat_id,"text":"Checking recent YouTube comments…"})
        count = scan_and_send(chat_id)
        tg("sendMessage",{"chat_id":chat_id,"text":f"Done. {count} new comment(s) need approval."})
        return
    if text.startswith("/reply"):
        tg("sendMessage",{"chat_id":chat_id,"text":"Use the Approve button on an AI draft. Manual reply workflow will be added in the next version."})

@app.get("/")
def home():
    return Response("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>YouTube Agent</title><style>body{font:16px system-ui;max-width:760px;margin:24px auto;padding:0 16px;background:#101827;color:#f4f7fb}
    section{background:#1b2940;padding:18px;border-radius:14px;margin:14px 0}input,textarea,select,button{box-sizing:border-box;width:100%;padding:12px;margin:7px 0;border-radius:8px;border:1px solid #52627b;background:#0e1726;color:white}
    button{background:#2874f0;border:0;font-weight:700}small{color:#b7c3d6}</style></head><body>
    <h2>🎬 YouTube Agent</h2><p>Upload a video, optional custom thumbnail, or schedule a public release.</p>
    <section><label>Manager API token<input id="token" type="password" autocomplete="off"></label>
    <form id="upload"><label>Video file<input name="video" type="file" accept="video/*" required></label>
    <label>Thumbnail (optional)<input name="thumbnail" type="file" accept="image/jpeg,image/png,image/gif,image/bmp"></label>
    <label>Title<input name="title" maxlength="100" required></label>
    <label>Description<textarea name="description" rows="5"></textarea></label>
    <label>Tags (comma separated)<input name="tags" placeholder="gaming, free fire"></label>
    <label>Visibility<select name="privacy"><option value="public">Public</option><option value="unlisted">Unlisted</option><option value="private">Private</option></select></label>
    <label>Schedule publish time (India time; optional)<input name="publish_at" type="datetime-local"></label>
    <button type="submit">Upload to YouTube</button></form><pre id="result">Ready</pre>
    <small>Scheduled videos remain private until YouTube publishes them at the selected time. API token is sent only to this service.</small></section>
    <script>document.getElementById('upload').addEventListener('submit',async e=>{e.preventDefault();const out=document.getElementById('result');out.textContent='Uploading… keep this page open.';
    const data=new FormData(e.target);try{const r=await fetch('/api/videos/upload',{method:'POST',headers:{'X-Manager-Token':document.getElementById('token').value},body:data});
    const j=await r.json();out.textContent=JSON.stringify(j,null,2);if(r.ok)e.target.reset();}catch(err){out.textContent=err.message;}});</script></body></html>""", mimetype="text/html")

@app.post("/api/videos/upload")
def upload_video_api():
    if not env("MANAGER_API_TOKEN") or not hmac.compare_digest(
        request.headers.get("X-Manager-Token", ""), env("MANAGER_API_TOKEN")):
        return jsonify(error="unauthorized"), 401
    video = request.files.get("video")
    thumbnail = request.files.get("thumbnail")
    if not video or not video.filename:
        return jsonify(error="video file is required"), 400
    if not (video.mimetype or "").startswith("video/"):
        return jsonify(error="selected file must be a video"), 400
    if thumbnail and thumbnail.filename and thumbnail.mimetype not in (
        "image/jpeg", "image/png", "image/gif", "image/bmp"):
        return jsonify(error="thumbnail must be JPEG, PNG, GIF, or BMP"), 400
    try:
        publish_at = parse_publish_at(request.form.get("publish_at", ""))
        body = build_video_metadata(
            request.form.get("title"), request.form.get("description", ""),
            request.form.get("tags", ""), request.form.get("privacy", "public"), publish_at)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    temp_paths = []
    try:
        suffix = os.path.splitext(video.filename)[1][:12] or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            video.save(tmp); video_path = tmp.name
        temp_paths.append(video_path)
        media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024,
                                resumable=True, mimetype=video.mimetype or "application/octet-stream")
        result = yt().videos().insert(part="snippet,status", body=body, media_body=media).execute()
        video_id = result["id"]
        thumbnail_warning = None
        if thumbnail and thumbnail.filename:
            thumb_suffix = os.path.splitext(thumbnail.filename)[1][:12] or ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=thumb_suffix) as tmp:
                thumbnail.save(tmp); thumb_path = tmp.name
            temp_paths.append(thumb_path)
            if os.path.getsize(thumb_path) > 2 * 1024 * 1024:
                thumbnail_warning = "Video uploaded, but thumbnail exceeds YouTube's 2 MB limit."
            else:
                try:
                    yt().thumbnails().set(videoId=video_id,
                        media_body=MediaFileUpload(thumb_path, resumable=False,
                                                  mimetype=thumbnail.mimetype or "image/jpeg")).execute()
                except Exception as exc:
                    log.exception("Thumbnail upload failed for %s", video_id)
                    thumbnail_warning = "Video uploaded, but thumbnail failed: " + str(exc)[:250]
        return jsonify(ok=True, video_id=video_id,
            video_url="https://youtu.be/" + video_id,
            scheduled_publish_at=publish_at,
            thumbnail_warning=thumbnail_warning), 201
    except Exception as exc:
        log.exception("YouTube video upload failed")
        return jsonify(error=str(exc)[:500]), 500
    finally:
        for path in temp_paths:
            try: os.remove(path)
            except OSError: pass

@app.get("/healthz")
def health():
    return jsonify(ok=True, service="youtube-agent", storage="memory-only",
                   pending_approvals=len(PENDING))

@app.post("/telegram/webhook")
def telegram_webhook():
    secret = env("TELEGRAM_WEBHOOK_SECRET")
    if not secret or not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token",""), secret):
        return jsonify(error="unauthorized"), 401
    update = request.get_json(silent=True) or {}
    if not isinstance(update, dict) or "update_id" not in update:
        return jsonify(error="invalid update"), 400
    try:
        process_update(update)
    except Exception:
        log.exception("Telegram update failed")
    return jsonify(ok=True)

@app.get("/oauth/start")
def oauth_start():
    client_id, redirect_uri = env("YOUTUBE_CLIENT_ID"), env("RENDER_EXTERNAL_URL").rstrip("/")+"/oauth/callback"
    if not client_id or not env("YOUTUBE_CLIENT_SECRET") or not env("RENDER_EXTERNAL_URL"):
        return "Set YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and RENDER_EXTERNAL_URL first.", 400
    state = secrets.token_urlsafe(24); OAUTH_STATES.add(state)
    params = {"client_id":client_id,"redirect_uri":redirect_uri,"response_type":"code",
              "scope":" ".join(SCOPES),"access_type":"offline","prompt":"consent","state":state}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?"+urllib.parse.urlencode(params))

@app.get("/oauth/callback")
def oauth_callback():
    state = request.args.get("state","")
    if state not in OAUTH_STATES:
        return "Invalid/expired OAuth state. Start again from /oauth/start.", 400
    OAUTH_STATES.discard(state)
    if request.args.get("error"):
        return "Google authorization cancelled: "+request.args["error"], 400
    r = requests.post("https://oauth2.googleapis.com/token",data={
        "code":request.args.get("code",""),"client_id":env("YOUTUBE_CLIENT_ID"),
        "client_secret":env("YOUTUBE_CLIENT_SECRET"),
        "redirect_uri":env("RENDER_EXTERNAL_URL").rstrip("/")+"/oauth/callback",
        "grant_type":"authorization_code"},timeout=30)
    r.raise_for_status(); token = r.json().get("refresh_token")
    if not token:
        return "Google did not return a refresh token. Revoke app access and authorize again with prompt=consent.", 400
    return ("<h2>YouTube authorization successful</h2><p>Copy this refresh token into Render as "
            "<b>YOUTUBE_REFRESH_TOKEN</b>, then delete this browser page/history.</p><textarea "
            "style='width:100%;height:120px'>"+token+"</textarea><p>Never share this token.</p>")

@app.post("/admin/set-webhook")
def set_webhook():
    if not env("MANAGER_API_TOKEN") or not hmac.compare_digest(
        request.headers.get("X-Manager-Token",""),env("MANAGER_API_TOKEN")):
        return jsonify(error="unauthorized"),401
    base=env("RENDER_EXTERNAL_URL").rstrip("/")
    result=tg("setWebhook",{"url":base+"/telegram/webhook",
        "secret_token":env("TELEGRAM_WEBHOOK_SECRET"),
        "allowed_updates":["message","callback_query"],"drop_pending_updates":False})
    return jsonify(ok=True,result=result)

@app.errorhandler(Exception)
def errors(exc):
    if isinstance(exc, HTTPException):
        return exc
    log.exception("Unhandled request error")
    return jsonify(error="internal_error"),500

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(env("PORT","10000")))
