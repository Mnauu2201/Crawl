import sys, json, time, io, threading, subprocess, asyncio, shutil
from pathlib import Path
from config import *
 
try:
    from groq_client import GroqClient, GroqAllKeysFailedError
    _GROQ_AVAILABLE = True
except ImportError:
    _GROQ_AVAILABLE = False
 
# ═══════════════════════════════════════════════════════════
# Hàm hậu kỳ — ported nguyên từ main.py, không đổi logic
# ═══════════════════════════════════════════════════════════
 
def _blur_subs(video_path, output_path, blur_top_pct=0.72, blur_bot_pct=0.92):
    """Làm mờ đúng vùng sub tiếng Trung trên Douyin.
 
    blur_top_pct: vị trí trên của vùng blur (tỉ lệ 0.0–1.0 chiều cao video)
    blur_bot_pct: vị trí dưới của vùng blur (tỉ lệ 0.0–1.0 chiều cao video)
 
    Mặc định 0.72–0.92 (72%–92%) phù hợp với sub 2 dòng phía dưới Douyin.
    Chỉnh qua GUI nếu video cụ thể có sub ở vị trí khác.
    """
    SUB_TOP_PCT = max(0.0, min(0.99, blur_top_pct))
    SUB_BOT_PCT = max(SUB_TOP_PCT + 0.01, min(1.0, blur_bot_pct))
 
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_type", "-of", "json",
        str(video_path)
    ], capture_output=True, text=True)
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
 
    # Nếu không tìm thấy video stream → file là audio-only, không thể blur
    if not streams or not streams[0].get("width"):
        raise RuntimeError(
            f"File không có video stream (audio-only?): {video_path.name}\n"
            "Playwright có thể đã tải nhầm URL audio. Thử lại để bắt đúng URL.")
 
    w = streams[0]["width"]
    h = streams[0]["height"]
 
    blur_y = int(h * SUB_TOP_PCT)
    blur_h = int(h * (SUB_BOT_PCT - SUB_TOP_PCT))
 
    # filter_complex bắt buộc (không dùng -vf) vì có 2 input stream:
    # [0:v] làm nền, [blurred] vùng crop+blur làm lớp phủ overlay.
    flt = (f"[0:v]crop={w}:{blur_h}:0:{blur_y},"
           f"boxblur=luma_radius=20:luma_power=2[blurred];"
           f"[0:v][blurred]overlay=0:{blur_y}[vout]")
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-filter_complex", flt, "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy", str(output_path)
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Lỗi blur sub:\n{r.stderr[-300:]}")
    return output_path
 
_TTS_VOICES = {"nu": "vi-VN-HoaiMyNeural", "nam": "vi-VN-NamMinhNeural"}
 
async def _gen_tts_segment(text, voice, out_path):
    import edge_tts
    await edge_tts.Communicate(text, voice).save(str(out_path))
 
def _run_tts_sync(text, voice, out_path, retries=3):
    """Chạy edge-tts trong thread riêng với event loop mới.
    asyncio.run() không dùng được trong GUI thread vì event loop đã tồn tại
    → tạo loop mới trong thread daemon để tránh xung đột.
 
    retries=3: giọng nam vi-VN-NamMinhNeural đôi khi trả file rỗng 0 bytes
    → tự retry tối đa 3 lần trước khi bỏ qua segment đó.
    """
    import concurrent.futures
    from pathlib import Path as _P
    for attempt in range(retries):
        def _in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_gen_tts_segment(text, voice, out_path))
            finally:
                loop.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_in_thread)
            fut.result(timeout=30)
        # Kiểm tra file có thực sự có dữ liệu không
        p = _P(out_path)
        if p.exists() and p.stat().st_size > 0:
            return  # thành công
        # File rỗng → xóa và thử lại
        p.unlink(missing_ok=True)
        time.sleep(0.5 * (attempt + 1))
    # Hết retry vẫn rỗng → raise để caller bỏ qua segment này
    raise RuntimeError(f"edge-tts trả file rỗng sau {retries} lần thử "
                       f"(voice={voice}, text={text[:30]!r})")
 
