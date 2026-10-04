from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, asc, func
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from app import models, schemas, rss
from app.config import settings


# Channel operations
def get_channel(db: Session, channel_db_id: int) -> Optional[models.Channel]:
    return db.query(models.Channel).filter(models.Channel.id == channel_db_id).first()


def get_channel_by_yt_id(db: Session, channel_id: str) -> Optional[models.Channel]:
    return db.query(models.Channel).filter(models.Channel.channel_id == channel_id).first()


def get_channels(db: Session, skip: int = 0, limit: int = 100) -> List[models.Channel]:
    return db.query(models.Channel).offset(skip).limit(limit).all()


def create_channel(db: Session, channel: schemas.ChannelCreate) -> models.Channel:
    db_channel = models.Channel(
        channel_id=channel.channel_id,
        title=channel.title,
        custom_url=channel.custom_url,
        thumbnail_url=channel.thumbnail_url,
        description=channel.description,
        added_at=datetime.utcnow()
    )
    db.add(db_channel)
    db.commit()
    db.refresh(db_channel)
    return db_channel


def delete_channel(db: Session, channel_id: str) -> bool:
    db_channel = db.query(models.Channel).filter(models.Channel.channel_id == channel_id).first()
    if db_channel:
        db.delete(db_channel)
        db.commit()
        return True
    return False

def update_channel_title(db: Session, channel_id: str, new_title: str) -> Optional[models.Channel]:
    db_channel = db.query(models.Channel).filter(models.Channel.channel_id == channel_id).first()
    if db_channel:
        db_channel.title = new_title
        db.commit()
        db.refresh(db_channel)
    return db_channel

def update_channel_polled(db: Session, channel_id: str) -> Optional[models.Channel]:
    db_channel = db.query(models.Channel).filter(models.Channel.channel_id == channel_id).first()
    if db_channel:
        db_channel.last_polled_at = datetime.utcnow()
        db.commit()
        db.refresh(db_channel)
    return db_channel


def set_channel_poll_result(
    db: Session, channel_id: str, ok: bool, error: Optional[str] = None
) -> Optional[models.Channel]:
    """Record the outcome of a poll attempt: stamps last_polled_at, sets
    last_poll_ok, and stores (on failure) / clears (on success) last_poll_error."""
    db_channel = db.query(models.Channel).filter(models.Channel.channel_id == channel_id).first()
    if db_channel:
        db_channel.last_polled_at = datetime.utcnow()
        db_channel.last_poll_ok = ok
        db_channel.last_poll_error = None if ok else (error or "unknown error")[:500]
        db.commit()
        db.refresh(db_channel)
    return db_channel


def get_cached_video_type(db: Session, video_id: str) -> Optional[bool]:
    """Returns the cached is_short verdict for a video_id, or None if it's
    never been checked."""
    row = db.query(models.VideoTypeCache).filter(models.VideoTypeCache.video_id == video_id).first()
    return row.is_short if row else None


def set_cached_video_type(db: Session, video_id: str, is_short: bool) -> None:
    row = db.query(models.VideoTypeCache).filter(models.VideoTypeCache.video_id == video_id).first()
    if row:
        row.is_short = is_short
        row.checked_at = datetime.utcnow()
    else:
        db.add(models.VideoTypeCache(video_id=video_id, is_short=is_short, checked_at=datetime.utcnow()))
    db.commit()


def is_video_short_cached(db: Session, video_id: str) -> bool:
    """Short/long-form check with a database-backed cache in front of the
    network call. A video's type never changes after upload, so once we've
    checked a video_id we never hit YouTube for it again -- this is what
    keeps the scheduler responsive: without it, every Short in a channel's
    RSS window gets re-checked over the network on every single poll,
    forever (Shorts are never stored as Video rows, so `exists` never
    short-circuits them)."""
    cached = get_cached_video_type(db, video_id)
    if cached is not None:
        return cached

    result = rss.is_video_short_bounded(video_id)
    if result is None:
        # Hard-timeout case: unknown, not a confirmed verdict. Don't cache a
        # guess -- treat as "not a Short" just for this poll so ingestion
        # isn't blocked, and let it be re-checked (hopefully successfully)
        # next time.
        return False

    set_cached_video_type(db, video_id, result)
    return result


def get_channel_unwatched_counts(db: Session) -> Dict[str, int]:
    """{channel_id: number of unwatched videos} for every channel with at least one."""
    rows = (
        db.query(models.Video.channel_id, func.count(models.Video.id))
        .filter(models.Video.is_watched == False)  # noqa: E712
        .group_by(models.Video.channel_id)
        .all()
    )
    return {channel_id: count for channel_id, count in rows}


