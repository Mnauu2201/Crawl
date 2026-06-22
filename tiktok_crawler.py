"""TikTok Crawler: Lấy danh sách video theo kênh (channel/profile) + tải về.

Khác Facebook: KHÔNG cần Playwright/login để lấy danh sách video công khai.
yt-dlp tự liệt kê được toàn bộ video của 1 kênh TikTok qua --flat-playlist,
nhanh và đơn giản hơn nhiều so với crawl Facebook (vốn phải scroll + mở
từng tab để lấy title vì FB chặn truy cập không qua trình duyệt thật).
"""
import json
import subprocess
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import get_ytdlp, ACCENT2, SUCCESS, WARN, ERR, SUBTEXT
from registry import Registry
from downloader import cookies_for_ytdlp, download_batch


class TikTokCrawler:
    def __init__(self, log, done, progress):
        self.log = log
        self.done = done
        self.progress = progress
        self._stop = False

    def stop(self):
        self._stop = True

    # ── Tiện ích ─────────────────────────────────────────────────────
    @staticmethod
    def _fmt_views(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            return ""
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)

    @staticmethod
    def _video_id_from_url(url):
        """Lấy ID duy nhất từ URL TikTok, dùng để ghi registry.

        Luôn dò lại từ URL (không tin field 'id' rời rạc của yt-dlp) để
        ID dùng lúc crawl() và lúc download() khớp nhau tuyệt đối — cùng
        pattern với FBCrawler._reel_id_from_url.
        """
        if "/video/" in url:
            return url.split("/video/")[-1].split("?")[0].rstrip("/")
        return url  # fallback: dùng cả URL làm id nếu không khớp pattern

    # ── Lấy danh sách video (1 lệnh yt-dlp, không cần Playwright) ─────
    def _list_videos(self, profile_url, max_videos, cookies_file):
        ytdlp = get_ytdlp()
        if not ytdlp:
            raise RuntimeError("yt-dlp không tìm thấy (pip install yt-dlp)")

        cmd = ytdlp + [
            "--flat-playlist", "--dump-single-json",
            "--playlist-end", str(max_videos),
            profile_url,
        ]
        ck = cookies_for_ytdlp(cookies_file)
        if ck:
            cmd += ["--cookies", ck]

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "Lỗi không rõ khi gọi yt-dlp")[-300:])

        data = json.loads(r.stdout)
        entries = data.get("entries") or []
        videos = []
        for e in entries:
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url") or ""
            if not url:
                vid_id = e.get("id")
                uploader = e.get("uploader") or e.get("channel")
                if vid_id and uploader:
                    url = f"https://www.tiktok.com/@{uploader}/video/{vid_id}"
            if not url:
                continue

            title = (e.get("title") or e.get("description") or "").strip()
            title = title[:300] if title else f"TikTok #{len(videos)+1}"

            thumb = ""
            thumbs = e.get("thumbnails") or []
            if thumbs:
                thumb = thumbs[-1].get("url", "")
            if not thumb:
                thumb = e.get("thumbnail") or ""

            videos.append({
                "id": self._video_id_from_url(url),
                "url": url,
                "title": title,
                "thumb": thumb,
                "views": self._fmt_views(e.get("view_count")),
            })
        return videos

    # ── Phase 2: video nào title bị cắt '...' → lấy lại title đầy đủ ──
    @staticmethod
    def _is_truncated(title):
        t = (title or "").rstrip()
        return t.endswith("...") or t.endswith("…")

    def _fetch_full_titles(self, videos, cookies_file, max_concurrent=2):
        """--flat-playlist chỉ đọc preview ngắn từ API danh sách kênh, nên
        TikTok tự cắt caption dài và thêm '...'. Với video nào bị cắt, gọi
        lại yt-dlp lấy đúng trang chi tiết video đó (không flat) để có
        caption đầy đủ — giống cách douyin_worker.py lấy title 1 video.

        Lấy trang chi tiết NẶNG hơn nhiều so với liệt kê nhanh (phải tải +
        parse cả trang), nên timeout phải rộng rãi (45s) và concurrency
        thấp (2 luồng) — tránh vừa bị timeout dồn dập vừa dễ bị TikTok
        chặn khi dội quá nhiều request riêng lẻ cùng lúc. Có retry 1 lần
        nếu lần đầu timeout/lỗi mạng. Mọi lỗi đều LOG RÕ LÝ DO (thay vì
        nuốt im lặng) để có cơ sở chẩn đoán nếu vẫn còn video lỗi.
        """
        need = [v for v in videos if self._is_truncated(v.get("title", ""))]
        if not need:
            return
        n = len(need)
        self.log(f"  {n} video có tiêu đề bị cắt ngắn ('...') — "
                 f"đang lấy tiêu đề đầy đủ ({max_concurrent} luồng)...", ACCENT2)
        ytdlp = get_ytdlp()
        ck = cookies_for_ytdlp(cookies_file)
        done_n = [0]
        fail_reasons = []  # giữ vài lý do lỗi đầu tiên để log debug

        def _try_once(v, timeout, field="title"):
            cmd = ytdlp + ["--print", f"%({field})s", "--skip-download",
                          "--no-warnings", v["url"]]
            if ck:
                cmd += ["--cookies", ck]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                return None, (r.stderr or "")[-150:].strip() or "yt-dlp lỗi không rõ"
            t = (r.stdout or "").strip()
            if not t:
                return None, "stdout rỗng"
            return t, None

        def _one(v):
            if self._stop:
                return
            err = None
            # Thử title 2 lần (timeout tăng dần), cuối cùng thử field
            # 'description' — phòng trường hợp chính field title cũng đã
            # bị TikTok cắt sẵn ở cả trang chi tiết, còn description thì không.
            for attempt, (timeout, field) in enumerate(
                    ((45, "title"), (60, "title"), (60, "description"))):
                if self._stop:
                    return
                try:
                    t, err = _try_once(v, timeout, field)
                    if t and not self._is_truncated(t):
                        v["title"] = t[:500]
                        err = None
                        break
                    elif t:
                        err = f"vẫn bị cắt ({field}): '{t[-30:]}'"
                except subprocess.TimeoutExpired:
                    err = f"timeout {timeout}s ({field})"
                except Exception as e:
                    err = f"exception: {e}"
            if err and len(fail_reasons) < 5:
                fail_reasons.append(f"{v['id'][:14]}: {err}")
            done_n[0] += 1
            self.progress(0.55 + done_n[0] / max(n, 1) * 0.40,
                         f"Lấy tiêu đề đầy đủ {done_n[0]}/{n}...")

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futs = [pool.submit(_one, v) for v in need]
            for _ in as_completed(futs):
                pass

        n_fixed = sum(1 for v in need if not self._is_truncated(v.get("title", "")))
        self.log(f"  ✅ Lấy đầy đủ được {n_fixed}/{n} tiêu đề bị cắt", SUCCESS)
        if fail_reasons:
            self.log("  ⚠ Lý do 1 số video chưa lấy được (debug):", WARN)
            for reason in fail_reasons:
                self.log(f"    {reason}", WARN)

    # ── Entry point: quét kênh ─────────────────────────────────────
    def crawl(self, profile_url, max_videos, cookies_file, result_fn, out_dir=None):
        self.log(f"Quét TikTok: {profile_url[:65]}", ACCENT2)
        self.progress(0.05, "Đang lấy danh sách video...")
        videos = []
        try:
            videos = self._list_videos(profile_url, max_videos, cookies_file)
            self.log(f"Lấy được {len(videos)} video", SUCCESS)

            if videos and not self._stop:
                self.progress(0.55, "Đang kiểm tra tiêu đề bị cắt ngắn...")
                self._fetch_full_titles(videos, cookies_file)

            if out_dir and videos:
                reg = Registry(out_dir, source="tiktok")
                n_marked = reg.mark_downloaded(videos, id_fn=lambda v: v["id"])
                if n_marked:
                    self.log(f"  {n_marked} video đã tải trước đó "
                             f"(đánh dấu ✅, bỏ tick sẵn)", SUBTEXT)
            elif not videos:
                self.log("Không tìm thấy video. Kiểm tra lại URL kênh "
                          "(vd: https://www.tiktok.com/@tenuser).", WARN)
        except Exception as e:
            self.log(f"Lỗi: {e}", ERR)
            self.log(traceback.format_exc(), ERR)

        self.progress(1.0, f"Quét xong: {len(videos)} video")
        result_fn(videos)
        self.done()

    # ── Tải video đã chọn (dùng chung downloader.download_batch) ─────
    def download(self, items, out_dir, cookies_file, max_concurrent=3):
        """items: list[(url, title)] — cùng kiểu app.py truyền cho FBCrawler,
        để GUI có thể tái dùng gần như y nguyên pattern của tab Facebook."""
        out_dir = Path(out_dir)
        reg = Registry(out_dir, source="tiktok")
        dict_items = [
            {"id": self._video_id_from_url(url), "url": url, "title": title}
            for url, title in items
        ]
        ok, n = download_batch(
            dict_items, out_dir,
            id_fn=lambda it: it["id"],
            cookies_file=cookies_file,
            registry=reg,
            max_concurrent=max_concurrent,
            stop_check=lambda: self._stop,
            log=self.log,
            progress=self.progress,
        )
        self.log(f"\nTải xong: {ok}/{n} video", SUCCESS if ok == n else WARN)
        self.progress(1.0, f"Hoàn tất {ok}/{n}")
        self.done()