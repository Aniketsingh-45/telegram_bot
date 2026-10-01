import os
import re
import shutil
import asyncio
import tempfile
import logging
import collections
import subprocess
import uuid
import html
import random
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, Request, BackgroundTasks
# pyrefly: ignore [missing-import]
from fastapi.responses import HTMLResponse
# pyrefly: ignore [missing-import]
import httpx
import yt_dlp
 
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# In-memory log buffer to inspect recent logs via /logs endpoint
class MemoryLogHandler(logging.Handler):
    def __init__(self, maxlen=200):
        super().__init__()
        self.logs = collections.deque(maxlen=maxlen)

    def emit(self, record):
        try:
            self.logs.append(self.format(record))
        except Exception:
            pass

log_capture = MemoryLogHandler()
log_capture.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger().addHandler(log_capture)


# Configurable environment variables
VERSION = "1.3.0"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "8061236263:AAGLbpiAy0VFPdxHD5XB-rwws7wYdb5ptNQ"
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB Telegram Bot API limit

# In-memory store: chat_id -> {url, message_id} waiting for quality selection
pending_downloads: Dict[int, Dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan manager for FastAPI.
    Automatically sets the Telegram webhook on server startup if WEBHOOK_URL is provided.
    """
    logger.info(f"=== Server starting up: Telegram Video Downloader Bot v{VERSION} ===")
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if webhook_url:
        target = f"{webhook_url.rstrip('/')}/webhook"
        logger.info(f"Registering Telegram webhook: {target}")
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                res = await client.post(f"{TELEGRAM_API}/setWebhook", json={"url": target})
                logger.info(f"Telegram setWebhook response: {res.json()}")
            except Exception as e:
                logger.error(f"Failed to auto-register webhook: {e}")
    else:
        logger.info("WEBHOOK_URL not set in environment. Webhook not auto-registered on startup.")
    yield


app = FastAPI(title="Telegram Video Downloader Bot", lifespan=lifespan)


# URL extraction pattern
URL_REGEX = re.compile(r'https?://[^\s<>"]+')


def is_youtube_video(url: str) -> bool:
    """Returns True if URL is a YouTube long-form video (not Shorts)."""
    yt_patterns = [
        r'(?:youtube\.com/watch\?.*v=|youtu\.be/)[A-Za-z0-9_-]{11}',
    ]
    shorts_patterns = [
        r'youtube\.com/shorts/',
        r'youtu\.be/shorts/',
    ]
    url_lower = url.lower()
    if any(re.search(p, url_lower) for p in shorts_patterns):
        return False
    return any(re.search(p, url) for p in yt_patterns)


def is_instagram_url(url: str) -> bool:
    """Returns True if the URL is an Instagram URL."""
    return 'instagram.com' in url.lower()


async def send_quality_keyboard(client: httpx.AsyncClient, chat_id: int, video_url: str) -> Optional[int]:
    """Sends an inline keyboard asking user to choose video quality."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎯 360p", "callback_data": "q_360"},
                {"text": "📺 480p", "callback_data": "q_480"},
            ],
            [
                {"text": "🔥 720p (HD)", "callback_data": "q_720"},
                {"text": "💎 1080p (FHD)", "callback_data": "q_1080"},
            ],
        ]
    }
    try:
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": "🎬 <b>YouTube Video mila!</b>\n\nKaunsi <b>quality</b> mein download karein?",
            "parse_mode": "HTML",
            "reply_markup": keyboard
        })
        result = resp.json().get("result", {})
        return result.get("message_id")
    except Exception as e:
        logger.error(f"Error sending quality keyboard: {e}")
        return None


async def answer_callback_query(client: httpx.AsyncClient, callback_query_id: str, text: str = ""):
    """Acknowledge a callback query to remove the loading spinner."""
    try:
        await client.post(f"{TELEGRAM_API}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text
        })
    except Exception as e:
        logger.warning(f"Error answering callback: {e}")


async def send_chat_action(client: httpx.AsyncClient, chat_id: int, action: str = "upload_video"):
    """Send typing or upload action to Telegram."""
    try:
        await client.post(f"{TELEGRAM_API}/sendChatAction", json={
            "chat_id": chat_id,
            "action": action
        })
    except Exception as e:
        logger.warning(f"Error sending chat action {action}: {e}")


async def send_message(client: httpx.AsyncClient, chat_id: int, text: str, parse_mode: str = "HTML"):
    """Helper to send a text message."""
    try:
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        })
        return resp.json()
    except Exception as e:
        logger.error(f"Error sending message to {chat_id}: {e}")
        return None


async def edit_message(client: httpx.AsyncClient, chat_id: int, message_id: int, text: str, parse_mode: str = "HTML"):
    """Helper to edit an existing message."""
    try:
        await client.post(f"{TELEGRAM_API}/editMessageText", json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        })
    except Exception as e:
        logger.warning(f"Error editing message {message_id}: {e}")


def _get_ydl_cookie_opts(temp_dir: str, platform: str = "youtube") -> dict:
    """
    Returns yt-dlp cookie options dict if a cookies file/env is found.
    platform: 'youtube' or 'instagram' — checks platform-specific cookie sources first.
    """
    # Platform-specific cookie paths
    if platform == "instagram":
        cookie_paths = [
            "/etc/secrets/instagram_cookies.txt",
            "/app/instagram_cookies.txt",
            "instagram_cookies.txt",
            "/etc/secrets/cookies.txt",
            "/app/cookies.txt",
            "cookies.txt",
        ]
        env_keys = ["INSTAGRAM_COOKIES", "YOUTUBE_COOKIES"]
    else:
        cookie_paths = [
            "/etc/secrets/cookies.txt",
            "/app/cookies.txt",
            "cookies.txt",
        ]
        env_keys = ["YOUTUBE_COOKIES"]

    for cp in cookie_paths:
        if os.path.exists(cp):
            logger.info(f"Loaded {platform} cookies from file: {cp}")
            return {'cookiefile': cp}

    for env_key in env_keys:
        cookies_data = os.getenv(env_key, "").strip()
        if cookies_data:
            cookies_data = cookies_data.replace("\\n", "\n")
            cookie_file = os.path.join(temp_dir, f"{platform}_cookies.txt")
            with open(cookie_file, "w", encoding="utf-8", newline="\n") as cf:
                cf.write(cookies_data)
            logger.info(f"Loaded {env_key} from env ({len(cookies_data)} chars)")
            return {'cookiefile': cookie_file}

    logger.info(f"No {platform} cookies found")
    return {}