def get_dashboard_stats(db: Session) -> Dict[str, Any]:
    """Headline numbers for the Feed Vitals strip."""
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    total_channels = db.query(func.count(models.Channel.id)).scalar() or 0
    unwatched = db.query(func.count(models.Video.id)).filter(
        models.Video.is_watched == False  # noqa: E712
    ).scalar() or 0
    bookmarked = db.query(func.count(models.Video.id)).filter(
        models.Video.is_bookmarked == True  # noqa: E712
    ).scalar() or 0
    added_this_week = db.query(func.count(models.Video.id)).filter(
        models.Video.added_at >= week_ago
    ).scalar() or 0

    in_progress = db.query(func.count(models.Video.id)).filter(
        models.Video.is_watched == False,  # noqa: E712
        models.Video.playback_seconds.isnot(None),
        models.Video.playback_seconds > 0,
    ).scalar() or 0

    errored = db.query(func.count(models.Channel.id)).filter(
        models.Channel.last_poll_ok == False  # noqa: E712
    ).scalar() or 0

    last_polled_at = db.query(func.max(models.Channel.last_polled_at)).scalar()
    never_polled = db.query(func.count(models.Channel.id)).filter(
        models.Channel.last_polled_at.is_(None)
    ).scalar() or 0

    # func.max() on SQLite may hand back a datetime or a raw "YYYY-MM-DD HH:MM:SS"
    # string depending on driver/type coercion — normalise to ISO-8601 + Z so
    # the browser's Date() parses it on every platform (iOS Safari rejects the
    # space-separated form).
    if hasattr(last_polled_at, "isoformat"):
        last_poll_iso = last_polled_at.isoformat() + "Z"
    elif last_polled_at:
        last_poll_iso = str(last_polled_at).replace(" ", "T") + "Z"
    else:
        last_poll_iso = None

    return {
        "channels": total_channels,
        "unwatched": unwatched,
        "bookmarked": bookmarked,
        "added_this_week": added_this_week,
        "in_progress": in_progress,
        "errored_feeds": errored,
        "never_polled": never_polled,
        "last_polled_at": last_poll_iso,
        "poll_interval_seconds": settings.poll_interval,
        "keep_per_channel": settings.keep_per_channel,
    }


# Video operations
def get_video(db: Session, video_id: str) -> Optional[models.Video]:
    return db.query(models.Video).filter(models.Video.video_id == video_id).first()


def get_videos(
    db: Session,
    channel_id: Optional[str] = None,
    is_watched: Optional[bool] = None,
    is_bookmarked: Optional[bool] = None,
    in_progress: bool = False,
    since: Optional[datetime] = None,
    sort: str = "newest",
    skip: int = 0,
    limit: int = 100
) -> List[models.Video]:
    query = db.query(models.Video).options(joinedload(models.Video.channel))

    if channel_id is not None:
        query = query.filter(models.Video.channel_id == channel_id)
    if is_watched is not None:
        query = query.filter(models.Video.is_watched == is_watched)
    if is_bookmarked is not None:
        query = query.filter(models.Video.is_bookmarked == is_bookmarked)
    if in_progress:
        query = query.filter(
            models.Video.is_watched == False,  # noqa: E712
            models.Video.playback_seconds.isnot(None),
            models.Video.playback_seconds > 0,
        )
    if since is not None:
        query = query.filter(models.Video.published_at >= since)

    if sort == "oldest":
        query = query.order_by(asc(models.Video.published_at), asc(models.Video.added_at))
    else:  # "newest" (default)
        query = query.order_by(desc(models.Video.published_at), desc(models.Video.added_at))

    return query.offset(skip).limit(limit).all()


def mark_videos_watched(
    db: Session,
    channel_id: Optional[str] = None,
    only_unwatched: bool = True,
) -> int:
    """Bulk-mark videos as watched. Scoped to one channel if channel_id is
    given, otherwise every channel. Returns the number of rows changed."""
    query = db.query(models.Video)
    if channel_id is not None:
        query = query.filter(models.Video.channel_id == channel_id)
    if only_unwatched:
        query = query.filter(models.Video.is_watched == False)  # noqa: E712
    changed = query.update({models.Video.is_watched: True}, synchronize_session=False)
    db.commit()
    return changed


