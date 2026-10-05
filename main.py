import os
import re
import time
import requests
from urllib.parse import urlparse
import yt_dlp
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
COUNTER_FILE = "counter.txt"

# Cấu hình các file cookie riêng biệt theo nền tảng
BASE_DIR = os.path.dirname(__file__)
COOKIE_FILES = {
    "douyin": os.path.join(BASE_DIR, "douyin-cookies.txt"),
    "youtube": os.path.join(BASE_DIR, "youtube-cookies.txt"),
}

def get_next_id() -> str:
    count = 1
    if os.path.exists(COUNTER_FILE):
        try:
            with open(COUNTER_FILE, "r") as f:
                content = f.read().strip()
                if content:
                    count = int(content) + 1
        except Exception:
            count = 1
            
    with open(COUNTER_FILE, "w") as f:
        f.write(str(count))
        
    return f"{count:010d}"

def resolve_url(url: str) -> str:
    """Giải nén link rút gọn Douyin/TikTok"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        }
        res = requests.get(url, allow_redirects=True, headers=headers, timeout=10)
        return res.url
    except Exception:
        return url

def extract_platform_name(url: str) -> str:
    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc or parsed_url.path
        domain = re.sub(r'^(www\.|m\.|mobile\.|vt\.|v\.)', '', domain.lower())
        platform = domain.split('.')[0]
        
        special_cases = {
            "youtu": "youtube",
            "fb": "facebook",
            "instagram": "instagram",
            "tiktok": "tiktok",
            "douyin": "douyin"
        }
        return special_cases.get(platform, platform if platform else "video")
    except Exception:
        return "video"

def get_cookie_file_for_url(url: str) -> str | None:
    """Xác định và trả về đường dẫn file cookie phù hợp theo nền tảng"""
    platform = extract_platform_name(url)
    cookie_path = COOKIE_FILES.get(platform)
    
    if cookie_path and os.path.exists(cookie_path):
        return cookie_path
    return None

def format_count(count: int) -> str:
    if not count and count != 0:
        return None
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)

def format_duration(seconds: int) -> str:
    """Chuyển đổi số giây thành định dạng MM:SS hoặc HH:MM:SS"""
    if not seconds:
        return None
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"    

def cleanup_old_files(folder_path: str, max_age_seconds: int = 900):
    """
    Tự động quét và xóa các file trong folder_path cũ hơn max_age_seconds.
    Mặc định 900 giây = 15 phút.
    """
    try:
        now = time.time()
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                file_age = now - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    try:
                        os.remove(file_path)
                        print(f"[CLEANUP] Đã xóa file cũ: {filename}")
                    except Exception as e:
                        print(f"[CLEANUP] Lỗi xóa file {filename}: {e}")
    except Exception as e:
        print(f"[CLEANUP] Lỗi khi quét thư mục dọn dẹp: {e}")

class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str

@app.post("/api/extract")
async def extract_video_info(data: URLRequest):
    target_url = resolve_url(data.url)
    platform = extract_platform_name(target_url)
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
    }

    # Thêm cấu hình giả lập client Android cho YouTube để giảm tỷ lệ bị chặn IP
    if platform == "youtube":
        ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'web']}}

    # Tự động gán file cookie phù hợp (douyin-cookies.txt hoặc youtube-cookies.txt)
    cookie_file = get_cookie_file_for_url(target_url)
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            
            video_formats = []
            seen_heights = set()
            best_audio = None
            max_audio_bitrate = 0

            for f in info.get('formats', []):
                if f.get('protocol') in ['m3u8_native', 'm3u8'] or not f.get('url'):
                    continue

                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                height = f.get('height')
                width = f.get('width')

                if vcodec == 'none' and acodec != 'none':
                    abr = f.get('abr', 0) or 0
                    if abr > max_audio_bitrate:
                        max_audio_bitrate = abr
                        best_audio = {
                            'format_id': f.get('format_id'),
                            'ext': 'mp3',
                            'resolution': 'Best Audio',
                            'filesize_mb': round(f.get('filesize', 0) / (1024 * 1024), 2) if f.get('filesize') else None,
                            'download_url': f.get('url'),
                            'label': 'MP3 - Audio Only'
                        }

                elif vcodec != 'none' and height and height >= 240:
                    if height not in seen_heights:
                        seen_heights.add(height)
                        res_label = f"{width}x{height}" if width else f"{height}p"

                        video_formats.append({
                            'height': height,
                            'format_id': f.get('format_id'),
                            'ext': 'mp4',
                            'resolution': res_label,
                            'filesize_mb': round(f.get('filesize', 0) / (1024 * 1024), 2) if f.get('filesize') else None,
                            'download_url': f.get('url'),
                            'label': f"MP4 - {res_label}"
                        })

            video_formats.sort(key=lambda x: x['height'], reverse=True)

            final_formats = video_formats
            if best_audio:
                final_formats.append(best_audio)

            uploader = info.get('uploader') or info.get('uploader_id') or info.get('channel') or "N/A"
            if uploader != "N/A" and not uploader.startswith("@"):
                uploader = "@" + uploader

            return {
                "success": True,
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "uploader": uploader,
                "platform": platform.upper(),
                "duration": format_duration(info.get('duration')),
                "views": format_count(info.get('view_count')),
                "likes": format_count(info.get('like_count')),
                "comments": format_count(info.get('comment_count')),
                "formats": final_formats
            }
            
    except Exception as e:
        clean_error = re.sub(r'\x1b\[[0-9;]*m', '', str(e))
        raise HTTPException(status_code=400, detail=clean_error)

@app.post("/api/merge")
async def merge_and_download(data: DownloadRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(cleanup_old_files, DOWNLOAD_DIR, 900)

    target_url = resolve_url(data.url)
    platform = extract_platform_name(target_url)
    next_id = get_next_id()
    filename = f"{platform}_{next_id}.mp4"
    output_path = os.path.join(DOWNLOAD_DIR, filename)

    ydl_opts = {
        'format': 'bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
    }

    if platform == "youtube":
        ydl_opts['extractor_args'] = {'youtube': {'player_client': ['android', 'web']}}

    cookie_file = get_cookie_file_for_url(target_url)
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target_url])
            
        return FileResponse(
            path=output_path,
            filename=filename,
            media_type='video/mp4',
            headers={"Access-Control-Expose-Headers": "Content-Disposition"}
        )
    except Exception as e:
        clean_error = re.sub(r'\x1b\[[0-9;]*m', '', str(e))
        raise HTTPException(status_code=500, detail=f"Lỗi khi ghép video: {clean_error}")

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
