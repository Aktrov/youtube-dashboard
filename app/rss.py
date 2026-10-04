import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime
from typing import Dict, List, Any, Optional

NAMESPACES = {
    'feed': 'http://www.w3.org/2005/Atom',
    'yt': 'http://www.youtube.com/xml/schemas/2015',
    'media': 'http://search.yahoo.com/mrss/'
}


def extract_channel_id(url_or_id: str) -> Optional[str]:
    """Extract a YouTube channel ID (UCxxxxxxxx) from various inputs.

    Supports direct IDs, full URLs (including /channel/, /c/, /user/, @handle),
    and URLs with query parameters or fragments. The function follows redirects
    and searches the final URL and page HTML for the channel ID using prioritized
    patterns to ensure the correct channel ID is resolved.
    """
    url_or_id = url_or_id.strip()

    # Direct channel ID
    if re.fullmatch(r"UC[a-zA-Z0-9_-]{22}", url_or_id):
        return url_or_id

    # Normalise to a full URL
    url = url_or_id
    if not url.startswith("http"):
        if url.startswith("youtube.com") or url.startswith("www.youtube.com") or url.startswith("youtu.be"):
            url = "https://" + url
        elif url.startswith("@") or url.startswith("c/") or url.startswith("user/") or url.startswith("channel/"):
            url = "https://www.youtube.com/" + url.lstrip('/')
        else:
            # Assume it's a handle name
            url = f"https://www.youtube.com/@{url}"

    # Strip query parameters and fragments
    base_url = url.split('?')[0].split('#')[0]

    # Direct /channel/ pattern in the supplied URL (fast path)
    m = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})", base_url, re.IGNORECASE)
    if m:
        return m.group(1)

    # Fetch the page, follow redirects, and inspect final URL and HTML
    try:
        req = urllib.request.Request(
            base_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            final_url = response.geturl()
            # Check final URL for a /channel/ pattern
            m_final = re.search(r"youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})", final_url, re.IGNORECASE)
            if m_final:
                return m_final.group(1)
            html = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Error fetching channel details from {base_url}: {e}")
        return None

    # Search for channel ID using prioritized patterns to avoid recommended/featured channels
    pattern_groups = [
        # 1. Canonical links (canonical URL of the page itself)
        [
            r'<link\s+[^>]*rel="canonical"\s+[^>]*href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"',
            r'<link\s+[^>]*href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"\s+[^>]*rel="canonical"',
        ],
        # 2. Open Graph tags (designated URL for sharing the current page)
        [
            r'<meta\s+[^>]*property="og:url"\s+[^>]*content="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"',
            r'<meta\s+[^>]*content="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"\s+[^>]*property="og:url"',
        ],
        # 3. Itemprop tags (designated channel owner ID)
        [
            r'<meta\s+[^>]*itemprop="channelId"\s+[^>]*content="(UC[a-zA-Z0-9_-]{22})"',
            r'<meta\s+[^>]*content="(UC[a-zA-Z0-9_-]{22})"\s+[^>]*itemprop="channelId"',
        ],
        # 4. Specific JSON properties referring to this specific page or video owner
        [
            r'"canonicalBaseUrl"\s*:\s*"/channel/(UC[a-zA-Z0-9_-]{22})"',
            r'"c4TabbedHeaderRenderer"\s*:\s*\{[^}]*"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"',
            r'"videoDetails"\s*:\s*\{[^}]*"channelId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"',
            r'"videoOwnerRenderer"\s*:\s*\{[^}]*"browseId"\s*:\s*"(UC[a-zA-Z0-9_-]{22})"',
        ],
        # 5. Twitter url cards
        [
            r'<meta\s+[^>]*name="twitter:url"\s+[^>]*content="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"',
            r'<meta\s+[^>]*content="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"\s+[^>]*name="twitter:url"',
        ],
        # Fallbacks: generic JSON structures (only if specific owner matches failed)
        [
            r'"channelId"\s*:\s*"?(UC[a-zA-Z0-9_-]{22})"?',
            r'"browseId"\s*:\s*"?(UC[a-zA-Z0-9_-]{22})"?',
            r'"externalId"\s*:\s*"?(UC[a-zA-Z0-9_-]{22})"?',
        ]
    ]

    for group in pattern_groups:
        for pat in group:
            match = re.search(pat, html, re.IGNORECASE)
            if match:
                return match.group(1)

    return None


def fetch_channel_feed(channel_id: str) -> Dict[str, Any]:
    """
    Fetches the RSS feed for a YouTube channel and parses its metadata & video list.

    YouTube provides two relevant playlist-based RSS feeds per channel:
      - UULF{suffix}  → the "Videos" tab  (long-form videos ONLY, Shorts excluded)
      - UU{suffix}    → all uploads        (includes Shorts)

    We always try the UULF feed first since it excludes most Shorts at the
    source, cutting down on the per-video is_video_short() checks callers
    still need to do (YouTube's UULF filtering isn't perfect, especially for
    longer-format Shorts). If the UULF feed is empty or unavailable we fall
    back to the full uploads feed.
    """
    # Convert channel_id (UC...) → playlist id suffix (strip the "UC" prefix)
    suffix = channel_id[2:]  # 22-character suffix after "UC"
    uulf_url = f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{suffix}"
    channel_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    xml_data = None
    used_url = None
    for url in (uulf_url, channel_url):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
            # If the feed has at least one <entry>, use it; otherwise try fallback
            if b"<entry>" in xml_data:
                used_url = url
                break
        except Exception:
            continue  # Try the next URL

    if not xml_data:
        raise RuntimeError(f"Could not fetch any RSS feed for channel {channel_id}")

    entry_count = xml_data.count(b"<entry>")
    feed_type = "UULF (Videos-only)" if used_url == uulf_url else "channel (all uploads)"
    print(f"[Feed] channel={channel_id} using {feed_type}: {entry_count} entries found")

    root = ET.fromstring(xml_data)


        # Find author node first to get the true channel name
    author_node = root.find('feed:author', NAMESPACES)
    author_name = None
    if author_node is not None:
        name_node = author_node.find('feed:name', NAMESPACES)
        if name_node is not None and name_node.text:
            author_name = name_node.text.strip()

    # Fall back to root title node if author name wasn't found
    if not author_name:
        title_node = root.find('feed:title', NAMESPACES)
        author_name = title_node.text.strip() if title_node is not None else "Unknown Channel"

    # Use extracted author_name for title
    title = author_name

    # # Title
    # title_node = root.find('feed:title', NAMESPACES)
    # title = title_node.text if title_node is not None else "Unknown Channel"

    # URL / Author URI
    custom_url = f"https://www.youtube.com/channel/{channel_id}"
    author_node = root.find('feed:author', NAMESPACES)
    if author_node is not None:
        uri_node = author_node.find('feed:uri', NAMESPACES)
        if uri_node is not None:
            custom_url = uri_node.text

    videos = []
    for entry in root.findall('feed:entry', NAMESPACES):
        video_id_node = entry.find('yt:videoId', NAMESPACES)
        v_id = video_id_node.text if video_id_node is not None else None
        if not v_id:
            continue

        v_title_node = entry.find('feed:title', NAMESPACES)
        v_title = v_title_node.text if v_title_node is not None else "No Title"

        published_node = entry.find('feed:published', NAMESPACES)
        published_str = published_node.text if published_node is not None else None
        if published_str:
            try:
                published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            except Exception:
                published_dt = datetime.utcnow()
        else:
            published_dt = datetime.utcnow()

        # Parse media group
        media_group = entry.find('media:group', NAMESPACES)
        description = None
        thumbnail_url = None
        if media_group is not None:
            desc_node = media_group.find('media:description', NAMESPACES)
            if desc_node is not None:
                description = desc_node.text
            thumb_node = media_group.find('media:thumbnail', NAMESPACES)
            if thumb_node is not None:
                thumbnail_url = thumb_node.attrib.get('url')

        if not thumbnail_url:
            thumbnail_url = f"https://i.ytimg.com/vi/{v_id}/hqdefault.jpg"

        video_url = f"https://www.youtube.com/watch?v={v_id}"

        videos.append({
            "video_id": v_id,
            "title": v_title,
            "description": description,
            "published_at": published_dt.replace(tzinfo=None),  # Naive UTC for database
            "thumbnail_url": thumbnail_url,
            "video_url": video_url
        })

    return {
        "title": title,
        "custom_url": custom_url,
        "videos": videos
    }


def is_video_short(video_id: str) -> bool:
    """
    Checks if a video is a YouTube Short by making a GET request.
    YouTube redirects /shorts/{video_id} to /watch?v={video_id} if it is a regular video.
    If it is a Short, it remains at /shorts/{video_id}. We use GET without reading the
    body for maximum compatibility with YouTube's redirect handler.
    """
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            final_url = response.geturl()
            # If the final URL contains '/watch', it's a regular video.
            return "/shorts/" in final_url
    except Exception as e:
        # On error, default to False to avoid missing regular videos
        print(f"Error checking if video {video_id} is a Short: {e}")
        return False


# is_video_short()'s own urlopen(timeout=5) covers connect/read stalls, but
# NOT a hung DNS lookup (getaddrinfo isn't bound by that timeout on every
# platform) -- the one way this call can block well past 5s with no
# exception raised. A small daemon-thread pool + hard wall-clock deadline is
# the last line of defense so a single bad lookup can never freeze the
# scheduler loop. Bounded to a couple of workers: normal operation with a
# warm cache barely touches this, so a stuck lookup tying up a worker for a
# while is an acceptable, rare cost.
_short_check_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="short-check")


def is_video_short_bounded(video_id: str, hard_timeout: float = 8.0) -> Optional[bool]:
    """Runs is_video_short() with a hard wall-clock deadline on top of its
    own socket timeout. Returns None (unknown) if the deadline is hit --
    callers should treat that as "try again next poll", not cache it, so a
    transient hang self-heals instead of freezing a channel's video type
    forever."""
    future = _short_check_executor.submit(is_video_short, video_id)
    try:
        return future.result(timeout=hard_timeout)
    except FutureTimeoutError:
        print(f"[Shorts] Hard timeout ({hard_timeout}s) checking video {video_id}; will retry on next poll.")
        return None
