import os
import re
import shutil
import asyncio
import tempfile
import logging
from typing import Optional, Dict, Any

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, Request, BackgroundTasks
# pyrefly: ignore [missing-import]
from fastapi.responses import HTMLResponse
# pyrefly: ignore [missing-import]
import httpx
import yt_dlp
 
from contextlib import asynccontextmanager
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Configurable environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "8061236263:AAEn1Kl3ZwA_JV5qc_lPNAo6sRiO-MH5ic0"
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB Telegram Bot API limit


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan manager for FastAPI.
    Automatically sets the Telegram webhook on server startup if WEBHOOK_URL is provided.
    """
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


def _sync_download(url: str, temp_dir: str) -> Dict[str, Any]:
    """
    Synchronous worker running yt-dlp download in a background thread.
    Returns metadata dict with filepath, title, duration, and file size.
    """
    outtmpl = os.path.join(temp_dir, "video_%(id)s.%(ext)s")

    ydl_opts = {
        # Format prioritizing standard resolutions under Telegram's 50MB limit
        'format': 'bestvideo[filesize_approx<=45M][ext=mp4]+bestaudio[ext=m4a]/bestvideo[filesize_approx<=45M]+bestaudio/best[filesize_approx<=48M]/bestvideo[height<=720]+bestaudio/best[height<=720]/best',
        'outtmpl': outtmpl,
        'merge_output_format': 'mp4',
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
        'extractor_args': {
            'youtube': {
                # Use android client to bypass 403 Forbidden issues on YouTube
                'player_client': ['android', 'web_safari']
            }
        },
        'http_chunk_size': 5242880,  # 5MB chunks to bypass progressive download throttle
        'concurrent_fragment_downloads': 5,
        'remote_components': ['ejs:github'],
        'js_runtimes': {'node': {}},
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise ValueError("No video information could be retrieved.")

        # Find the downloaded file in temp_dir
        found_files = [
            os.path.join(temp_dir, f)
            for f in os.listdir(temp_dir)
            if not f.endswith(('.part', '.ytdl')) and os.path.isfile(os.path.join(temp_dir, f))
        ]

        if not found_files:
            raise FileNotFoundError("Video file was not created by yt-dlp.")

        # Pick the largest file in the folder (usually the final merged mp4)
        target_file = max(found_files, key=os.path.getsize)

        return {
            "file_path": target_file,
            "title": info.get("title", "Video"),
            "duration": info.get("duration", 0),
            "width": info.get("width"),
            "height": info.get("height"),
            "file_size": os.path.getsize(target_file),
            "webpage_url": info.get("webpage_url", url)
        }


async def action_ticker(client: httpx.AsyncClient, chat_id: int, action: str, stop_ev: asyncio.Event):
    """Periodically sends Telegram chat action while an operation is running."""
    while not stop_ev.is_set():
        await send_chat_action(client, chat_id, action)
        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def process_video_download(chat_id: int, video_url: str):
    """
    Background worker that handles downloading the video and sending it to Telegram.
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
            logger.info(f"Downloading video from {video_url} for chat {chat_id}...")
            video_data = await asyncio.to_thread(_sync_download, video_url, temp_dir)

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

            # Update status message
            if status_msg_id:
                await edit_message(
                    client, chat_id, status_msg_id,
                    "📤 <b>Download complete! Telegram par video bhej raha hoon...</b>"
                )

            await send_chat_action(client, chat_id, "upload_video")

            # Prepare caption
            caption = f"🎬 <b>{title[:300]}</b>"
            if duration:
                mins, secs = divmod(int(duration), 60)
                caption += f"\n⏱ <i>Duration: {mins:02d}:{secs:02d}</i>"
            caption += "\n\n🤖 <i>Downloaded via @AniketVideo_bot</i>"

            # Upload video directly using multipart/form-data
            with open(file_path, "rb") as f:
                filename = os.path.basename(file_path)
                files = {
                    "video": (filename, f, "video/mp4")
                }
                data = {
                    "chat_id": str(chat_id),
                    "caption": caption,
                    "parse_mode": "HTML",
                    "supports_streaming": "true"
                }
                if duration:
                    data["duration"] = str(duration)
                if video_data.get("width"):
                    data["width"] = str(video_data["width"])
                if video_data.get("height"):
                    data["height"] = str(video_data["height"])

                upload_resp = await client.post(
                    f"{TELEGRAM_API}/sendVideo",
                    data=data,
                    files=files
                )

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
            err_text = (
                "❌ <b>Video download nahi ho paya!</b>\n\n"
                "Kripya check karein ki:\n"
                "• Link sahi aur publicly accessible hai.\n"
                "• Video private ya age-restricted toh nahi hai."
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
                "• 📸 <b>Instagram</b> (Reels & Posts)\n"
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
            # Add video processing to background tasks
            background_tasks.add_task(process_video_download, chat_id, video_url)
        else:
            invalid_text = (
                "❌ <b>Koi valid link nahi mila!</b>\n\n"
                "Kripya ek valid video link bhejein (jaise YouTube Shorts, Video, ya Instagram Reel)."
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                await send_message(client, chat_id, invalid_text)

    return {"status": "ok"}


if __name__ == "__main__":
    # pyrefly: ignore [missing-import]
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)