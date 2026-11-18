import re

SUPPORTED_PLATFORMS = {"instagram", "facebook", "snapchat"}


def build_social_draft(platform, topic):
    platform = str(platform or "").strip().lower()
    topic = re.sub(r"\s+", " ", str(topic or "")).strip()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("Platform instagram, facebook ya snapchat hona chahiye.")
    if not topic:
        raise ValueError("Topic likho. Example: /draft instagram | new gaming video")
    if len(topic) > 500:
        raise ValueError("Topic 500 characters se chhota rakho.")
    if platform == "instagram":
        caption = (f"{topic}\n\nAapko ye kaisa laga? Comment mein zaroor batayein! "
                   "Post ko save aur share karna na bhoolein.")
        hashtags = "#Instagram #Reels #ExplorePage #ContentCreator"
    elif platform == "facebook":
        caption = (f"{topic}\n\nAapki rai hamare liye important hai. Comment karke batayein "
                   "aur post ko apne friends ke saath share karein! — Facebook")
        hashtags = "#Facebook #Community #Updates"
    else:
        caption = f"{topic}\n\nSnapchat Spotlight ke liye short, catchy video caption."
        hashtags = "#Snapchat #Spotlight #SnapchatVideo"
    return {"platform": platform, "topic": topic, "caption": caption, "hashtags": hashtags}
