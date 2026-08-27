from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional, List

from app import crud, schemas, rss, database
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _range_cutoff(range_key: str) -> Optional[datetime]:
    """Map a 'range' pill to a published_at cutoff. 'all' (or anything
    unrecognised) means no cutoff."""
    now = datetime.utcnow()
    if range_key == "today":
        return now - timedelta(days=1)
    if range_key == "week":
        return now - timedelta(days=7)
    if range_key == "month":
        return now - timedelta(days=30)
    return None


# HTML Dashboard View
@router.get("/", response_class=HTMLResponse)
def dashboard_view(
    request: Request,
    channel_id: Optional[str] = None,
    filter_type: str = "unwatched",  # "all", "unwatched", "bookmarked"
    q: Optional[str] = None,
    sort: str = "newest",            # "newest", "oldest"
    range: str = "all",              # "today", "week", "month", "all"
    db: Session = Depends(database.get_db)
):
    # Fetch channels for sidebar
    channels = crud.get_channels(db)
    unwatched_counts = crud.get_channel_unwatched_counts(db)

    is_watched = None
    is_bookmarked = None

    if filter_type == "unwatched":
        is_watched = False
    elif filter_type == "bookmarked":
        is_bookmarked = True

    # Get videos matching the status filters
    videos = crud.get_videos(
        db,
        channel_id=channel_id,
        is_watched=is_watched,
        is_bookmarked=is_bookmarked,
        since=_range_cutoff(range),
        sort=sort if sort in ("newest", "oldest") else "newest",
        limit=200
    )

    # Fuzzy title filter if search string is provided
    if q:
        q_lower = q.lower()
        videos = [
            v for v in videos
            if q_lower in v.title.lower() or (v.description and q_lower in v.description.lower())
        ]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "channels": channels,
            "unwatched_counts": unwatched_counts,
            "videos": videos,
            "stats": crud.get_dashboard_stats(db),
            "active_channel_id": channel_id,
            "active_filter": filter_type,
            "active_sort": sort if sort in ("newest", "oldest") else "newest",
            "active_range": range if range in ("today", "week", "month", "all") else "all",
            "search_query": q or "",
            "base_path": settings.base_path,
            "now_utc": datetime.utcnow(),
        }
    )


@router.get("/api/stats")
def api_stats(db: Session = Depends(database.get_db)):
    return crud.get_dashboard_stats(db)


@router.get("/manifest.webmanifest")
def manifest():
    bp = settings.base_path or ""
    return JSONResponse({
        "name": "YT Relay",
        "short_name": "YT Relay",
        "start_url": bp + "/",
        "scope": bp + "/",
        "display": "standalone",
        "background_color": "#06080a",
        "theme_color": "#06080a",
        "icons": [
            {"src": bp + "/static/logo.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
        ],
    })


# API: Subscription Management
@router.post("/api/channels", response_model=schemas.ChannelResponse)
def subscribe_channel(
    payload: schemas.ChannelCreate,
    db: Session = Depends(database.get_db)
):
    # Resolve handle/URL/raw ID to raw 24-character channel ID
    channel_id = rss.extract_channel_id(payload.channel_id)
    if not channel_id:
        raise HTTPException(
            status_code=400,
            detail="Could not extract channel ID. Please provide a valid channel URL or UC... ID."
        )

    # Check if already subscribed
    existing = crud.get_channel_by_yt_id(db, channel_id)
    if existing:
        raise HTTPException(
            status_code=400,
            detail="Channel is already subscribed."
        )

    # Fetch initial feed to get metadata (like the official title)
    try:
        feed_data = rss.fetch_channel_feed(channel_id)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch YouTube RSS feed: {e}"
        )

    # Save to database
    new_channel = schemas.ChannelCreate(
        channel_id=channel_id,
        title=feed_data["title"] or payload.title or "Unknown Channel",
        custom_url=feed_data["custom_url"] or payload.custom_url,
        thumbnail_url=payload.thumbnail_url,
        description=payload.description
    )
    db_channel = crud.create_channel(db, new_channel)

    # Backfill with the initial videos from feed
    crud.add_videos_if_not_exists(db, channel_id, feed_data["videos"])

    return db_channel


@router.delete("/api/channels/{channel_id}")
def unsubscribe_channel(
    channel_id: str,
    db: Session = Depends(database.get_db)
):
    success = crud.delete_channel(db, channel_id)
    if not success:
        raise HTTPException(status_code=404, detail="Channel not found.")
    return {"message": "Successfully unsubscribed from channel."}


@router.post("/api/channels/{channel_id}/poll")
def force_poll_channel(
    channel_id: str,
    db: Session = Depends(database.get_db)
):
    channel = crud.get_channel_by_yt_id(db, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")

    try:
        feed_data = rss.fetch_channel_feed(channel_id)
        new_vids = crud.add_videos_if_not_exists(db, channel_id, feed_data["videos"])
        crud.set_channel_poll_result(db, channel_id, ok=True)
        return {"message": "Polled successfully.", "new_videos_count": len(new_vids)}
    except Exception as e:
        crud.set_channel_poll_result(db, channel_id, ok=False, error=str(e))
        raise HTTPException(status_code=500, detail=f"Polling failed: {e}")


@router.post("/api/channels/poll")
def force_poll_all_channels(
    db: Session = Depends(database.get_db)
):
    channels = crud.get_channels(db)
    polled_count = 0
    failed_count = 0
    new_vids_count = 0

    for channel in channels:
        try:
            feed_data = rss.fetch_channel_feed(channel.channel_id)
            new_vids = crud.add_videos_if_not_exists(db, channel.channel_id, feed_data["videos"])
            crud.set_channel_poll_result(db, channel.channel_id, ok=True)
            polled_count += 1
            new_vids_count += len(new_vids)
        except Exception as e:
            failed_count += 1
            try:
                crud.set_channel_poll_result(db, channel.channel_id, ok=False, error=str(e))
            except Exception:
                pass

    return {
        "message": f"Polled {polled_count} channels."
                   + (f" {failed_count} failed." if failed_count else ""),
        "new_videos_count": new_vids_count,
        "failed_count": failed_count,
    }


# API: Video Interaction
@router.put("/api/videos/{video_id}", response_model=schemas.VideoResponse)
def update_video(
    video_id: str,
    payload: schemas.VideoUpdate,
    db: Session = Depends(database.get_db)
):
    video = crud.update_video_status(
        db,
        video_id=video_id,
        is_watched=payload.is_watched,
        is_bookmarked=payload.is_bookmarked
    )
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")
    return video


class BulkWatchRequest(BaseModel):
    channel_id: Optional[str] = None  # None = every channel


@router.post("/api/videos/mark-watched")
def bulk_mark_watched(
    payload: BulkWatchRequest = Body(default=BulkWatchRequest()),
    db: Session = Depends(database.get_db)
):
    """Mark every currently-unwatched video watched, optionally scoped to one
    channel. Backs the dashboard's 'mark all watched' action."""
    if payload.channel_id and not crud.get_channel_by_yt_id(db, payload.channel_id):
        raise HTTPException(status_code=404, detail="Channel not found.")
    count = crud.mark_videos_watched(db, channel_id=payload.channel_id)
    return {"message": f"Marked {count} videos watched.", "count": count}


class ChannelUpdate(BaseModel):
    title: str

@router.patch("/api/channels/{channel_id}")
def rename_channel(
    channel_id: str,
    payload: ChannelUpdate,
    db: Session = Depends(database.get_db)
):
    channel = crud.update_channel_title(db, channel_id=channel_id, new_title=payload.title)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found.")
    return channel