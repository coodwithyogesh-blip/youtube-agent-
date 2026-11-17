import json
import re
import requests

def clean_generated_metadata(raw):
    if not isinstance(raw, dict):
        raise ValueError("AI metadata must be a JSON object")
    title = str(raw.get("title") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not title:
        raise ValueError("AI did not generate a video title")
    if len(title) > 100:
        title = title[:97].rstrip() + "..."
    hashtags = []
    for value in raw.get("hashtags") or []:
        tag = re.sub(r"[^A-Za-z0-9_]", "", str(value).lstrip("#"))
        if tag and tag.lower() not in {x.lower() for x in hashtags}:
            hashtags.append("#" + tag)
    hashtags = hashtags[:15]
    if hashtags:
        description = (description[:4800].rstrip() + "\n\n" + " ".join(hashtags)).strip()
    tags = []
    for value in raw.get("tags") or []:
        tag = str(value).strip()[:500]
        if tag and tag.lower() not in {x.lower() for x in tags}:
            tags.append(tag)
    return {"title": title, "description": description[:5000], "tags": tags[:500],
            "hashtags": hashtags}

def generate_video_metadata(api_key, model, video_path, filename, caption=""):
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for automatic video metadata")
    endpoint = "https://generativelanguage.googleapis.com/upload/v1beta/files"
    with open(video_path, "rb") as stream:
        response = requests.post(
            endpoint, params={"key": api_key},
            headers={"X-Goog-Upload-Protocol": "multipart"},
            data={"metadata": json.dumps({"file": {"display_name": filename[:100]}})},
            files={"file": (filename, stream, "video/mp4")}, timeout=180)
    response.raise_for_status()
    file_info = response.json().get("file", {})
    file_uri, file_name = file_info.get("uri"), file_info.get("name")
    if not file_uri or not file_name:
        raise RuntimeError("Gemini did not return a usable video file reference")
    try:
        for _ in range(30):
            status = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/" + file_name,
                params={"key": api_key}, timeout=20)
            status.raise_for_status()
            state = status.json()
            if state.get("state") == "ACTIVE":
                break
            if state.get("state") == "FAILED":
                raise RuntimeError("Gemini could not process this video")
            import time
            time.sleep(2)
        else:
            raise RuntimeError("Gemini video processing timed out")
        prompt = (
            "Analyze the actual video content and create YouTube SEO metadata. "
            "Return ONLY valid JSON with keys title, description, tags, hashtags. "
            "Title must be accurate, engaging, and <=100 characters. Description should "
            "summarize only visible/audible content, include a natural call to action, "
            "and not invent names, events, links, or claims. Provide 10-20 relevant tags "
            "and 3-8 relevant hashtags without keyword stuffing. Use the video's language "
            "when clear; otherwise use English. Never claim anything not shown. "
            f"Uploader note/Telegram caption: {caption[:1000]}"
        )
        generated = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json={"contents":[{"parts":[{"text":prompt},
                    {"file_data":{"mime_type":file_info.get("mimeType","video/mp4"),
                                  "file_uri":file_uri}}]}],
                  "generationConfig":{"temperature":0.35,"responseMimeType":"application/json"}},
            timeout=120)
        generated.raise_for_status()
        text = generated.json()["candidates"][0]["content"]["parts"][0]["text"]
        return clean_generated_metadata(json.loads(text))
    finally:
        try:
            requests.delete("https://generativelanguage.googleapis.com/v1beta/" + file_name,
                            params={"key": api_key}, timeout=15)
        except Exception:
            pass

def should_auto_reply(comment):
    text = (comment or "").strip().lower()
    if not text or len(text) > 3000:
        return False
    if "http://" in text or "https://" in text or "www." in text:
        return False
    blocked = ("buy followers", "free subscribers", "crypto investment",
               "send me your password", "share your phone number",
               "share your address", "whatsapp me", "telegram me")
    return not any(term in text for term in blocked)