def _parse_srt_for_tts(srt_path):
    """Đọc file .srt đã có và trả về list[{"start", "end", "vi"}] —
    đúng format mà _generate_tts() cần, để Phase 2 có thể chạy TTS mà
    không cần gọi lại Whisper hay dịch."""
    import re as _re
    txt = Path(srt_path).read_text(encoding="utf-8")
    blocks = _re.split(r"\n\s*\n", txt.strip())
    segs = []
    ts_pat = _re.compile(
        r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        # Tìm dòng timestamp
        ts_line = next((l for l in lines if "-->" in l), None)
        if not ts_line:
            continue
        m = ts_pat.search(ts_line)
        if not m:
            continue
        h1,m1,s1,ms1, h2,m2,s2,ms2 = (int(x) for x in m.groups())
        start = h1*3600 + m1*60 + s1 + ms1/1000
        end   = h2*3600 + m2*60 + s2 + ms2/1000
        # Lấy tất cả dòng text sau dòng timestamp (bỏ số thứ tự đầu)
        ts_idx = lines.index(ts_line)
        vi_text = " ".join(lines[ts_idx+1:]).strip()
        if vi_text:
            segs.append({"start": start, "end": end, "vi": vi_text})
    return segs
 
 
def _generate_tts(translated, out_dir, voice_key="nu"):
    """Tạo file audio TTS tiếng Việt, đặt đúng vị trí thời gian."""
    voice = _TTS_VOICES.get(voice_key, _TTS_VOICES["nu"])
    tmp_dir = out_dir / "_tts_tmp"
    tmp_dir.mkdir(exist_ok=True)
    total_dur = max(seg["end"] for seg in translated) + 1.0
 
    # Tạo file silent làm nền
    silent = tmp_dir / "silent.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono",
                    "-t", str(total_dur), "-acodec", "pcm_s16le",
                    str(silent)], capture_output=True)
 
    # Tạo từng segment TTS
    seg_files = []
    for i, seg in enumerate(translated):
        txt = seg["vi"].strip()
        if not txt:
            continue
        sp = tmp_dir / f"seg_{i:04d}.mp3"
        try:
            _run_tts_sync(txt, voice, sp)
            if sp.exists() and sp.stat().st_size > 0:
                seg_files.append((seg["start"], sp))
            time.sleep(0.05)
        except Exception:
            pass
 
    if not seg_files:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("Không tạo được file TTS nào (edge-tts đã cài chưa?)")
 
    # Gộp tất cả segment vào 1 track với đúng offset thời gian.
    # QUAN TRỌNG: phải có dấu cách trước "amix" — ffmpeg cần tách
    # label [dN] khỏi tên filter, thiếu dấu cách → parse sai → output silent.
    inputs = ["-i", str(silent)]
    filter_parts = []
    for idx, (start_t, sp) in enumerate(seg_files):
        inputs += ["-i", str(sp)]
        ms = int(start_t * 1000)
        filter_parts.append(f"[{idx+1}:a]adelay={ms}|{ms}[d{idx}]")
 
    delay_labels = "".join(f"[d{i}]" for i in range(len(seg_files)))
    n_inputs = len(seg_files) + 1
    flt = (";".join(filter_parts) +
           f";[0:a]{delay_labels} amix=inputs={n_inputs}:"
           f"duration=first:dropout_transition=0:normalize=0[aout]")
 
    tts_final = out_dir / "_tts_final.wav"
    r = subprocess.run(["ffmpeg", "-y"] + inputs +
                       ["-filter_complex", flt, "-map", "[aout]",
                        "-acodec", "pcm_s16le", "-ar", "44100",
                        str(tts_final)], capture_output=True, text=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if r.returncode != 0:
        raise RuntimeError(f"Lỗi gộp TTS:\n{r.stderr[-300:]}")
    if not tts_final.exists() or tts_final.stat().st_size == 0:
        raise RuntimeError("File TTS output rỗng — ffmpeg filter lỗi âm thầm")
    return tts_final
 
def _mix_tts(video_path, tts_audio, output_path, orig_vol=0.15):
    """Gộp TTS vào video, giữ nhẹ âm gốc."""
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(video_path), "-i", str(tts_audio),
        "-filter_complex",
        f"[0:a]volume={orig_vol}[orig];[1:a]volume=2.0[tts];[orig][tts]amix=inputs=2:duration=first:normalize=0[aout]",
        "-map", "0:v?", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        str(output_path)
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Lỗi mix TTS:\n{r.stderr[-500:]}")
    return output_path
 
def _burn_subs(video_path, srt_path, output_path, font_size=22, margin_v=25):
    """Burn phụ đề tiếng Việt cứng vào video.
 
    QUAN TRỌNG: ffmpeg subtitles filter tự convert .srt sang .ass nội bộ
    và LUÔN gán PlayResY=288 mặc định (cố định trong libavfilter, không
    phụ thuộc kích thước video thật — xem vf_subtitles.c). Nếu không
    chỉ định original_size, libass coi FontSize/MarginV là số đo trên
    canvas ảo 288px rồi MỚI scale lên video thật. Với video dọc Douyin
    cao ~1920px, hệ số scale ngầm này là 1920/288 ≈ 6.7× — khiến
    FontSize=80 mà người dùng nhập thực ra render ra ~533px, to gấp
    nhiều lần so với preview và so với con số người dùng thấy trên UI.
    Truyền original_size=WxH (kích thước video thật, lấy bằng ffprobe)
    để libass dùng đúng canvas thật — FontSize/MarginV giờ đúng nghĩa
    đen là số pixel trên video thật, khớp 1:1 với preview.
    """
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json",
        str(video_path)
    ], capture_output=True, text=True)
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    if not streams or not streams[0].get("width"):
        raise RuntimeError(f"Không đọc được kích thước video: {video_path.name}")
    vid_w, vid_h = streams[0]["width"], streams[0]["height"]
 
    style = ("FontName=Arial,"
             f"FontSize={font_size},"
             "PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,"
             f"BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV={margin_v}")
    srt_str = str(srt_path.resolve()).replace("\\", "/")
    if sys.platform == "win32":
        srt_str = srt_str.replace(":", "\\:")
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", (f"subtitles='{srt_str}':force_style='{style}'"
                f":original_size={vid_w}x{vid_h}"),
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "copy",
        str(output_path)
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Lỗi burn sub:\n{r.stderr[-300:]}")
    return output_path
 
