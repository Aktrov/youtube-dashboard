import os
try:
    from pydantic_settings import BaseSettings
except ImportError:
    from pydantic import BaseSettings


class Settings(BaseSettings):
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./youtube_tracker.db")
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "600"))  # in seconds
    app_title: str = "YouTube Subscription Tracker"
    base_path: str = os.getenv("BASE_PATH", "")

    # How many recent videos to retain per channel. The tracker is an "inbox":
    # each poll adds at most this many fresh videos per channel, and the pruner
    # trims every channel back down to this many (bookmarked videos are always
    # kept on top of that). Raise it to keep a longer tail per channel.
    keep_per_channel: int = int(os.getenv("KEEP_PER_CHANNEL", "2"))

    class Config:
        env_file = ".env"


settings = Settings()