def _extract_instagram_shortcode(url: str) -> Optional[str]:
    """Extracts the shortcode from an Instagram URL."""
    match = re.search(r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    return match.group(1) if match else None


def _load_cookies_as_dict(temp_dir: str) -> dict:
    """
    Load cookies from INSTAGRAM_COOKIES env or cookie files and return
    a dict suitable for httpx cookies parameter.
    """
    cookie_dict = {}
    cookie_file_path = None

    # Find cookie file
    for cp in [
        "/etc/secrets/instagram_cookies.txt",
        "/app/instagram_cookies.txt",
        "instagram_cookies.txt",
        "/etc/secrets/cookies.txt",
        "/app/cookies.txt",
        "cookies.txt",
    ]:
        if os.path.exists(cp):
            cookie_file_path = cp
            break

    if not cookie_file_path:
        # Try env vars
        for env_key in ["INSTAGRAM_COOKIES", "YOUTUBE_COOKIES"]:
            cookies_data = os.getenv(env_key, "").strip()
            if cookies_data:
                cookies_data = cookies_data.replace("\\n", "\n")
                cookie_file_path = os.path.join(temp_dir, "http_cookies.txt")
                with open(cookie_file_path, "w", encoding="utf-8", newline="\n") as cf:
                    cf.write(cookies_data)
                break

    if cookie_file_path:
        try:
            with open(cookie_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 7:
                        cookie_dict[parts[5]] = parts[6]
        except Exception as e:
            logger.warning(f"Failed to parse cookie file: {e}")

    return cookie_dict


def _scrape_instagram_media(url: str, temp_dir: str) -> List[Dict[str, Any]]:
    """
    Fallback: scrape Instagram pages for media URLs using cookies.
    Tries multiple page types: embed, main page, API endpoint.
    """
    shortcode = _extract_instagram_shortcode(url)
    if not shortcode:
        raise ValueError("Cannot extract shortcode from Instagram URL")

    # Load cookies for HTTP requests
    cookie_dict = _load_cookies_as_dict(temp_dir)
    has_http_cookies = bool(cookie_dict)
    logger.info(f"Embed scraping: have {len(cookie_dict)} HTTP cookies")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Ch-Ua-Platform': '"Windows"',
    }

    results = []
    all_page_contents = []

    # ---- Try multiple page sources ----
    urls_to_try = [
        # Main post page (most data when logged in)
        f"https://www.instagram.com/p/{shortcode}/",
        f"https://www.instagram.com/reel/{shortcode}/",
        # Embed pages
        f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
        f"https://www.instagram.com/p/{shortcode}/embed/",
        f"https://www.instagram.com/reel/{shortcode}/embed/",
        # API endpoint
        f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis",
    ]

    for page_url in urls_to_try:
        try:
            resp = httpx.get(
                page_url,
                headers=headers,
                cookies=cookie_dict if has_http_cookies else None,
                timeout=15.0,
                follow_redirects=True
            )
            if resp.status_code == 200 and len(resp.text) > 500:
                all_page_contents.append(resp.text)
                logger.info(f"Fetched {page_url} ({len(resp.text)} chars)")
                # If we got a big page with cookies, that's probably enough
                if has_http_cookies and len(resp.text) > 10000:
                    break
        except Exception as e:
            logger.warning(f"Failed to fetch {page_url}: {e}")

    if not all_page_contents:
        raise ValueError("Could not fetch any Instagram page")

    # Combine all page contents for searching
    combined_content = "\n".join(all_page_contents)
    logger.info(f"Total scraped content: {len(combined_content)} chars from {len(all_page_contents)} page(s)")

    # ---- Extract video URLs ----
    video_urls = []
    for pattern in [
        r'"video_url"\s*:\s*"([^"]+)"',
        r'"video_versions"\s*:\s*\[.*?"url"\s*:\s*"([^"]+)"',
        r'"src"\s*:\s*"(https://[^"]*\.mp4[^"]*)"',
        r'<video[^>]+src="([^"]+)"',
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'property="og:video"\s+content="([^"]+)"',
        r'property="og:video:secure_url"\s+content="([^"]+)"',
    ]:
        found = re.findall(pattern, combined_content)
        for u in found:
            clean_u = u.replace('\\u0026', '&').replace('\\/', '/').replace('&amp;', '&')
            if clean_u not in video_urls and 'instagram' in clean_u.lower() or 'cdninstagram' in clean_u.lower() or 'fbcdn' in clean_u.lower():
                video_urls.append(clean_u)

    # ---- Extract image URLs ----
    image_urls = []
    for pattern in [
        r'"display_url"\s*:\s*"([^"]+)"',
        r'"display_src"\s*:\s*"([^"]+)"',
        r'"image_versions2".*?"url"\s*:\s*"([^"]+)"',
        r'property="og:image"\s+content="([^"]+)"',
        r'"thumbnail_src"\s*:\s*"([^"]+)"',
        r'class="EmbeddedMediaImage"[^>]+src="([^"]+)"',
    ]:
        found = re.findall(pattern, combined_content)
        for u in found:
            clean_u = u.replace('\\u0026', '&').replace('\\/', '/').replace('&amp;', '&')
            if clean_u not in image_urls and 's150x150' not in clean_u and 's320x320' not in clean_u:
                image_urls.append(clean_u)

    # ---- Extract title ----
    title = "Instagram Post"
    m_title = re.search(r'"text"\s*:\s*"([^"]{1,300})"', combined_content)
    if m_title:
        raw = m_title.group(1)
        # Skip if it looks like JSON metadata, not a caption
        if len(raw) > 5 and not raw.startswith('{'):
            title = raw[:200]
    if title == "Instagram Post":
        m_title = re.search(r'<meta property="og:description" content="([^"]+)"', combined_content)
        if m_title:
            title = html.unescape(m_title.group(1))[:200]

    logger.info(f"Scraped {len(video_urls)} video URL(s) and {len(image_urls)} image URL(s)")

    # ---- Download videos ----
    dl_headers = {
        'User-Agent': headers['User-Agent'],
        'Referer': 'https://www.instagram.com/',
    }
    for i, vurl in enumerate(video_urls[:5]):  # Max 5 videos
        try:
            file_path = os.path.join(temp_dir, f"insta_scraped_v{i}_{uuid.uuid4().hex[:6]}.mp4")
            resp = httpx.get(vurl, headers=dl_headers, timeout=60.0, follow_redirects=True)
            resp.raise_for_status()
            with open(file_path, "wb") as f:
                f.write(resp.content)
            fsize = os.path.getsize(file_path)
            if fsize > 5000:  # Must be > 5KB to be a real video
                file_path = _ensure_telegram_audio(file_path)
                results.append({
                    'file_path': file_path,
                    'title': title,
                    'duration': None,
                    'width': None,
                    'height': None,
                    'file_size': os.path.getsize(file_path),
                    'webpage_url': url,
                    'is_image': False,
                })
                logger.info(f"Downloaded scraped video {i}: {fsize} bytes")
        except Exception as e:
            logger.warning(f"Failed to download scraped video {i}: {e}")

    # ---- Download images if no videos ----
    if not results:
        seen_bases = set()
        for i, iurl in enumerate(image_urls[:10]):  # Max 10 images
            base_match = re.search(r'/([a-zA-Z0-9_-]+)_n\.jpg', iurl)
            base_id = base_match.group(1) if base_match else str(i)
            if base_id in seen_bases:
                continue
            seen_bases.add(base_id)

            try:
                file_path = os.path.join(temp_dir, f"insta_scraped_i{i}_{uuid.uuid4().hex[:6]}.jpg")
                resp = httpx.get(iurl, headers=dl_headers, timeout=15.0, follow_redirects=True)
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                fsize = os.path.getsize(file_path)
                if fsize > 1000:
                    results.append({
                        'file_path': file_path,
                        'title': title,
                        'duration': None,
                        'width': None,
                        'height': None,
                        'file_size': fsize,
                        'webpage_url': url,
                        'is_image': True,
                    })
                    logger.info(f"Downloaded scraped image {i}: {fsize} bytes")
            except Exception as e:
                logger.warning(f"Failed to download scraped image {i}: {e}")

    if not results:
        raise ValueError(f"Scraped {len(video_urls)} video URLs and {len(image_urls)} image URLs but none could be downloaded")

    return results


def _sync_download_instagram(url: str, temp_dir: str) -> List[Dict[str, Any]]:
    """
    Downloads Instagram posts/reels/carousels using multiple strategies:
    1. yt-dlp with Instagram cookies (most reliable)
    2. yt-dlp without cookies using 'best' format
    3. Direct HTTP scraping with cookies (fallback)
    """
    cookie_opts = _get_ydl_cookie_opts(temp_dir, platform="instagram")
    has_cookies = bool(cookie_opts)

    # ---- Strategy 1 & 2: yt-dlp ----
    outtmpl = os.path.join(temp_dir, "insta_%(playlist_index)s_%(id)s.%(ext)s")
    ydl_opts = {
        'format': 'best[ext=mp4]/best/bestvideo+bestaudio',
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'postprocessor_args': {
            'merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k']
        },
        'noplaylist': False,
        'quiet': False,
        'no_warnings': False,
        'http_chunk_size': 10485760,
        'concurrent_fragment_downloads': 4,
        'socket_timeout': 30,
        'retries': 3,
    }
    ydl_opts.update(cookie_opts)

    ytdlp_error = None
    try:
        results = []
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise ValueError("No info extracted from Instagram URL.")

            entries = info.get('entries') or [info]
            logger.info(f"Instagram yt-dlp: found {len(entries)} media item(s)")

            for entry in entries:
                if not entry:
                    continue
                prep = ydl.prepare_filename(entry)
                base_no_ext = os.path.splitext(prep)[0]

                target_file = None
                for candidate in [f"{base_no_ext}.mp4", prep]:
                    if os.path.isfile(candidate):
                        target_file = candidate
                        break

                if not target_file:
                    already_found = {r['file_path'] for r in results}
                    all_files = [
                        os.path.join(temp_dir, f)
                        for f in os.listdir(temp_dir)
                        if not f.endswith(('.part', '.ytdl', '.json'))
                        and os.path.isfile(os.path.join(temp_dir, f))
                        and os.path.join(temp_dir, f) not in already_found
                    ]
                    if all_files:
                        target_file = max(all_files, key=os.path.getsize)

                if not target_file:
                    logger.warning(f"Could not find output file for entry: {entry.get('id')}")
                    continue

                is_image = target_file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                if not is_image:
                    target_file = _ensure_telegram_audio(target_file)

                results.append({
                    'file_path': target_file,
                    'title': entry.get('title', '') or info.get('title', 'Instagram Post'),
                    'duration': entry.get('duration'),
                    'width': entry.get('width'),
                    'height': entry.get('height'),
                    'file_size': os.path.getsize(target_file),
                    'webpage_url': entry.get('webpage_url', url),
                    'is_image': is_image,
                })

        if results:
            return results
        ytdlp_error = "yt-dlp returned no media files"

    except Exception as e:
        ytdlp_error = str(e)
        logger.warning(f"yt-dlp Instagram failed: {e}")

    # ---- Strategy 3: Direct HTTP scraping fallback ----
    logger.info(f"Trying HTTP scraping fallback for {url}...")
    try:
        return _scrape_instagram_media(url, temp_dir)
    except Exception as scrape_err:
        logger.warning(f"HTTP scraping also failed: {scrape_err}")

    # All strategies failed
    if has_cookies:
        raise yt_dlp.utils.DownloadError(
            f"Instagram download failed. Cookies may be expired — re-export them from your browser. "
            f"Original error: {ytdlp_error}"
        )
    else:
        raise yt_dlp.utils.DownloadError(
            f"Instagram requires login cookies. Set INSTAGRAM_COOKIES env variable. "
            f"Original error: {ytdlp_error}"
        )


