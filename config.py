"""Màu sắc, hằng số dùng chung toàn app."""
import sys, shutil, subprocess

# ── Màu ──────────────────────────────────────────────────────────
BG      = "#0f0f13"
PANEL   = "#1a1a24"
CARD    = "#22222f"
ACCENT  = "#7c6af7"
ACCENT2 = "#4fc3f7"
SUCCESS = "#4caf50"
WARN    = "#ff9800"
ERR     = "#f44336"
TEXT    = "#e8e8f0"
SUBTEXT = "#8888aa"

# ── yt-dlp ───────────────────────────────────────────────────────
def get_ytdlp():
    if shutil.which("yt-dlp"): return ["yt-dlp"]
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True)
    if r.returncode == 0: return [sys.executable, "-m", "yt_dlp"]
    return None
