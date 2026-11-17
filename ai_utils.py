import json
import os
import re
import time
import requests

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def generate_content_with_fallback(api_key, model, payload, timeout=60,
                                  fallback_model=None, max_attempts=3):
    """Call Gemini GenerateContent with bounded retries and one model fallback.

    Transient HTTP/network failures use exponential backoff. A fallback model is
    tried only after the primary model is unavailable or not found. Error text
    intentionally omits request URLs and API keys.
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required")
    models = []
    for candidate in (model, fallback_model):
        name = str(candidate or "").strip()
        if name and name not in models:
            models.append(name)
    if not models:
        raise RuntimeError("GEMINI_MODEL is not configured")

    last_status = None
    last_network_error = None
    for model_index, model_name in enumerate(models):
        for attempt in range(max(1, int(max_attempts))):
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model_name}:generateContent")
            try:
                response = requests.post(url, params={"key": api_key},
                                         json=payload, timeout=timeout)
                status = response.status_code
                if status >= 400:
                    last_status = status
                    if status not in RETRYABLE_STATUS_CODES and status != 404:
                        raise RuntimeError(
                            f"Gemini API request failed with HTTP {status}. "
                            "Check API key permissions, quota, and request settings."
                        )
                    if attempt + 1 < max_attempts and status in RETRYABLE_STATUS_CODES:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    break
                return response.json()
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                if status is not None:
                    last_status = status
                    if status not in RETRYABLE_STATUS_CODES and status != 404:
                        raise RuntimeError(
                            f"Gemini API request failed with HTTP {status}. "
                            "Check API key permissions, quota, and request settings."
                        ) from None
                else:
                    last_network_error = type(exc).__name__
                if attempt + 1 < max_attempts and (status is None or status in RETRYABLE_STATUS_CODES):
                    time.sleep(min(2 ** attempt, 8))
                    continue
                break
        # Try the fallback only after transient failure or model-not-found.
        if model_index + 1 < len(models):
            continue

    if last_status is not None:
        raise RuntimeError(
            f"Gemini primary and fallback models are unavailable (HTTP {last_status}). "
            "Wait a little and retry; check Google AI Studio status and model access."
        )
    raise RuntimeError(
        "Gemini request could not connect after retries"
        + (f" ({last_network_error})" if last_network_error else "")
        + ". Check Render network/logs and try again."
    )

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
            files={"metadata": ("metadata", json.dumps({"file": {"display_name": filename[:100]}}), "application/json"),
                   "file": (filename, stream, "video/mp4")}, timeout=180)
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
        generated = generate_content_with_fallback(
            api_key, model,
            {"contents":[{"parts":[{"text":prompt},
                    {"file_data":{"mime_type":file_info.get("mimeType","video/mp4"),
                                  "file_uri":file_uri}}]}],
             "generationConfig":{"temperature":0.35,"responseMimeType":"application/json"}},
            timeout=120, fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash"))
        text = generated["candidates"][0]["content"]["parts"][0]["text"]
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
