def comment_poll_interval(value):
    """Return a safe polling interval; the requested two-minute cadence is the floor."""
    try:
        seconds = int(value or 120)
    except (TypeError, ValueError):
        seconds = 120
    return max(120, min(seconds, 3600))


def score_video_seo(video):
    """Transparent metadata completeness score; not a search-ranking prediction."""
    title = str(video.get("title") or "").strip()
    description = str(video.get("description") or "").strip()
    tags = video.get("tags") or []
    checks = {
        "title": 20 <= len(title) <= 70,
        "description": len(description) >= 100,
        "tags": len(tags) >= 5,
        "thumbnail": bool(video.get("thumbnail")),
    }
    score = sum(weight for key, weight in zip(checks, (25, 30, 25, 20)) if checks[key])
    suggestions = []
    if not checks["title"]:
        suggestions.append("Title ko clear aur descriptive rakho (20–70 characters target).")
    if not checks["description"]:
        suggestions.append("Description mein video ka accurate summary aur useful details add karo.")
    if not checks["tags"]:
        suggestions.append("Kam se kam 5 relevant tags add karo; unrelated tags mat use karo.")
    if not checks["thumbnail"]:
        suggestions.append("Custom, readable thumbnail upload karo.")
    return {"score": score, "checks": checks, "suggestions": suggestions}


def weak_seo_videos(videos, threshold=80):
    """Return every video below the metadata-completeness threshold."""
    return [video for video in videos
            if int(video.get("score", 0) or 0) < int(threshold)]


def seo_score_distribution(videos):
    """Count videos in stable score bands for the Telegram SEO graph."""
    bands = {"poor": 0, "fair": 0, "good": 0, "excellent": 0}
    for video in videos:
        score = max(0, min(100, int(video.get("score", 0) or 0)))
        if score < 40:
            bands["poor"] += 1
        elif score < 70:
            bands["fair"] += 1
        elif score < 90:
            bands["good"] += 1
        else:
            bands["excellent"] += 1
    return bands
