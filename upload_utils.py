import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def parse_tags(raw):
    value = (raw or "").strip()
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("Tags JSON must be a list")
        values = parsed
    except json.JSONDecodeError:
        values = value.split(",")
    tags = []
    for item in values:
        tag = str(item).strip()
        if not tag:
            continue
        if len(tag) > 500:
            raise ValueError("Each tag must be 500 characters or fewer")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > 500:
        raise ValueError("YouTube allows at most 500 tags")
    return tags

def parse_publish_at(value, now=None):
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Schedule must be an ISO date/time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    parsed = parsed.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if parsed <= current:
        raise ValueError("Scheduled publish time must be in the future")
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def build_video_metadata(title, description, tags_raw, privacy, publish_at):
    title = (title or "").strip()
    if not title:
        raise ValueError("Video title is required")
    if len(title) > 100:
        raise ValueError("YouTube title must be 100 characters or fewer")
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("Privacy must be private, unlisted, or public")
    status = {"privacyStatus": "private" if publish_at else privacy}
    if publish_at:
        status["publishAt"] = publish_at
    snippet = {"title": title, "description": (description or "")[:5000]}
    tags = parse_tags(tags_raw)
    if tags:
        snippet["tags"] = tags
    return {"snippet": snippet, "status": status}