def _sync_download(url: str, temp_dir: str, quality: Optional[str] = None) -> Dict[str, Any]:
    """
    Synchronous worker running yt-dlp download in a background thread.
    Returns metadata dict with filepath, title, duration, and file size.
    quality: '360', '480', '720', '1080' for YouTube; None = auto-best
    Instagram URLs are handled by _sync_download_instagram instead.
    """
    outtmpl = os.path.join(temp_dir, "video_%(id)s.%(ext)s")

    # Build format string based on requested quality
    if quality:
        h = quality  # e.g. '720'
        fmt = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h}]+bestaudio/"
            f"best[height<={h}]/best"
        )
    else:
        # Always merge best video + audio for proper sound
        fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/b[ext=mp4]/best'

    ydl_opts = {
        'format': fmt,
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'postprocessor_args': {
            'merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k']
        },
        'http_chunk_size': 10485760,  # 10MB chunks
        'concurrent_fragment_downloads': 5,
        'noplaylist': True,
        'quiet': False,
        'no_warnings': False,
        # Use mobile player clients to bypass YouTube datacenter bot detection
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios'],
            }
        },
    }

    # Add cookies if available
    ydl_opts.update(_get_ydl_cookie_opts(temp_dir, platform="youtube"))

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("No video information could be retrieved.")

        # Resolve output file accurately
        target_file = None
        prep = ydl.prepare_filename(info)
        base_no_ext = os.path.splitext(prep)[0]
        for candidate in [f"{base_no_ext}.mp4", prep]:
            if os.path.isfile(candidate):
                target_file = candidate
                break

        if not target_file:
            # Fallback to finding the largest valid .mp4 file in temp_dir
            mp4_files = [
                os.path.join(temp_dir, f)
                for f in os.listdir(temp_dir)
                if f.endswith('.mp4') and not f.endswith(('.part', '.ytdl')) and os.path.isfile(os.path.join(temp_dir, f))
            ]
            if mp4_files:
                target_file = max(mp4_files, key=os.path.getsize)
            else:
                found_files = [
                    os.path.join(temp_dir, f)
                    for f in os.listdir(temp_dir)
                    if not f.endswith(('.part', '.ytdl')) and os.path.isfile(os.path.join(temp_dir, f))
                ]
                if not found_files:
                    raise FileNotFoundError("Video file was not created by yt-dlp.")
                target_file = max(found_files, key=os.path.getsize)

        # Guarantee audio is standardized to AAC-LC for 100% Telegram audio compatibility
        final_file = _ensure_telegram_audio(target_file)

        return {
            "file_path": final_file,
            "title": info.get("title", "Video"),
            "duration": info.get("duration", 0),
            "width": info.get("width"),
            "height": info.get("height"),
            "file_size": os.path.getsize(final_file),
            "webpage_url": info.get("webpage_url", url)
        }