def add_videos_if_not_exists(db: Session, channel_id: str, videos_data: List[Dict[str, Any]]) -> List[models.Video]:
    """
    Add up to settings.keep_per_channel videos from the RSS feed to the
    database for this channel. The feed data comes from the UULF (Videos-only)
    playlist, but that filtering isn't reliable (longer-format Shorts
    especially slip through), so each new candidate is verified via
    is_video_short_cached() (network hit only on the first-ever check of a
    given video_id) before being stored.
    """
    MAX_PER_CHANNEL = settings.keep_per_channel

    new_videos = []
    stored_count = 0  # confirmed non-Short videos for this channel

    for video in videos_data:
        if stored_count >= MAX_PER_CHANNEL:
            break

        exists = db.query(models.Video).filter(models.Video.video_id == video["video_id"]).first()
        if exists:
            stored_count += 1
            continue

        if is_video_short_cached(db, video["video_id"]):
            continue

        db_video = models.Video(
            video_id=video["video_id"],
            channel_id=channel_id,
            title=video["title"],
            description=video["description"],
            published_at=video["published_at"],
            thumbnail_url=video["thumbnail_url"],
            video_url=video["video_url"],
            is_watched=False,
            is_bookmarked=False,
            added_at=datetime.utcnow()
        )
        db.add(db_video)
        new_videos.append(db_video)
        stored_count += 1

    if new_videos:
        db.commit()
        for v in new_videos:
            db.refresh(v)

    prune_videos_for_channel(db, channel_id)
    return new_videos


def prune_videos_for_channel(db: Session, channel_id: str):
    """
    Keep only the settings.keep_per_channel latest videos (by published_at
    desc) and any bookmarked videos for a given channel, deleting the rest.
    """
    keep_n = settings.keep_per_channel

    # Get all videos for the channel, sorted by published_at DESC
    videos = (
        db.query(models.Video)
        .filter(models.Video.channel_id == channel_id)
        .order_by(desc(models.Video.published_at))
        .all()
    )

    # We want to keep:
    # 1. The keep_n latest videos
    # 2. Any bookmarked video
    # 3. Any video the user is partway through (has a resume point) — otherwise
    #    it would be deleted out from under them before they finish it
    to_keep = set()
    for v in videos[:keep_n]:
        to_keep.add(v.video_id)
    for v in videos:
        if v.is_bookmarked or (v.playback_seconds or 0) > 0:
            to_keep.add(v.video_id)
            
    # Delete any video not in the keep set
    deleted_count = 0
    for v in videos:
        if v.video_id not in to_keep:
            db.delete(v)
            deleted_count += 1
            
    if deleted_count > 0:
        db.commit()
        print(f"[Pruner] Deleted {deleted_count} old videos for channel {channel_id}.")


def remove_misclassified_shorts(db: Session) -> int:
    """
    Scans every stored video and deletes any that are actually YouTube
    Shorts, verified via is_video_short_cached() (cached after the first
    check, so re-runs on later startups don't re-hit the network for videos
    already classified). Needed because the UULF feed's Shorts filtering
    isn't perfect, so some Shorts get stored as regular videos before this
    check existed / runs again.
    """
    removed = 0
    for video in db.query(models.Video).all():
        if is_video_short_cached(db, video.video_id):
            db.delete(video)
            removed += 1

    if removed:
        db.commit()

    return removed


def update_video_status(
    db: Session,
    video_id: str,
    is_watched: Optional[bool] = None,
    is_bookmarked: Optional[bool] = None
) -> Optional[models.Video]:
    db_video = db.query(models.Video).filter(models.Video.video_id == video_id).first()
    if db_video:
        if is_watched is not None:
            db_video.is_watched = is_watched
            # Marking watched by hand also clears the resume point; un-watching
            # leaves playback_seconds alone (it's already 0/NULL in practice).
            if is_watched:
                db_video.playback_seconds = None
        if is_bookmarked is not None:
            db_video.is_bookmarked = is_bookmarked
        db.commit()
        db.refresh(db_video)
    return db_video


# A video within this many seconds of its end, or past this fraction, counts as
# finished: clear the resume point and mark it watched.
_FINISH_TAIL_SECONDS = 15
_FINISH_FRACTION = 0.97
# Below this many seconds, playback hasn't really started — nothing to resume.
_RESUME_FLOOR_SECONDS = 10


def update_video_progress(
    db: Session,
    video_id: str,
    seconds: float,
    duration: Optional[float] = None,
) -> Optional[models.Video]:
    """Record how far into a video the user has watched, for resume-on-reopen.
    Auto-marks the video watched (and clears the resume point) once playback
    reaches the end."""
    db_video = db.query(models.Video).filter(models.Video.video_id == video_id).first()
    if not db_video:
        return None

    seconds = max(0, int(seconds or 0))
    if duration and duration > 0:
        db_video.duration_seconds = int(duration)

    dur = db_video.duration_seconds or 0
    finished = dur > 0 and (
        seconds >= dur - _FINISH_TAIL_SECONDS or seconds >= dur * _FINISH_FRACTION
    )

    if finished:
        db_video.playback_seconds = None
        db_video.is_watched = True
    elif seconds >= _RESUME_FLOOR_SECONDS:
        db_video.playback_seconds = seconds
    else:
        db_video.playback_seconds = None

    db_video.playback_updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_video)
    return db_video
