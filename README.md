# MKS – video & audio downloader website

## Run on your computer
1. Install Python 3.10+, ffmpeg (HD video + MP3) and Deno (YouTube support).
   - Windows: `winget install ffmpeg` and `winget install DenoLand.Deno`
   - Ubuntu/Linux: `sudo apt install ffmpeg` and see https://deno.com to install Deno
   Close and reopen Command Prompt after installing.
2. In this folder:
   pip install -r requirements.txt
   python server.py
3. Open http://localhost:5000

## How a download works
Click Download -> the server prepares the file once -> the browser downloads the ready file.
Download managers (IDM etc.) and resume work, and the same video is never fetched twice.
Finished files are deleted automatically 20 minutes after their last use.

## Put it online
Use a VPS that can run Python, ffmpeg and Deno (shared hosting and Blogger cannot).
   gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 server:app
IMPORTANT: keep `-w 1` (one worker). Jobs are kept in memory, so several workers would
lose track of each other's downloads. Threads give you the parallelism.
Put Nginx or Caddy in front with HTTPS. Make sure the disk has room for temporary files.

## Keep it working
Sites change often. Update the downloader engine regularly:
   pip install -U "yt-dlp[default]"

## Test checklist (one public link per site)
- YouTube normal HD video: shows 1080p (and 4K if it has it); video plays with sound.
- YouTube Short: qualities look tidy (no odd sizes like 608p).
- TikTok: public video link (tiktok.com/@user/video/... or vm.tiktok.com/...).
- Facebook: public video or reel (facebook.com/watch, /reel/, fb.watch).
- X / Twitter: public post that contains a video.
- Audio: one MP3 (any bitrate) and one M4A.
- Only ONE "200" line per download in the server window (no repeats).