def _ensure_telegram_audio(input_file: str) -> str:
    """
    Always re-encodes audio to standard AAC-LC stereo so Telegram players on
    Android, iOS, Desktop, and Web can play it with full sound.
    This fixes Instagram Reels that sometimes have silent/missing audio.
    """
    try:
        probe = subprocess.run([
            'ffprobe', '-v', 'error',
            '-show_entries', 'stream=codec_type,codec_name',
            '-select_streams', 'a',
            '-of', 'csv=p=0',
            input_file
        ], capture_output=True, text=True, timeout=30)

        if 'audio' not in probe.stdout:
            logger.info("Source has no audio stream — skipping re-encode.")
            return input_file

        output_file = os.path.splitext(input_file)[0] + "_playable.mp4"
        # Always re-encode audio to AAC-LC stereo at 128k/44100Hz for max compatibility
        cmd = [
            'ffmpeg', '-y', '-i', input_file,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-ac', '2',          # force stereo
            '-movflags', '+faststart',
            output_file
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode == 0 and os.path.isfile(output_file) and os.path.getsize(output_file) > 0:
            logger.info(f"Re-encoded audio to AAC-LC stereo: {output_file}")
            return output_file
        else:
            logger.warning(f"FFmpeg audio re-encode failed, using original. stderr: {res.stderr[-300:]}")
            return input_file
    except Exception as e:
        logger.warning(f"Error in _ensure_telegram_audio: {e}")
        return input_file



async def action_ticker(client: httpx.AsyncClient, chat_id: int, action: str, stop_ev: asyncio.Event):
    """Periodically sends Telegram chat action while an operation is running."""
    while not stop_ev.is_set():
        await send_chat_action(client, chat_id, action)
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def _send_media_item(
    client: httpx.AsyncClient,
    chat_id: int,
    media: Dict[str, Any],
    caption: str,
    index: int,
    total: int
) -> bool:
    """
    Sends a single media item (photo or video) to Telegram.
    Returns True on success.
    """
    file_path = media['file_path']
    is_image = media.get('is_image', file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')))
    duration = media.get('duration')
    file_size = media.get('file_size', os.path.getsize(file_path))

    if file_size > MAX_TELEGRAM_SIZE_BYTES:
        size_mb = round(file_size / (1024 * 1024), 1)
        logger.warning(f"Media item {index}/{total} too large: {size_mb} MB — skipping.")
        return False

    # Bypass Telegram deduplication cache by appending a random byte
    with open(file_path, "ab") as f:
        f.write(bytes([random.randint(0, 255)]))

    # Only include caption on first item if multiple
    item_caption = caption if index == 1 else ""

    with open(file_path, "rb") as f:
        filename = os.path.basename(file_path)
        if is_image:
            files = {"photo": (filename, f, "image/jpeg")}
            data = {
                "chat_id": str(chat_id),
                "caption": item_caption,
                "parse_mode": "HTML"
            }
            api_endpoint = f"{TELEGRAM_API}/sendPhoto"
        else:
            files = {"video": (filename, f, "video/mp4")}
            data = {
                "chat_id": str(chat_id),
                "caption": item_caption,
                "parse_mode": "HTML",
                "supports_streaming": "true"
            }
            if duration:
                data["duration"] = str(int(duration))
            if media.get("width"):
                data["width"] = str(media["width"])
            if media.get("height"):
                data["height"] = str(media["height"])
            api_endpoint = f"{TELEGRAM_API}/sendVideo"

        resp = await client.post(api_endpoint, data=data, files=files)

    if resp.status_code == 200:
        logger.info(f"Sent media item {index}/{total} to chat {chat_id}")
        return True
    else:
        logger.error(f"Failed to send media item {index}/{total}: {resp.text}")
        return False


async def process_instagram_download(chat_id: int, video_url: str):
    """
    Background worker specifically for Instagram posts/reels/carousels.
    Handles downloading multiple images/videos in a carousel post.
    """
    temp_dir = tempfile.mkdtemp(prefix="tg_insta_")
    stop_ticker = asyncio.Event()

    timeout_cfg = httpx.Timeout(600.0, connect=60.0)
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        ticker_task = asyncio.create_task(action_ticker(client, chat_id, "upload_photo", stop_ticker))

        status_msg = await send_message(
            client, chat_id,
            "🔍 <b>Instagram link mila!</b> Media fetch ki ja rahi hai, kripya intezaar karein..."
        )
        status_msg_id = status_msg.get("result", {}).get("message_id") if status_msg else None

        try:
            if status_msg_id:
                await edit_message(
                    client, chat_id, status_msg_id,
                    "⬇️ <b>Instagram se media download ho raha hai...</b>"
                )

            logger.info(f"Downloading Instagram media from {video_url} for chat {chat_id}...")
            media_list = await asyncio.to_thread(_sync_download_instagram, video_url, temp_dir)

            total = len(media_list)
            logger.info(f"Downloaded {total} Instagram media item(s)")

            if status_msg_id:
                item_word = "items" if total > 1 else "item"
                await edit_message(
                    client, chat_id, status_msg_id,
                    f"📤 <b>{total} media {item_word} download ho gaye! Telegram par bhej raha hoon...</b>"
                )

            # Build caption from first item title
            first_title = media_list[0].get('title', 'Instagram Post') if media_list else 'Instagram Post'
            if re.match(r'^[\d_\-]+$', first_title.strip()):
                first_title = "Instagram Post"
            caption = f"📸 <b>{first_title[:250]}</b>"
            caption += "\n\n🤖 <i>Downloaded via @AniketVideo_bot</i>"

            has_video = any(not m.get('is_image', True) for m in media_list)
            if has_video:
                caption += "\n🔊 <i>Agar aawaz na aaye toh video ke speaker icon par tap karein!</i>"

            success_count = 0
            for i, media in enumerate(media_list, 1):
                await send_chat_action(
                    client, chat_id,
                    "upload_photo" if media.get('is_image') else "upload_video"
                )
                ok = await _send_media_item(client, chat_id, media, caption, i, total)
                if ok:
                    success_count += 1

            # Delete status message
            if status_msg_id:
                try:
                    await client.post(f"{TELEGRAM_API}/deleteMessage", json={
                        "chat_id": chat_id,
                        "message_id": status_msg_id
                    })
                except Exception:
                    pass

            if success_count == 0:
                await send_message(
                    client, chat_id,
                    "❌ <b>Koi bhi media send nahi ho paya.</b> Shayad post private hai ya size limit exceed ho gayi."
                )
            elif success_count < total:
                await send_message(
                    client, chat_id,
                    f"⚠️ <b>{success_count}/{total} media items send ho gaye.</b> Kuch items size limit ki wajah se skip ho gaye."
                )

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp DownloadError for Instagram: {e}")
            raw_err = str(e).strip()
            clean_err = raw_err.split('\n')[0][:180]
            clean_err = re.sub(r'^(ERROR:\s*(\[[^\]]+\]\s*)?)', '', clean_err).strip()
            if 'login' in raw_err.lower() or 'cookies' in raw_err.lower() or 'empty media' in raw_err.lower():
                err_text = (
                    "❌ <b>Instagram media download nahi ho paya!</b>\n\n"
                    "⚠️ Instagram ab login maangta hai. Server se bina login ke download nahi hota.\n\n"
                    "🔧 <b>Solution:</b> Bot admin ko <code>INSTAGRAM_COOKIES</code> environment variable set karna hoga "
                    "apne Instagram session cookies ke saath.\n\n"
                    "💡 <i>Public Reels abhi bhi kaam kar sakti hain — dobara try karein!</i>"
                )
            else:
                err_text = (
                    "❌ <b>Instagram media download nahi ho paya!</b>\n\n"
                    f"⚠️ <b>Karan:</b> <code>{clean_err}</code>\n\n"
                    "💡 <b>Tips:</b>\n"
                    "• Check karein ki post <b>public</b> hai\n"
                    "• Dobara try karein, thodi der mein kaam kar sakta hai"
                )
            if status_msg_id:
                await edit_message(client, chat_id, status_msg_id, err_text)
            else:
                await send_message(client, chat_id, err_text)

        except Exception as e:
            logger.error(f"Unexpected error in process_instagram_download: {e}", exc_info=True)
            err_str = str(e)
            if 'INSTAGRAM_COOKIES' in err_str or 'login' in err_str.lower():
                err_text = (
                    "❌ <b>Instagram download fail ho gaya!</b>\n\n"
                    "⚠️ Instagram ab bina login ke media serve nahi karta (server IPs ke liye).\n\n"
                    "🔧 <b>Fix:</b> Bot admin ko Render/server par <code>INSTAGRAM_COOKIES</code> env variable set karna hoga.\n\n"
                    "💡 <i>Kuch public Reels bina cookies ke bhi kaam kar sakti hain — dobara try karein!</i>"
                )
            else:
                err_text = f"❌ <b>Kuch gadbad ho gayi:</b> {err_str[:150]}"
            if status_msg_id:
                await edit_message(client, chat_id, status_msg_id, err_text)
            else:
                await send_message(client, chat_id, err_text)

        finally:
            stop_ticker.set()
            ticker_task.cancel()
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"Cleaned up temp directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean temp dir: {e}")


async def process_video_download(chat_id: int, video_url: str, quality: Optional[str] = None):
    """
    Background worker that handles downloading the video and sending it to Telegram.
    quality: '360', '480', '720', '1080' for YouTube; None = auto-best
    Runs asynchronously without blocking webhook responses.
    """
    temp_dir = tempfile.mkdtemp(prefix="tg_video_")
    stop_ticker = asyncio.Event()
    
    timeout_cfg = httpx.Timeout(600.0, connect=60.0)
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        # Start periodic chat action ticker
        ticker_task = asyncio.create_task(action_ticker(client, chat_id, "upload_video", stop_ticker))

        # Send initial status message
        status_msg = await send_message(
            client, chat_id,
            "🔍 <b>Link mil gaya!</b> Video fetch ki ja rahi hai, kripya intezaar karein..."
        )
        status_msg_id = status_msg.get("result", {}).get("message_id") if status_msg else None

        try:

            if status_msg_id:
                await edit_message(
                    client, chat_id, status_msg_id,
                    "⬇️ <b>Video download ho raha hai...</b>"
                )

            # Download video in thread pool to prevent blocking asyncio loop
            quality_label = f" ({quality}p)" if quality else ""
            logger.info(f"Downloading video{quality_label} from {video_url} for chat {chat_id}...")
            if quality:
                await edit_message(client, chat_id, status_msg_id, f"⬇️ <b>{quality}p mein download ho raha hai...</b>")
            video_data = await asyncio.to_thread(_sync_download, video_url, temp_dir, quality)

            file_path = video_data["file_path"]
            file_size = video_data["file_size"]
            title = video_data["title"]
            duration = video_data.get("duration")

            logger.info(f"Download complete: {file_path}, Size: {file_size / (1024*1024):.2f} MB")

            # Check size against Telegram limit
            if file_size > MAX_TELEGRAM_SIZE_BYTES:
                size_mb = round(file_size / (1024 * 1024), 1)
                warning_text = (
                    f"⚠️ <b>Video ka size bahut bada hai!</b>\n\n"
                    f"📁 Size: <b>{size_mb} MB</b>\n"
                    f"Telegram Bot API sirf <b>50 MB</b> tak ki files allow karta hai.\n\n"
                    f"💡 <i>Tip: Koi chhota video ya Reel/Shorts bhejein.</i>"
                )
                if status_msg_id:
                    await edit_message(client, chat_id, status_msg_id, warning_text)
                else:
                    await send_message(client, chat_id, warning_text)
                return

            # Determine if it's an image or video based on extension
            is_image = file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))

            # Update status message
            if status_msg_id:
                media_type_str = "Photo" if is_image else "Video"
                await edit_message(
                    client, chat_id, status_msg_id,
                    f"📤 <b>Download complete! Telegram par {media_type_str} bhej raha hoon...</b>"
                )

            await send_chat_action(client, chat_id, "upload_photo" if is_image else "upload_video")

            # Prepare caption
            caption = f"📸 <b>{title[:300]}</b>" if is_image else f"🎬 <b>{title[:300]}</b>"
            if duration and not is_image:
                mins, secs = divmod(int(duration), 60)
                caption += f"\n⏱ <i>Duration: {mins:02d}:{secs:02d}</i>"
            caption += "\n\n🤖 <i>Downloaded via @AniketVideo_bot</i>"
            if not is_image:
                caption += "\n🔊 <i>Agar aawaz na aaye toh video ke speaker icon par tap karein!</i>"

            # Bypass Telegram Deduplication Cache by appending a random byte
            with open(file_path, "ab") as f:
                f.write(bytes([random.randint(0, 255)]))

            # Upload media directly using multipart/form-data
            with open(file_path, "rb") as f:
                filename = os.path.basename(file_path)
                
                if is_image:
                    files = {"photo": (filename, f, "image/jpeg")}
                    data = {
                        "chat_id": str(chat_id),
                        "caption": caption,
                        "parse_mode": "HTML"
                    }
                    api_endpoint = f"{TELEGRAM_API}/sendPhoto"
                else:
                    files = {"video": (filename, f, "video/mp4")}
                    data = {
                        "chat_id": str(chat_id),
                        "caption": caption,
                        "parse_mode": "HTML",
                        "supports_streaming": "true"
                    }
                    if duration:
                        data["duration"] = str(int(duration))
                    if video_data.get("width"):
                        data["width"] = str(video_data["width"])
                    if video_data.get("height"):
                        data["height"] = str(video_data["height"])
                    api_endpoint = f"{TELEGRAM_API}/sendVideo"

                upload_resp = await client.post(api_endpoint, data=data, files=files)

                if upload_resp.status_code == 200:
                    logger.info(f"Video sent successfully to chat {chat_id}!")
                    # Delete the interim status message to keep chat clean
                    if status_msg_id:
                        try:
                            await client.post(f"{TELEGRAM_API}/deleteMessage", json={
                                "chat_id": chat_id,
                                "message_id": status_msg_id
                            })
                        except Exception:
                            pass
                else:
                    logger.error(f"Failed to send video: {upload_resp.text}")
                    error_msg = f"❌ <b>Video upload failed:</b> {upload_resp.json().get('description', 'Unknown error')}"
                    if status_msg_id:
                        await edit_message(client, chat_id, status_msg_id, error_msg)
                    else:
                        await send_message(client, chat_id, error_msg)

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp DownloadError: {e}")
            raw_err = str(e).strip()
            clean_err = raw_err.split('\n')[0][:180]
            clean_err = re.sub(r'^(ERROR:\s*(\[[^\]]+\]\s*)?)', '', clean_err).strip()

            if "Unsupported URL" in raw_err:
                tip = "• Yeh link supported nahi hai. Kripya kisi supported website (jaise YouTube, Instagram, Facebook) ka link bhejein."
                clean_err = "Unsupported URL"
            elif any(k in raw_err for k in ["Sign in to confirm", "player response", "429", "bot"]):
                tip = (
                    "💡 <b>YouTube ne datacenter IP block kiya hai (Bot detection).</b>\n\n"
                    "• <b>Instagram Reels</b>, <b>TikTok</b> ya <b>Twitter</b> videos try karein (ye 100% chalte hain).\n"
                    "• Ya YouTube ke liye <code>YOUTUBE_COOKIES</code> configure karein."
                )
            else:
                tip = "• Check karein ki link sahi aur publicly accessible hai."

            err_text = (
                "❌ <b>Video download nahi ho paya!</b>\n\n"
                f"⚠️ <b>Karan:</b> <code>{clean_err}</code>\n\n"
                f"{tip}"
            )
            if status_msg_id:
                await edit_message(client, chat_id, status_msg_id, err_text)
            else:
                await send_message(client, chat_id, err_text)



        except Exception as e:
            logger.error(f"Unexpected error in process_video_download: {e}", exc_info=True)
            err_text = f"❌ <b>Kuch gadbad ho gayi:</b> {str(e)[:150]}"
            if status_msg_id:
                await edit_message(client, chat_id, status_msg_id, err_text)
            else:
                await send_message(client, chat_id, err_text)

        finally:
            stop_ticker.set()
            ticker_task.cancel()
            # Clean up temporary directory and files
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"Cleaned up temp directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean temp dir: {e}")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Health check and status dashboard."""
    webhook_configured = bool(os.getenv("WEBHOOK_URL"))
    webhook_status_badge = (
        '<span style="background: #10b981; color: white; padding: 4px 10px; border-radius: 9999px; font-size: 12px; margin-left: 8px;">Configured</span>'
        if webhook_configured
        else '<span style="background: #f59e0b; color: white; padding: 4px 10px; border-radius: 9999px; font-size: 12px; margin-left: 8px;">Needs Setup</span>'
    )
    return f"""
    <!DOCTYPE html>
    <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Telegram Video Downloader Bot</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
                .card {{ background: #1e293b; padding: 2rem; border-radius: 1rem; box-shadow: 0 10px 25px rgba(0,0,0,0.5); text-align: center; max-width: 520px; width: 100%; border: 1px solid #334155; }}
                h1 {{ color: #38bdf8; margin: 0.5rem 0; font-size: 1.6rem; }}
                p {{ color: #94a3b8; line-height: 1.6; font-size: 0.95rem; margin-bottom: 1.25rem; }}
                .status {{ display: inline-block; background: #10b981; color: white; padding: 0.35rem 0.85rem; border-radius: 9999px; font-weight: bold; font-size: 0.875rem; margin-bottom: 1rem; }}
                .links {{ display: flex; flex-direction: column; gap: 0.75rem; margin-top: 1.5rem; }}
                .btn {{ display: block; padding: 0.75rem 1rem; border-radius: 0.5rem; text-decoration: none; font-weight: 600; font-size: 0.95rem; transition: background 0.2s; }}
                .btn-primary {{ background: #0284c7; color: white; }}
                .btn-primary:hover {{ background: #0369a1; }}
                .btn-secondary {{ background: #334155; color: #cbd5e1; }}
                .btn-secondary:hover {{ background: #475569; }}
                .info-box {{ text-align: left; background: #0f172a; padding: 12px 16px; border-radius: 8px; font-size: 13px; color: #94a3b8; margin: 12px 0; border: 1px solid #1e293b; }}
            </style>
        </head>
        <body>
            <div class="card">
                <div class="status">🟢 Bot Server Running</div>
                <h1>Telegram Video Downloader</h1>
                <p>FastAPI webhook server is running and ready to handle video download requests for YouTube, Instagram, and more.</p>
                <div class="info-box">
                    <div><b>Webhook Status:</b> {webhook_status_badge}</div>
                    <div style="margin-top: 6px;"><b>Bot Username:</b> @AniketVideo_bot</div>
                </div>
                <div class="links">
                    <a class="btn btn-primary" href="https://t.me/AniketVideo_bot" target="_blank">Open Telegram Bot 🚀</a>
                    <a class="btn btn-secondary" href="/webhook-info" target="_blank">Check Telegram Webhook Status 🔍</a>
                </div>
            </div>
        </body>
    </html>
    """


@app.get("/webhook-info")
async def get_webhook_info():
    """Inspect current Telegram webhook status."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(f"{TELEGRAM_API}/getWebhookInfo")
            return res.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}


@app.get("/set-webhook")
async def manual_set_webhook(request: Request, url: Optional[str] = None):
    """
    Manually register or update the webhook.
    Usage: /set-webhook or /set-webhook?url=https://your-domain.onrender.com
    """
    # Auto-detect current host URL if not explicitly provided
    detected_base = str(request.base_url).rstrip("/")
    if detected_base.startswith("http://") and "localhost" not in detected_base and "127.0.0.1" not in detected_base:
        detected_base = "https://" + detected_base[7:]

    target_url = url or os.getenv("WEBHOOK_URL", "").strip() or detected_base
    target = f"{target_url.rstrip('/')}/webhook"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            res = await client.post(f"{TELEGRAM_API}/setWebhook", json={"url": target})
            return {"status": "success", "target_url": target, "telegram_response": res.json()}
        except Exception as e:
            return {"status": "error", "message": str(e)}


@app.get("/version")
async def get_version():
    """Returns the deployed bot version."""
    return {"version": VERSION}


@app.get("/logs")
async def get_recent_logs():
    """Inspect recent server and download logs in browser."""
    return {"logs": list(log_capture.logs)}



@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives incoming updates from Telegram webhook.
    Delegates heavy processing to BackgroundTasks and responds immediately.
    """
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON payload received: {e}")
        return {"status": "error", "message": "Invalid JSON"}

    if "message" in data:
        msg = data["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "").strip()

        if not chat_id or not text:
            return {"status": "ok"}

        # Handle commands
        if text.startswith("/start"):
            welcome_text = (
                "👋 <b>Namaste! Main aapka Video Downloader Bot hoon.</b>\n\n"
                "Aap mujhe in platforms ke video links bhej sakte hain:\n"
                "• 🎬 <b>YouTube</b> (Videos & Shorts)\n"
                "• 📸 <b>Instagram</b> (Reels, Posts & Carousels 🆕)\n"
                "• 🐦 <b>Twitter / X</b>\n"
                "• 🎵 <b>Facebook, TikTok aur anya websites!</b>\n\n"
                "🚀 <i>Bas koi bhi video link copy karke yahan bhejiye!</i>"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                await send_message(client, chat_id, welcome_text)
            return {"status": "ok"}

        if text.startswith("/help"):
            help_text = (
                "ℹ️ <b>Kaise use karein:</b>\n\n"
                "1. YouTube ya Instagram par jaakar video ka <b>Share Link</b> copy karein.\n"
                "2. Is chat mein paste karke send karein.\n"
                "3. Bot automatically video download karke aapko bhej dega.\n\n"
                "⚠️ <i>Note: Telegram Bot API limit ki wajah se 50MB se badi videos upload nahi ho sakti.</i>"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                await send_message(client, chat_id, help_text)
            return {"status": "ok"}

        # Search for any valid HTTP/HTTPS link in the message
        match = URL_REGEX.search(text)
        if match:
            video_url = match.group(0)
            if is_youtube_video(video_url):
                # Ask for quality first via inline keyboard
                async with httpx.AsyncClient(timeout=10.0) as client:
                    msg_id = await send_quality_keyboard(client, chat_id, video_url)
                # Store pending download until user picks quality
                pending_downloads[chat_id] = {"url": video_url, "message_id": msg_id}
            elif is_instagram_url(video_url):
                # Instagram: dedicated handler supports carousels & multi-image posts
                background_tasks.add_task(process_instagram_download, chat_id, video_url)
            else:
                # Shorts, TikTok, Twitter, etc. → direct download
                background_tasks.add_task(process_video_download, chat_id, video_url)
        else:
            invalid_text = (
                "❌ <b>Koi valid link nahi mila!</b>\n\n"
                "Kripya ek valid video link bhejein (jaise YouTube Shorts, Video, ya Instagram Reel)."
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                await send_message(client, chat_id, invalid_text)

    elif "callback_query" in data:
        # Handle quality button press
        cq = data["callback_query"]
        cq_id = cq.get("id")
        cq_data = cq.get("data", "")
        cq_chat_id = cq.get("message", {}).get("chat", {}).get("id")
        cq_msg_id = cq.get("message", {}).get("message_id")

        if cq_data.startswith("q_") and cq_chat_id:
            quality_map = {"q_360": "360", "q_480": "480", "q_720": "720", "q_1080": "1080"}
            chosen_quality = quality_map.get(cq_data)
            pending = pending_downloads.pop(cq_chat_id, None)

            async with httpx.AsyncClient(timeout=10.0) as client:
                # Acknowledge the button press
                await answer_callback_query(client, cq_id, f"✅ {chosen_quality}p selected!")
                # Delete the quality-selection message
                if cq_msg_id:
                    try:
                        await client.post(f"{TELEGRAM_API}/deleteMessage", json={
                            "chat_id": cq_chat_id,
                            "message_id": cq_msg_id
                        })
                    except Exception:
                        pass

            if pending and chosen_quality:
                background_tasks.add_task(
                    process_video_download, cq_chat_id, pending["url"], chosen_quality
                )
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await send_message(client, cq_chat_id, "❌ <b>Session expire ho gaya, dobara link bhejein.</b>")

    return {"status": "ok"}


if __name__ == "__main__":
    # pyrefly: ignore [missing-import]
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)