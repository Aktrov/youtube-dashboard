from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, index=True)
    channel_id = Column(String, unique=True, index=True, nullable=False)
    title = Column(String, nullable=False)
    custom_url = Column(String, nullable=True)
    thumbnail_url = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    last_polled_at = Column(DateTime, nullable=True)
    # Feed health: outcome of the most recent poll attempt. last_poll_ok is
    # None until the channel has been polled once; last_poll_error holds the
    # exception text from the last failed poll (cleared on the next success).
    last_poll_ok = Column(Boolean, nullable=True)
    last_poll_error = Column(Text, nullable=True)

    videos = relationship("Video", back_populates="channel", cascade="all, delete-orphan")


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(String, unique=True, index=True, nullable=False)
    channel_id = Column(String, ForeignKey("channels.channel_id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=False)
    thumbnail_url = Column(String, nullable=True)
    video_url = Column(String, nullable=False)
    is_watched = Column(Boolean, default=False)
    is_bookmarked = Column(Boolean, default=False)
    added_at = Column(DateTime, default=datetime.utcnow)
    # Resume playback: last known position (seconds) and the video's total
    # length, so we can restart where the user left off and tell when a video
    # is "basically finished". playback_seconds is 0/NULL when unstarted or
    # completed; playback_updated_at stamps the last save.
    playback_seconds = Column(Integer, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    playback_updated_at = Column(DateTime, nullable=True)

    channel = relationship("Channel", back_populates="videos")
