import json
import base64
import mimetypes
import os
import re
import time
import requests

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _retry_delay(response, attempt):
    """Honor Gemini Retry-After when supplied; otherwise use capped backoff."""
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after), 1.0), 60.0)
        except (TypeError, ValueError):
            pass
    return min(2 ** (attempt + 1), 30)


def generate_content_with_fallback(api_key, model, payload, timeout=60,
                                  fallback_model=None, max_attempts=6):
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
    attempts = max(1, int(max_attempts))
    for model_index, model_name in enumerate(models):
        for attempt in range(attempts):
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
                    if attempt + 1 < attempts and status in RETRYABLE_STATUS_CODES:
                        time.sleep(_retry_delay(response, attempt))
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
                if attempt + 1 < attempts and (status is None or status in RETRYABLE_STATUS_CODES):
                    time.sleep(_retry_delay(response, attempt))
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

def social_video_prompt(platform, caption=""):
    platform = str(platform or "").strip().lower()
    prompts = {
        "instagram": "Instagram Reels: write a concise, engaging caption, relevant hashtags, and searchable tags.",
        "facebook": "Facebook video: write a natural community-friendly post caption, relevant hashtags, and searchable tags.",
        "snapchat": "Snapchat Spotlight: write a very short punchy caption and a small set of relevant hashtags and tags.",
    }
    if platform not in prompts:
        raise ValueError("Social platform must be instagram, facebook, or snapchat")
    return (
        "Analyze BOTH the actual visual content and the audio/speech in this video. "
        "Do not invent people, events, claims, or details that are not clearly visible or audible. "
        + prompts[platform]
        + " Return ONLY valid JSON with keys title, description, tags, hashtags. "
        "Use the video's language when clear; otherwise use English. "
        "Description is the platform-ready caption/post. "
        "Provide 5-12 relevant tags and 3-8 hashtags. Avoid keyword stuffing. "
        f"Creator note: {str(caption or '')[:1000]}"
    )


def generate_video_metadata(api_key, model, video_path, filename, caption="", platform="youtube", thumbnail_path=None, progress_callback=None, max_processing_seconds=1800):
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for automatic video metadata")
    deadline = time.monotonic() + max(60, int(max_processing_seconds))
    def report(message):
        if progress_callback:
            try:
                progress_callback(message)
            except Exception:
                pass

    file_info = {}
    file_name = None
    try:
        report("📤 Gemini par video upload shuru ho raha hai…")
        size = os.path.getsize(video_path)
        # Gemini resumable upload: initiate, then stream the bytes to the returned URL.
        start_response = requests.post(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            params={"key": api_key},
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": filename[:100]}},
            timeout=30,
        )
        start_response.raise_for_status()
        upload_url = start_response.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Gemini upload session URL missing")
        if time.monotonic() >= deadline:
            raise RuntimeError("Gemini upload session timed out")
        report("📤 Gemini ko video data bhej raha hoon…")
        with open(video_path, "rb") as stream:
            upload_response = requests.put(
                upload_url,
                headers={
                    "X-Goog-Upload-Command": "upload, finalize",
                    "X-Goog-Upload-Offset": "0",
                    "Content-Length": str(size),
                    "Content-Type": "video/mp4",
                },
                data=stream,
                timeout=max(1800, int(max_processing_seconds)),
            )
        upload_response.raise_for_status()
        file_info = upload_response.json().get("file", {})
        file_uri, file_name = file_info.get("uri"), file_info.get("name")
        if not file_uri or not file_name:
            raise RuntimeError("Gemini did not return a usable video file reference")

        report("⏳ Gemini video processing shuru…")
        while time.monotonic() < deadline:
            status = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/" + file_name,
                params={"key": api_key}, timeout=15)
            status.raise_for_status()
            state = status.json()
            if state.get("state") == "ACTIVE":
                break
            if state.get("state") == "FAILED":
                raise RuntimeError("Gemini could not process this video")
            report("⏳ Gemini video processing abhi chal rahi hai…")
            time.sleep(min(10, max(1, deadline - time.monotonic())))
        else:
            raise RuntimeError("Gemini video processing timed out (30 minutes)")

        if str(platform or "youtube").lower() in ("instagram", "facebook", "snapchat"):
            prompt = social_video_prompt(platform, caption)
        else:
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
        report("🖼️ Video aur thumbnail analyse kar raha hai…")
        parts = [{"text": prompt},
                 {"file_data": {"mime_type": file_info.get("mimeType", "video/mp4"),
                                "file_uri": file_uri}}]
        if thumbnail_path:
            with open(thumbnail_path, "rb") as image_stream:
                image_bytes = image_stream.read()
            image_mime = mimetypes.guess_type(thumbnail_path)[0] or "image/jpeg"
            parts.append({"inline_data": {"mime_type": image_mime,
                                          "data": base64.b64encode(image_bytes).decode("ascii")}})
            prompt += " Also inspect the supplied thumbnail image for visual context, but do not mistake thumbnail-only elements as events occurring in the video."
            parts[0]["text"] = prompt
        report("✍️ Title, description aur tags generate kar raha hai…")
        generated = generate_content_with_fallback(
            api_key, model,
            {"contents": [{"parts": parts}],
             "generationConfig": {"temperature": 0.35, "responseMimeType": "application/json"}},
            timeout=90, fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite"),
            max_attempts=2)
        text = generated["candidates"][0]["content"]["parts"][0]["text"]
        return clean_generated_metadata(json.loads(text))
    except requests.RequestException as exc:
        report("❌ Gemini upload/API connection fail hua. Dobara video bhejo.")
        raise RuntimeError("Gemini upload/API network error: " + type(exc).__name__) from None
    finally:
        if file_name:
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
