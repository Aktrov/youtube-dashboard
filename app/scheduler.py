import time
import threading
from app.database import SessionLocal
from app.config import settings
from app import crud, rss, models

_stop_event = threading.Event()
_thread = None


def poll_all_channels():
    """
    Worker function that connects to the database, gets all registered channels,
    and updates their video libraries from YouTube's RSS feeds.
    """
    db = SessionLocal()
    try:
        channels = crud.get_channels(db)
        print(f"[Scheduler] Starting polling for {len(channels)} channels...")
        for channel in channels:
            if _stop_event.is_set():
                break
            try:
                feed_data = rss.fetch_channel_feed(channel.channel_id)
                # Update title if current title is generic or missing
                if feed_data.get("title") and feed_data["title"] != "Unknown Channel":
                    crud.update_channel_title(db, channel.channel_id, feed_data["title"])
                new_vids = crud.add_videos_if_not_exists(db, channel.channel_id, feed_data["videos"])
                crud.set_channel_poll_result(db, channel.channel_id, ok=True)
                if len(new_vids) > 0:
                    print(f"[Scheduler] Polled '{channel.title}': added {len(new_vids)} new videos.")
            except Exception as e:
                print(f"[Scheduler] Error polling channel '{channel.title}' ({channel.channel_id}): {e}")
                try:
                    crud.set_channel_poll_result(db, channel.channel_id, ok=False, error=str(e))
                except Exception:
                    pass
    finally:
        db.close()


def clean_existing_videos():
    """
    On startup, remove any stored videos that are actually Shorts (the UULF
    feed's Shorts filtering isn't perfect) and prune every channel's video
    list down to the 2 latest entries.
    """
    db = SessionLocal()
    try:
        removed = crud.remove_misclassified_shorts(db)
        if removed:
            print(f"[Scheduler] Removed {removed} misclassified Shorts from the database.")

        channels = db.query(models.Channel).all()
        print(f"[Scheduler] Pruning videos to 2 latest per subscription for {len(channels)} channels...")
        for channel in channels:
            if _stop_event.is_set():
                break
            crud.prune_videos_for_channel(db, channel.channel_id)
        print("[Scheduler] Startup pruning complete.")
    except Exception as e:
        print(f"[Scheduler] Error during startup pruning: {e}")
    finally:
        db.close()


def _worker_loop():
    print("[Scheduler] Background scheduler loop started.")
    # Run once at startup
    poll_all_channels()

    # Start existing database clean-up and pruning in a separate thread so it does not block the polling scheduler loop
    threading.Thread(target=clean_existing_videos, daemon=True).start()

    while not _stop_event.is_set():
        # Sleep in 1-second intervals to allow responsive shutdown
        interval = settings.poll_interval
        for _ in range(interval):
            if _stop_event.is_set():
                break
            time.sleep(1)

        if not _stop_event.is_set():
            poll_all_channels()

    print("[Scheduler] Background scheduler loop stopped.")


def start_scheduler():
    global _thread, _stop_event
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_worker_loop, daemon=True)
    _thread.start()


def stop_scheduler():
    global _thread, _stop_event
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=5)
