import re

SUPPORTED_PLATFORMS = {"instagram", "facebook"}


def build_social_draft(platform, topic):
    platform = str(platform or "").strip().lower()
    topic = re.sub(r"\s+", " ", str(topic or "")).strip()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("Platform instagram ya facebook hona chahiye.")
    if not topic:
        raise ValueError("Topic likho. Example: /draft instagram | new gaming video")
    if len(topic) > 500:
        raise ValueError("Topic 500 characters se chhota rakho.")
    if platform == "instagram":
        caption = (f"{topic}\n\nAapko ye kaisa laga? Comment mein zaroor batayein! "
                   "Post ko save aur share karna na bhoolein.")
        hashtags = "#Instagram #Reels #ExplorePage #ContentCreator"
    else:
        caption = (f"{topic}\n\nAapki rai hamare liye important hai. Comment karke batayein "
                   "aur post ko apne friends ke saath share karein! — Facebook")
        hashtags = "#Facebook #Community #Updates"
    return {"platform": platform, "topic": topic, "caption": caption, "hashtags": hashtags}