class DouyinWorker:
    STEPS = ["⬇ Tải", "🎵 Audio", "🤖 Whisper", "🌐 Dịch", "💾 SRT",
             "🎬 Mờ sub", "🎙 TTS", "🔥 Burn"]
 
    def __init__(self, log, done, progress, step, on_video_ready=None):
        self.log = log; self.done = done
        self.progress = progress; self.step = step
        self._stop = False
        # Callback tuỳ chọn: gọi ngay khi video tải xong (TRƯỚC khi vào
        # audio/whisper/dịch) — cho phép app.py cập nhật preview bằng
        # frame thật ngay lập tức, để user chỉnh blur/sub trong lúc
        # pipeline vẫn đang chạy phía sau, không phải đợi tới hết video
        # rồi mới biết mình chỉnh sai % (đây là khoảng trống thật khiến
        # preview luôn trễ hơn lúc cần — video tải xong sớm nhưng preview
        # vẫn đứng yên ở placeholder/frame fetch riêng có thể đã fail).
        self.on_video_ready = on_video_ready or (lambda video_path: None)
 
    def stop(self): self._stop = True
 
    # ── Lấy 1 frame thật từ link Douyin để hiển thị preview ─────────
    # Dùng lại logic bắt URL video qua Playwright (network sniffing),
    # nhưng KHÔNG tải cả file — ffmpeg đọc trực tiếp từ URL stream và
    # chỉ rút đúng 1 frame, nhanh hơn nhiều so với tải full video chỉ
    # để xem trước vị trí blur/sub.
    def get_preview_frame(self, page_url, out_jpg_path, timeout_sec=20):
        """Trả về (True, None) nếu lấy được frame, (False, lý_do) nếu không.
        Ghi JPEG ra out_jpg_path."""
        try:
            vid_url, _audio_url = self._playwright_get_video_url(
                page_url, timeout_sec=timeout_sec)
        except Exception as e:
            return False, f"Playwright lỗi: {e}"
        if not vid_url:
            return False, "Không bắt được URL video (có thể cần cookies)"
 
        r = subprocess.run([
            "ffmpeg", "-y",
            "-headers", "Referer: https://www.douyin.com/\r\n",
            "-user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "-ss", "00:00:01", "-i", vid_url,
            "-vframes", "1", "-q:v", "2", str(out_jpg_path),
        ], capture_output=True, text=True, timeout=30)
 
        if r.returncode != 0 or not Path(out_jpg_path).exists():
            err = (r.stderr or "")[-300:].strip() or "ffmpeg lỗi không rõ"
            return False, err
        return True, None
 
    # ── Helper: Playwright bắt URL video thật từ network requests ──────
    # Khi Douyin thay đổi API → chỉ cần kiểm tra lại DOUYIN_VIDEO_PATTERNS
    # bên dưới, không cần sửa gì khác trong class.
    DOUYIN_VIDEO_PATTERNS = [
        "aweme/v1/play",   # API endpoint chính của Douyin
        "video/tos",       # CDN TOS (ByteDance)
        "v26-web.douyinvod.com",  # CDN mới (2024+)
        "v3-web.douyinvod.com",
        "v19-web.douyinvod.com",
    ]
    # Pattern của URL audio-only — cần loại trừ để tránh bắt nhầm
    DOUYIN_AUDIO_ONLY_PATTERNS = [
        "audio_mp4",
        "audio-only",
        "audio_only",
        "aac",
    ]
 
    def _playwright_get_video_url(self, page_url, timeout_sec=30):
        """Mở Douyin bằng Chromium, bắt URL file video từ network.
        Trả về URL (str) hoặc None nếu không bắt được.
 
        Douyin ngày càng thường xuyên tách video và audio thành 2 stream
        riêng (video-only mp4 + audio-only mp4). Hàm này bắt cả hai và
        trả về tuple (video_url, audio_url) nếu phát hiện split-stream,
        hoặc (combined_url, None) nếu chỉ có 1 URL mux đầy đủ.
        Caller (_one) sẽ mux lại bằng ffmpeg nếu audio_url != None.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.log("  ⚠ playwright chưa cài (pip install playwright && "
                     "python -m playwright install chromium)", WARN)
            return None, None
 
        video_candidates = []   # (url, size) — có video stream
        audio_candidates = []   # (url, size) — audio-only
 
        def _get_size(resp):
            """Lấy kích thước thực từ content-length hoặc content-range.
            Douyin dùng range requests → header là 'content-range: bytes X-Y/TOTAL'
            nên phải parse TOTAL, không phải bytes-in-this-response."""
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > 0:
                return int(cl)
            cr = resp.headers.get("content-range", "")
            # format: "bytes 0-1023/1048576" → lấy phần sau dấu /
            if "/" in cr:
                total = cr.split("/")[-1].strip()
                if total.isdigit() and int(total) > 0:
                    return int(total)
            return 0
 
        def _is_audio_only(url, resp):
            url_l = url.lower()
            if any(p in url_l for p in self.DOUYIN_AUDIO_ONLY_PATTERNS):
                return True
            ct = resp.headers.get("content-type", "").lower()
            # audio/mp4 hoặc audio/aac → audio-only
            if ct.startswith("audio/"):
                return True
            return False
 
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/136.0.0.0 Safari/537.36",
                    locale="zh-CN",
                    extra_http_headers={"Referer": "https://www.douyin.com/"},
                )
                page = ctx.new_page()
 
                def on_response(resp):
                    url = resp.url
                    if not any(p in url for p in self.DOUYIN_VIDEO_PATTERNS):
                        return
                    ct = (resp.headers.get("content-type") or "").lower()
                    # Phải là video/* hoặc audio/* hoặc octet-stream, hoặc URL kết thúc .mp4
                    if not ("video" in ct or "audio" in ct or "octet" in ct
                            or url.lower().endswith(".mp4")):
                        return
                    size = _get_size(resp)
                    if _is_audio_only(url, resp):
                        if not any(u == url for u, _ in audio_candidates):
                            audio_candidates.append((url, size))
                    else:
                        if not any(u == url for u, _ in video_candidates):
                            video_candidates.append((url, size))
 
                page.on("response", on_response)
                try:
                    page.goto(page_url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
 
                deadline = time.time() + timeout_sec
                while time.time() < deadline and not self._stop:
                    page.wait_for_timeout(500)
                    # Dừng sớm nếu đã có video URL kích thước > 5 MB
                    if any(cl > 5_000_000 for _, cl in video_candidates):
                        break
 
                browser.close()
        except Exception as e:
            self.log(f"  ⚠ Playwright lỗi: {e}", WARN)
            return None, None
 
        if not video_candidates:
            if not audio_candidates:
                return None, None
            # Chỉ bắt được audio → fail
            self.log("  ⚠ Playwright chỉ bắt được audio URL, không có video", WARN)
            return None, None
 
        # Loại video URL có size=0 (không tính được) nếu có URL khác tốt hơn
        real_video = [(u, cl) for u, cl in video_candidates if cl > 0]
        if not real_video:
            real_video = video_candidates  # fallback: dùng hết, không loại
 
        best_video_url, best_cl = max(real_video, key=lambda x: x[1])
 
        # Audio URL — chọn lớn nhất nếu có
        best_audio_url = None
        if audio_candidates:
            real_audio = [(u, cl) for u, cl in audio_candidates if cl > 0] or audio_candidates
            best_audio_url, _ = max(real_audio, key=lambda x: x[1])
 
        n_total = len(video_candidates) + len(audio_candidates)
        mode = "video+audio split" if best_audio_url else "muxed"
        self.log(f"  📊 Playwright bắt được {n_total} URL "
                 f"({mode}, video {best_cl//1024//1024} MB)", SUBTEXT)
        return best_video_url, best_audio_url
 
    def _download_direct(self, video_url, out_path, referer):
        """Tải file video trực tiếp bằng requests (streaming).
        Trả về Path của file đã tải, hoặc None nếu lỗi.
        """
        try:
            import requests as _req
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/136.0.0.0 Safari/537.36",
                "Referer": referer,
                "Accept": "*/*",
            }
            with _req.get(video_url, headers=headers, stream=True,
                          timeout=60, allow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                got = 0
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024*64):
                        if self._stop: return None
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = got / total * 100
                            self.progress(pct/100*0.13+0.01,
                                         f"Tải qua Playwright... {pct:.0f}%")
            p = Path(out_path)
            if not p.exists():
                self.log("  ⚠ Tải trực tiếp: file không tồn tại sau khi tải", WARN)
                return None
            size = p.stat().st_size
            # Video Douyin thật luôn >100KB. File nhỏ hơn = HTML lỗi,
            # JSON error response, hoặc manifest rỗng bị ghi nhầm thành
            # .mp4 — đây là nguyên nhân gốc khiến pipeline nhận "video
            # 0 MB" rồi đưa thẳng vào ffmpeg, ffmpeg fail âm thầm, và
            # Whisper sau đó báo "No such file or directory" trên .wav
            # chưa từng được tạo ra.
            MIN_VALID_SIZE = 100_000
            if size < MIN_VALID_SIZE:
                self.log(f"  ⚠ Tải trực tiếp: file chỉ {size} byte "
                         "(không phải video thật) — xoá và báo thất bại", WARN)
                p.unlink(missing_ok=True)
                return None
            return p
        except Exception as e:
            self.log(f"  ⚠ Tải trực tiếp lỗi: {e}", WARN)
            return None
 
    # ── Bước 2–8: xử lý sau khi đã có file video ───────────────────────
    def _process_video(self, vid, out_dir, wav=None, **kw):
        """Nhận file video đã tải → Whisper → Dịch → SRT → hậu kỳ.
        Tách ra để cả nhánh yt-dlp lẫn nhánh Playwright đều gọi được.
        kw: model, use_groq, groq_client, do_blur, do_tts, do_burn, voice, orig_vol
        """
        model      = kw.get("model", "base")
        use_groq   = kw.get("use_groq", False)
        groq_client= kw.get("groq_client")
        do_blur    = kw.get("do_blur", False)
        do_tts     = kw.get("do_tts", False)
        do_burn    = kw.get("do_burn", False)
        voice      = kw.get("voice", "nu")
        orig_vol   = kw.get("orig_vol", 0.15)
        blur_top_pct = kw.get("blur_top_pct", 0.72)
        blur_bot_pct = kw.get("blur_bot_pct", 0.92)
        margin_v   = kw.get("margin_v", 25)
        font_size  = kw.get("font_size", 22)
 
        # 2. AUDIO
        self.step(1); self.progress(0.15, "Tách audio...")
        self.log("🎵 Tach audio...", ACCENT2)
        # Luôn dùng absolute path để tránh ffmpeg/Groq resolve sai
        # khi working directory khác out_dir (thường xảy ra trên Windows)
        vid = vid.resolve()
        if not vid.exists() or vid.stat().st_size == 0:
            raise RuntimeError(
                f"File video không tồn tại hoặc rỗng: {vid} — bước tải "
                "video trước đó đã thất bại âm thầm (kiểm tra log tải).")
        wav = (out_dir / (vid.stem + ".wav")).resolve()
        r = subprocess.run(["ffmpeg", "-y", "-i", str(vid),
                        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                        str(wav)], capture_output=True, text=True)
        # KHÔNG được báo "thành công" chỉ vì subprocess chạy xong — phải
        # check return code + file thực sự được ghi ra, không sẽ rơi vào
        # đúng bug đã xảy ra: log "✅ Tach audio xong" trong khi .wav chưa
        # bao giờ được tạo, rồi Whisper crash với lỗi khó hiểu ở bước sau.
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            err = (r.stderr or "")[-500:].strip() or "không rõ lý do"
            raise RuntimeError(f"Tách audio thất bại (ffmpeg rc={r.returncode}): {err}")
        self.log("  ✅ Tach audio xong", SUCCESS)
 
        # 3. WHISPER
        self.step(2); self.progress(0.20, "Đang nhận dạng giọng nói...")
        segs = None
 
        if use_groq and groq_client:
            self.log("🤖 Whisper qua Groq API (cloud)...", ACCENT2)
            t0 = time.time()
            try:
                segs = groq_client.transcribe(
                    str(wav), language="zh",
                    log=lambda m: self.log(f"  {m}", WARN))
                self.progress(0.57, f"Groq Whisper xong: {len(segs)} đoạn ✅")
                self.log(f"  ✅ {len(segs)} doan (Groq, {time.time()-t0:.1f}s)", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Groq transcribe lỗi ({e}) — chuyển sang "
                         "Whisper local (offline)...", WARN)
                segs = None
 
        if segs is None:
            self.progress(0.20, "Whisper đang tải model...")
            self.log(f"🤖 Whisper local ({model})...", ACCENT2)
            dur_r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                    "format=duration", "-of",
                                    "default=noprint_wrappers=1:nokey=1", str(wav)],
                                   capture_output=True, text=True)
            try: aud_dur = float(dur_r.stdout.strip())
            except: aud_dur = 0
            speed = {"tiny": 40, "base": 15, "small": 8, "medium": 4, "large": 2}
            est = aud_dur / speed.get(model, 10) if aud_dur > 0 else 120
 
            import whisper as _w
            _ev = threading.Event(); _res = [None, None]
            def _wrun():
                try:
                    _res[0] = _w.load_model(model).transcribe(
                        str(wav), language="zh", task="transcribe", verbose=False)
                except Exception as e: _res[1] = e
                finally: _ev.set()
            threading.Thread(target=_wrun, daemon=True).start()
            old_err = sys.stderr; sys.stderr = io.StringIO()
            ws = time.time()
            while not _ev.wait(timeout=0.5):
                if self._stop: sys.stderr = old_err; _ev.wait(); return False
                el = time.time() - ws; fr = min(el / max(est, 1), 0.97)
                bar = "█" * int(fr*18) + "░" * (18 - int(fr*18))
                self.progress(0.20 + fr*0.35, f"Whisper [{bar}] {int(fr*100)}%")
            sys.stderr = old_err
            if _res[1]: raise _res[1]
            segs = _res[0].get("segments", [])
            self.progress(0.57, f"Whisper xong: {len(segs)} đoạn ✅")
            self.log(f"  ✅ {len(segs)} doan (local)", SUCCESS)
 
        wav.unlink(missing_ok=True)
 
        # 4. DỊCH
        self.step(3); self.progress(0.58, "Bắt đầu dịch...")
        translated = []
 
        if use_groq and groq_client:
            self.log("🌐 Dịch qua Groq LLM (cả batch, có ngữ cảnh)...", ACCENT2)
            t0 = time.time()
            try:
                texts = [seg["text"].strip() for seg in segs if seg["text"].strip()]
                vi_list = groq_client.translate_batch(
                    texts, source_lang="zh", target_lang="vi",
                    log=lambda m: self.log(f"  {m}", WARN))
                idx = 0
                for seg in segs:
                    txt = seg["text"].strip()
                    if not txt: continue
                    translated.append({"start": seg["start"], "end": seg["end"],
                                       "zh": txt, "vi": vi_list[idx]})
                    idx += 1
                self.progress(0.93, f"Groq dịch xong {len(translated)} đoạn")
                self.log(f"  ✅ Dich xong {len(translated)} doan "
                         f"(Groq, {time.time()-t0:.1f}s)", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Groq dịch lỗi ({e}) — chuyển sang "
                         "GoogleTranslator (offline-ish, dịch từng câu)...", WARN)
                translated = []
 
        if not translated:
            self.log("🌐 Dich qua GoogleTranslator (từng câu)...", ACCENT2)
            from deep_translator import GoogleTranslator
            tr = GoogleTranslator(source="zh-CN", target="vi")
            n2 = len(segs)
            for si, seg in enumerate(segs):
                if self._stop: return False
                txt = seg["text"].strip()
                if not txt: continue
                try: vi = tr.translate(txt); time.sleep(0.25)
                except: vi = txt
                translated.append({"start": seg["start"], "end": seg["end"],
                                   "zh": txt, "vi": vi})
                self.progress(0.58 + 0.35*(si/max(n2, 1)), f"Dịch {si+1}/{n2} đoạn")
            self.log(f"  ✅ Dich xong {len(translated)} doan (GoogleTranslator)", SUCCESS)
 
        # 5. LƯU SRT
        self.step(4); self.progress(0.95, "Lưu SRT...")
        stem = vid.stem.replace("_original", "")
        (out_dir / f"{stem}_segments.json").write_text(
            json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
        def fmt(s):
            ms = int(s*1000); h, ms = divmod(ms, 3600000)
            m2, ms = divmod(ms, 60000); sc, ms = divmod(ms, 1000)
            return f"{h:02d}:{m2:02d}:{sc:02d},{ms:03d}"
        lines = []
        for i2, seg in enumerate(translated, 1):
            lines += [str(i2), f"{fmt(seg['start'])} --> {fmt(seg['end'])}", seg["vi"], ""]
        srt = out_dir / f"{stem}_vi.srt"
        srt.write_text("\n".join(lines), encoding="utf-8")
        self.progress(1.0 if not (do_blur or do_tts or do_burn) else 0.60, "SRT xong ✅")
        self.log(f"✨ SRT: {srt.name}", SUCCESS)
        if not (do_blur or do_tts or do_burn):
            self.log("   → Import SRT vao CapCut", ACCENT2)
            return True
 
        # ── Bước hậu kỳ (tuỳ chọn) ──────────────────────────────────
        working_video = vid
 
        if do_blur:
            if self._stop: return False
            self.step(5); self.progress(0.63, "Làm mờ sub gốc tiếng Trung...")
            self.log("🎬 Lam mo sub Trung goc...", ACCENT2)
            blurred = out_dir / f"{stem}_blurred.mp4"
            try:
                _blur_subs(working_video, blurred, blur_top_pct, blur_bot_pct)
                working_video = blurred
                self.log("  ✅ Lam mo xong", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Lỗi mờ sub ({e}) — bỏ qua bước này", WARN)
            self.progress(0.72, "Mờ sub xong ✅")
 
        if do_tts:
            if self._stop: return False
            self.step(6); self.progress(0.73, "Đang tạo giọng đọc TTS...")
            self.log(f"🎙 TTS tiếng Việt (edge-tts, giọng {voice})...", ACCENT2)
            try:
                tts_audio = _generate_tts(translated, out_dir, voice)
                sz = tts_audio.stat().st_size // 1024
                self.log(f"  📁 TTS audio: {tts_audio.name} ({sz} KB)", SUBTEXT)
                tts_mixed = out_dir / f"{stem}_with_tts.mp4"
                _mix_tts(working_video, tts_audio, tts_mixed, orig_vol)
                if tts_mixed.exists():
                    self.log(f"  📁 Mix xong: {tts_mixed.name} "
                             f"({tts_mixed.stat().st_size//1024//1024} MB)", SUBTEXT)
                tts_audio.unlink(missing_ok=True)
                working_video = tts_mixed
                self.log("  ✅ Lồng tiếng xong", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Lỗi TTS ({e}) — bỏ qua bước này "
                         "(đã cài edge-tts chưa? pip install edge-tts)", WARN)
            self.progress(0.88, "TTS xong ✅")
 
        if do_burn:
            if self._stop: return False
            self.step(7); self.progress(0.89, "Đang burn phụ đề vào video...")
            self.log("🔥 Burn sub tiếng Việt vào video...", ACCENT2)
            final = out_dir / f"{stem}_FINAL.mp4"
            try:
                _burn_subs(working_video, srt, final, font_size, margin_v)
                working_video = final
                self.log(f"  ✅ Video hoàn chỉnh: {final.name}", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Lỗi burn sub ({e}) — bỏ qua bước này", WARN)
            self.progress(0.98, "Burn xong ✅")
 
        for tmp_name in ["_blurred.mp4", "_with_tts.mp4"]:
            p = out_dir / f"{stem}{tmp_name}"
            if p != working_video:
                p.unlink(missing_ok=True)
 
        self.progress(1.0, "Hoàn thành ✅")
        self.log(f"✨ XONG! → {working_video.name}", SUCCESS)
        return True
 
    def run_postprocess_only(self, vid_path, out_dir,
                             model="base", use_groq=False,
                             do_blur=True, do_tts=True, do_burn=True,
                             voice="nu", orig_vol=0.15,
                             blur_top_pct=0.72, blur_bot_pct=0.92,
                             margin_v=25, font_size=22):
        """Phase 2: bỏ qua tải + Whisper + dịch — chỉ chạy hậu kỳ (blur,
        TTS, burn) lên file video và SRT đã có từ Phase 1. Dùng khi user
        muốn chỉnh % blur / MarginV / FontSize sau khi đã xem preview rồi
        mới xuất video_FINAL, không cần tải lại hay transcribe lại.
 
        Yêu cầu: vid_path phải tồn tại và có file SRT tương ứng
        (<stem>_vi.srt) trong cùng out_dir — tức Phase 1 đã chạy xong.
        """
        vid = Path(vid_path).resolve()
        out_dir = Path(out_dir)
 
        if not vid.exists():
            self.log(f"❌ Không tìm thấy file video: {vid.name}", ERR)
            self.log("   Phase 1 (Tải + SRT) phải chạy xong trước.", WARN)
            self.done(); return
 
        # Tìm SRT đã có từ Phase 1 — cùng stem, hậu tố _vi.srt
        stem = vid.stem.replace("_original", "").replace("_blurred", "")
        srt = out_dir / f"{stem}_vi.srt"
        if not srt.exists():
            # Fallback: glob bất kỳ _vi.srt trong thư mục
            srts = sorted(out_dir.glob("*_vi.srt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if srts:
                srt = srts[0]
            else:
                self.log(f"❌ Không tìm thấy file SRT trong {out_dir.name}/", ERR)
                self.log("   Phase 1 (Tải + SRT) phải chạy xong trước.", WARN)
                self.done(); return
 
        self.log(f"\n{'─'*46}", SUBTEXT)
        self.log(f"▶ Áp dụng hậu kỳ lên: {vid.name}", TEXT)
        self.log(f"  SRT: {srt.name}", SUBTEXT)
        self.progress(0.0, "Chuẩn bị áp dụng hậu kỳ...")
 
        groq_client = None
        if use_groq and _GROQ_AVAILABLE:
            try:
                groq_client = GroqClient()
            except Exception:
                pass
 
        # Bỏ qua audio/whisper/dịch — nhảy thẳng vào hậu kỳ
        # Dùng _process_video với wav=None và segments đã có qua srt
        # Cách sạch nhất: tái dùng đúng đoạn hậu kỳ trong _process_video
        # bằng cách truyền srt đã có, không gọi lại whisper/dịch.
        try:
            ok = self._run_postprocess(vid, out_dir, srt,
                                       do_blur=do_blur, do_tts=do_tts, do_burn=do_burn,
                                       voice=voice, orig_vol=orig_vol,
                                       blur_top_pct=blur_top_pct, blur_bot_pct=blur_bot_pct,
                                       margin_v=margin_v, font_size=font_size)
            self.log(f"\n{'='*46}", ACCENT)
            if ok:
                self.log("XONG: Hậu kỳ thành công!", SUCCESS)
            else:
                self.log("XONG: Hậu kỳ thất bại — xem log.", WARN)
            self.progress(1.0, "Hoàn tất ✅" if ok else "Lỗi — xem log")
        except Exception as e:
            import traceback
            self.log(f"❌ Lỗi: {e}", ERR)
            self.log(traceback.format_exc(), ERR)
        self.done()
 
    def _run_postprocess(self, vid, out_dir, srt,
                         do_blur, do_tts, do_burn,
                         voice, orig_vol,
                         blur_top_pct, blur_bot_pct,
                         margin_v, font_size):
        """Chỉ chạy phần hậu kỳ của _process_video — tách ra để
        run_postprocess_only gọi được mà không phải copy-paste logic."""
        import re as re_
        stem = vid.stem
        working_video = vid
 
        if do_blur:
            if self._stop: return False
            self.step(5); self.progress(0.10, "Làm mờ sub gốc tiếng Trung...")
            self.log("🎬 Lam mo sub Trung goc...", ACCENT2)
            blurred = out_dir / f"{stem}_blurred.mp4"
            try:
                _blur_subs(working_video, blurred, blur_top_pct, blur_bot_pct)
                working_video = blurred
                self.log("  ✅ Lam mo xong", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Lỗi mờ sub ({e}) — bỏ qua bước này", WARN)
            self.progress(0.40, "Mờ sub xong ✅")
 
        if do_tts:
            if self._stop: return False
            self.step(6); self.progress(0.42, "Đang tạo giọng đọc TTS...")
            self.log(f"🎙 TTS tiếng Việt (edge-tts, giọng {voice})...", ACCENT2)
            # Đọc lại SRT để lấy segments cho TTS
            try:
                segs = _parse_srt_for_tts(srt)
                tts_audio = _generate_tts(segs, out_dir, voice)
                sz = tts_audio.stat().st_size // 1024
                self.log(f"  📁 TTS audio: {tts_audio.name} ({sz} KB)", SUBTEXT)
                tts_mixed = out_dir / f"{stem}_with_tts.mp4"
                _mix_tts(working_video, tts_audio, tts_mixed, orig_vol)
                if tts_mixed.exists():
                    self.log(f"  📁 Mix xong: {tts_mixed.name} "
                             f"({tts_mixed.stat().st_size//1024//1024} MB)", SUBTEXT)
                tts_audio.unlink(missing_ok=True)
                working_video = tts_mixed
                self.log("  ✅ Lồng tiếng xong", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Lỗi TTS ({e}) — bỏ qua bước này", WARN)
            self.progress(0.80, "TTS xong ✅")
 
        if do_burn:
            if self._stop: return False
            self.step(7); self.progress(0.82, "Đang burn phụ đề vào video...")
            self.log("🔥 Burn sub tiếng Việt vào video...", ACCENT2)
            final = out_dir / f"{stem}_FINAL.mp4"
            try:
                _burn_subs(working_video, srt, final, font_size, margin_v)
                self.log(f"  ✅ Video hoàn chỉnh: {final.name}", SUCCESS)
                self.log(f"✨ XONG! → {final.name}", SUCCESS)
            except Exception as e:
                self.log(f"  ⚠ Lỗi burn ({e})", WARN)
                return False
            self.progress(1.0, "Burn xong ✅")
 
        return True
 
    def run(self, urls, out_dir, model, use_groq=False,
            do_blur=False, do_tts=False, do_burn=False,
            voice="nu", orig_vol=0.15, cookies_file=None,
            blur_top_pct=0.72, blur_bot_pct=0.92,
            margin_v=25, font_size=22):
        ok = 0; n = len(urls)
 
        # Khởi tạo GroqClient 1 LẦN cho cả batch (không phải mỗi video),
        # để giữ nguyên trạng thái xoay vòng/cooldown key xuyên suốt —
        # tránh việc video sau lại thử lại đúng key vừa bị rate-limit ở
        # video trước. Lỗi khởi tạo (thiếu groq_client.py / thiếu key) →
        # tự rớt về chạy local cho TOÀN BỘ batch, có log rõ lý do.
        groq_client = None
        if use_groq:
            if not _GROQ_AVAILABLE:
                self.log("⚠ groq_client.py không tìm thấy — dùng Whisper/Dịch "
                        "local (offline) cho toàn bộ batch.", WARN)
                use_groq = False
            else:
                try:
                    groq_client = GroqClient()
                    self.log(f"☁ Dùng Groq API ({len(groq_client.pool)} key) cho "
                            f"Whisper + Dịch — tự rớt về local nếu Groq lỗi.", ACCENT2)
                except Exception as e:
                    self.log(f"⚠ Không khởi tạo được Groq ({e}) — dùng Whisper/Dịch "
                            f"local (offline) cho toàn bộ batch.", WARN)
                    use_groq = False
 
        for i, url in enumerate(urls, 1):
            if self._stop: self.log("⏹ Dừng.", WARN); break
            self.log(f"\n{'─'*46}", SUBTEXT)
            self.log(f"[{i}/{n}] {url[:65]}", TEXT)
            self.progress(0.0, f"Video {i}/{n}...")
            if self._one(url, Path(out_dir), model, use_groq, groq_client,
                         do_blur, do_tts, do_burn, voice, orig_vol,
                         cookies_file,
                         blur_top_pct=blur_top_pct, blur_bot_pct=blur_bot_pct,
                         margin_v=margin_v, font_size=font_size): ok += 1
        self.log(f"\n{'='*46}", ACCENT)
        self.log(f"XONG: {ok}/{n} video thành công!", SUCCESS)
        self.progress(1.0, f"Hoàn tất {ok}/{n} ✅")
        self.done()
 
    def _one(self, url, out_dir, model, use_groq=False, groq_client=None,
             do_blur=False, do_tts=False, do_burn=False,
             voice="nu", orig_vol=0.15, cookies_file=None,
             blur_top_pct=0.72, blur_bot_pct=0.92,
             margin_v=25, font_size=22):
        import re as re_
        from pathlib import Path as _Path
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            ytdlp = get_ytdlp()
 
            # ── Header bắt buộc cho Douyin (cần thiết từ 2025, thay đổi thường xuyên)
            # Nếu sau này Douyin chặn lại → cập nhật 3 dòng này là đủ, không cần sửa gì khác.
            DOUYIN_HEADERS = [
                "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "--add-header", "Referer:https://www.douyin.com/",
                "--add-header", "Accept-Language:zh-CN,zh;q=0.9,en;q=0.8",
            ]
 
            # Build cookie args một lần, dùng chung cho mọi lệnh yt-dlp
            ck_args = []
            if cookies_file and _Path(cookies_file).exists():
                ck_args = ["--cookies", str(cookies_file)]
                self.log(f"  🍪 Dùng cookies: {_Path(cookies_file).name}", SUBTEXT)
 
            # 1. TẢI
            self.step(0); self.progress(0.01, "Đang tải video...")
            self.log("⬇ Tai video...", ACCENT2)
            r = subprocess.run(ytdlp + ck_args + DOUYIN_HEADERS +
                               ["--print", "%(title).60s", "--no-download", url],
                               capture_output=True, text=True)
            title = "".join(c for c in (r.stdout.strip() or "video")
                            if c.isalnum() or c in "_- ")[:40].strip() or "video"
            out_path = out_dir / f"{title}_original.mp4"
 
            def dl(cmd):
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     bufsize=0, encoding="utf-8", errors="replace")
                buf = ""; last = ""; all_lines = []
                while True:
                    if self._stop: p.terminate(); return -999, "", []
                    ch = p.stdout.read(1)
                    if not ch:
                        if p.poll() is not None: break
                        continue
                    if ch in ("\r", "\n"):
                        line = buf.strip(); buf = ""
                        if not line: continue
                        last = line; all_lines.append(line)
                        m = re_.search(r"(\d+\.?\d*)%\s+of\s+([\d\.]+\S*)", line)
                        if m and "download" in line.lower():
                            pct = float(m.group(1))
                            self.progress(pct/100*0.13+0.01,
                                         f"Đang tải... {pct:.0f}% ({m.group(2)})")
                    else: buf += ch
                p.wait(); return p.returncode, last, all_lines
 
            # ── LỚP 1: yt-dlp + cookies + Douyin headers (phương án chính) ──
            self.log("  📡 Thử yt-dlp + Douyin headers...", SUBTEXT)
            rc, last, all_lines = dl(ytdlp + ck_args + DOUYIN_HEADERS +
                                     ["-f", "best[ext=mp4]/best", "-o", str(out_path),
                                      "--no-playlist", "--progress", url])
            if rc == -999: return False
 
            needs_fresh_cookies = rc != 0 and (
                "fresh cookies" in " ".join(all_lines).lower() or
                "fresh cookies" in last.lower()
            )
 
            # ── LỚP 2: Playwright bắt URL video trực tiếp từ network ──
            # Chỉ kích hoạt khi yt-dlp báo cần "fresh cookies" — tức Douyin
            # đã thay đổi API/signature. Playwright dùng browser thật → qua
            # được mọi bot-check mà không cần cookies thủ công.
            if needs_fresh_cookies or (rc != 0 and ck_args):
                self.log("  🌐 yt-dlp bị block → thử Playwright (browser thật)...", WARN)
                vid_url_found, aud_url_found = self._playwright_get_video_url(url)
                if vid_url_found and not self._stop:
                    self.log(f"  ✅ Playwright bắt được URL video", SUCCESS)
                    self.progress(0.05, "Đang tải qua Playwright...")
 
                    if aud_url_found:
                        # ── Split-stream: Douyin tách video và audio riêng ──
                        # Tải video-only và audio-only rồi mux bằng ffmpeg.
                        # Đây là trường hợp gây ra lỗi "Output file does not
                        # contain any stream" khi ffmpeg -vn không có audio
                        # để rút ra — file mp4 chỉ có video track.
                        self.log("  🔀 Phát hiện split-stream "
                                 "(video-only + audio-only) — tải và mux...", SUBTEXT)
                        vid_tmp = out_path.with_suffix(".video_tmp.mp4")
                        aud_tmp = out_path.with_suffix(".audio_tmp.mp4")
                        vid_dl = self._download_direct(vid_url_found, vid_tmp, url)
                        aud_dl = self._download_direct(aud_url_found, aud_tmp, url)
                        if vid_dl and aud_dl:
                            self.log("  📦 Mux video + audio...", SUBTEXT)
                            mux_r = subprocess.run([
                                "ffmpeg", "-y",
                                "-i", str(vid_dl), "-i", str(aud_dl),
                                "-c:v", "copy", "-c:a", "copy",
                                str(out_path)
                            ], capture_output=True, text=True)
                            vid_tmp.unlink(missing_ok=True)
                            aud_tmp.unlink(missing_ok=True)
                            if mux_r.returncode == 0 and out_path.exists():
                                vid = out_path
                                self.log(f"  ✅ Mux xong: {vid.name} "
                                         f"({vid.stat().st_size//1024//1024} MB)", SUCCESS)
                            else:
                                err = (mux_r.stderr or "")[-200:].strip()
                                self.log(f"  ⚠ Mux thất bại: {err}", WARN)
                                vid = None
                        else:
                            vid_tmp.unlink(missing_ok=True)
                            aud_tmp.unlink(missing_ok=True)
                            vid = None
                    else:
                        # ── Muxed stream: tải trực tiếp ──
                        vid = self._download_direct(vid_url_found, out_path, url)
 
                    if vid:
                        self.progress(0.14, f"Tải xong! ({vid.stat().st_size//1024//1024} MB)")
                        self.log(f"  ✅ {vid.name}", SUCCESS)
                        try:
                            self.on_video_ready(vid)
                        except Exception:
                            pass
                        return self._process_video(vid, out_dir, wav=None,
                                                   model=model, use_groq=use_groq,
                                                   groq_client=groq_client,
                                                   do_blur=do_blur, do_tts=do_tts,
                                                   do_burn=do_burn, voice=voice,
                                                   orig_vol=orig_vol,
                                                   blur_top_pct=blur_top_pct,
                                                   blur_bot_pct=blur_bot_pct,
                                                   margin_v=margin_v,
                                                   font_size=font_size)
                    else:
                        self.log("  ⚠ Playwright tải thất bại — thử yt-dlp không cookies...", WARN)
                else:
                    self.log("  ⚠ Playwright không bắt được URL — thử yt-dlp không cookies...", WARN)
 
                # ── LỚP 3: yt-dlp KHÔNG cookies (video public) ──
                self.log("  📡 Thử yt-dlp không cookies (video public)...", SUBTEXT)
                rc3, last, _ = dl(ytdlp + DOUYIN_HEADERS +
                                  ["-f", "best[ext=mp4]/best", "-o", str(out_path),
                                   "--no-playlist", "--progress", url])
                if rc3 == -999: return False
                if rc3 != 0:
                    raise RuntimeError(
                        "Tất cả 3 phương án đều thất bại.\n"
                        "→ Hãy export cookies Douyin mới từ Chrome (extension 'Get cookies.txt LOCALLY')\n"
                        f"Chi tiết lỗi: {last[:200]}"
                    )
 
            elif rc != 0:
                # Lỗi khác (không phải fresh cookies) — thử lại không chọn format
                rc2, last, _ = dl(ytdlp + ck_args + DOUYIN_HEADERS +
                                  ["-o", str(out_path), "--progress", url])
                if rc2 == -999: return False
                if rc2 != 0: raise RuntimeError(last[:300])
 
            # Tìm file video đã tải — KHÔNG dùng mtime vì yt-dlp bỏ qua
            # download nếu file đã tồn tại (mtime cũ hơn SRT/JSON từ lần
            # chạy trước → sorted by mtime chọn nhầm file).
            # Ưu tiên: out_path chính xác → glob "_original.*" → glob video.
            VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
            if out_path.exists():
                vid = out_path
            else:
                cands = [p for p in out_dir.iterdir()
                         if p.suffix.lower() in VIDEO_EXT and "_original" in p.name]
                if not cands:
                    cands = [p for p in out_dir.iterdir()
                             if p.suffix.lower() in VIDEO_EXT]
                if not cands:
                    raise RuntimeError("Không tìm thấy file video sau khi tải")
                vid = max(cands, key=lambda p: p.stat().st_mtime)
            self.progress(0.14, f"Tải xong! ({vid.stat().st_size//1024//1024} MB)")
            self.log(f"  ✅ {vid.name}", SUCCESS)
            try:
                self.on_video_ready(vid)
            except Exception:
                pass
 
            # Bước 2–8: audio → whisper → dịch → SRT → hậu kỳ
            return self._process_video(vid, out_dir,
                                       model=model, use_groq=use_groq,
                                       groq_client=groq_client,
                                       do_blur=do_blur, do_tts=do_tts,
                                       do_burn=do_burn, voice=voice,
                                       orig_vol=orig_vol,
                                       blur_top_pct=blur_top_pct,
                                       blur_bot_pct=blur_bot_pct,
                                       margin_v=margin_v,
                                       font_size=font_size)
        except Exception as e:
            import traceback
            self.log(f"❌ Lỗi: {e}", ERR)
            self.log(traceback.format_exc(), ERR)
            return False