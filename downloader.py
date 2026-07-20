"""Downloader dùng chung cho mọi nguồn yt-dlp hỗ trợ (Facebook, TikTok,
YouTube, Instagram...).

Trước đây nằm rải rác trong fb_crawler.py:
  - _storage_state_to_netscape / _cookies_for_ytdlp  (convert cookie)
  - download() method                                 (tải tuần tự, KHÔNG
    có concurrency dù đã import ThreadPoolExecutor)

Module này tách phần đó ra dùng chung, và bổ sung concurrency thật cho
download_batch (trước đây tải tuần tự từng video 1, rất chậm khi scale).
"""
import json
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import get_ytdlp, SUCCESS, ERR, WARN


# ── Sanitize tên file ────────────────────────────────────────────────
def sanitize_filename(title, max_len=40, fallback="video"):
    safe = "".join(c for c in (title or "") if c.isalnum() or c in "_- ")
    safe = safe.strip()[:max_len]
    return safe or fallback


# ── Cookie: storage_state.json (Playwright) → cookies.txt (Netscape) ──
def storage_state_to_netscape(json_path):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    lines = ["# Netscape HTTP Cookie File"]
    far_future = int(time.time()) + 60 * 60 * 24 * 365
    for c in data.get("cookies", []):
        domain = c.get("domain", "")
        if not domain:
            continue
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires")
        expires = str(int(expires)) if expires and expires > 0 else str(far_future)
        lines.append("\t".join([
            domain, flag, c.get("path", "/"), secure, expires,
            c.get("name", ""), c.get("value", "")]))
    tmp = Path(json_path).parent / "_storage_state_as_cookies.txt"
    tmp.write_text("\n".join(lines), encoding="utf-8")
    return str(tmp)


def cookies_for_ytdlp(cookies_file):
    """Trả về đường dẫn cookies.txt hợp lệ cho yt-dlp (tự convert nếu là .json)."""
    if not cookies_file or not Path(cookies_file).exists():
        return None
    if str(cookies_file).lower().endswith(".json"):
        try:
            return storage_state_to_netscape(cookies_file)
        except Exception:
            return None
    return str(cookies_file)


# Các lỗi TẠM THỜI của yt-dlp — thường do TikTok/nguồn soft rate-limit
# hoặc lag mạng, KHÔNG phải do video có vấn đề thật (private/xoá/hết hạn).
# Bằng chứng: cùng 1 video, lần chạy này lỗi rehydration, lần khác lại
# tải được bình thường — nên retry sau vài giây thường sẽ qua.
_TRANSIENT_ERROR_MARKERS = (
    "rehydration",       # TikTok trả HTML rút gọn, thiếu __UNIVERSAL_DATA__
    "unable to extract", # parse trang thất bại nói chung, hay do rate-limit
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "temporary failure",
    "http error 429",    # too many requests
    "http error 503",    # service unavailable
)


def _is_transient_error(err_text):
    t = (err_text or "").lower()
    return any(marker in t for marker in _TRANSIENT_ERROR_MARKERS)


# ── Tải 1 video ─────────────────────────────────────────────────────
def download_one(url, title, out_dir, cookies_file=None, extra_args=None,
                  max_retries=3, retry_delay=4):
    """Tải 1 video bằng yt-dlp. Trả về (ok: bool, path_hoặc_loi: str).

    Fix 'lỗi rehydration ngẫu nhiên khiến video bị coi thất bại vĩnh viễn'
    (2026-07-16): trước đây chỉ gọi yt-dlp đúng 1 lần — nếu dính lỗi tạm
    thời (TikTok soft rate-limit trả HTML rút gọn, timeout mạng...) thì
    coi là thất bại luôn, dù thử lại ngay sau vài giây thường sẽ qua (đã
    quan sát: cùng video lúc lỗi lúc không giữa 2 lần crawl). Giờ tự
    retry tối đa `max_retries` lần với backoff tăng dần, NHƯNG chỉ với
    lỗi khớp _TRANSIENT_ERROR_MARKERS — lỗi khác (video private, đã xoá,
    sai định dạng...) trả về ngay lần đầu, retry thêm cũng vô ích."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(title, fallback="video")
    out_path = out_dir / f"{safe}.mp4"

    ytdlp = get_ytdlp()
    if not ytdlp:
        return False, "yt-dlp không tìm thấy (pip install yt-dlp)"

    cmd = ytdlp + ["-f", "best[ext=mp4]/best", "-o", str(out_path), url]
    ck = cookies_for_ytdlp(cookies_file)
    if ck:
        cmd += ["--cookies", ck]
    if extra_args:
        cmd += extra_args

    last_err = "Lỗi không rõ"
    for attempt in range(1, max_retries + 1):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return True, str(out_path)

        last_err = (r.stderr or "")[-200:]
        if attempt < max_retries and _is_transient_error(last_err):
            # Backoff tăng dần (4s, 8s, 12s...) — giãn cách đủ để qua
            # được soft rate-limit, không cần random vì mỗi video đã
            # chạy trên luồng riêng (ThreadPoolExecutor), tự nhiên lệch
            # thời điểm nhau rồi.
            time.sleep(retry_delay * attempt)
            continue
        # Lỗi không thuộc dạng tạm thời, hoặc đã hết lượt retry.
        break

    return False, last_err


# ── Tải hàng loạt: concurrency + registry + stop graceful ────────────
def download_batch(items, out_dir, id_fn, cookies_file=None, registry=None,
                    max_concurrent=3, stop_check=None, log=None, progress=None):
    """
    items: list dict, mỗi item cần ít nhất {'url', 'title'}.
    id_fn: hàm lấy ID duy nhất từ item để ghi registry, vd lambda it: it['id'].
    registry: instance Registry (tuỳ chọn) — nếu có:
              - tự bỏ qua item đã có trong registry (khỏi tải lại)
              - tự add ngay khi 1 item tải xong thành công
    max_concurrent: số video tải song song (mặc định 3, giống Phase 2
                    title-fetch của Facebook — tránh bị rate-limit).
    stop_check: hàm trả True nếu cần dừng giữa chừng.
    log(msg, color=None), progress(fraction, label): callback tuỳ chọn.

    Trả về (ok_count, total_count_đã_thử_tải).
    """
    log = log or (lambda *a, **k: None)
    progress = progress or (lambda *a, **k: None)
    stop_check = stop_check or (lambda: False)

    todo = []
    skipped = 0
    for it in items:
        iid = id_fn(it)
        if registry and registry.has(iid):
            skipped += 1
            continue
        todo.append((iid, it))

    if skipped:
        log(f"  ⏭ Bỏ qua {skipped} video đã có trong registry (đã tải trước đó).")

    n = len(todo)
    if n == 0:
        log("Không có video mới cần tải (đã tải hết trước đó).")
        return 0, 0

    ok_count = 0
    done_count = 0

    def _work(iid, it):
        if stop_check():
            return iid, it, False, "stopped"
        ok, info = download_one(it["url"], it.get("title", ""), out_dir,
                                 cookies_file=cookies_file)
        return iid, it, ok, info

    pool = ThreadPoolExecutor(max_workers=max_concurrent)
    try:
        futures = {pool.submit(_work, iid, it): (iid, it) for iid, it in todo}
        for fut in as_completed(futures):
            if stop_check():
                log("⏹ Dừng.", WARN)
                break
            iid, it, ok, info = fut.result()
            done_count += 1
            title = (it.get("title") or "")[:55]
            if ok:
                ok_count += 1
                log(f"  ✅ [{done_count}/{n}] {title}", SUCCESS)
                if registry:
                    registry.add(iid)
            else:
                log(f"  ❌ [{done_count}/{n}] {title} — {info} "
                    f"(đã thử lại vẫn lỗi)", ERR)
            progress(done_count / n, f"Tải {done_count}/{n}...")
    finally:
        # wait=False + cancel_futures: nếu người dùng bấm Dừng, các tác
        # vụ CHƯA bắt đầu sẽ bị huỷ ngay, không chờ hết hàng đợi mới thoát.
        # Các tác vụ ĐANG tải dở (subprocess yt-dlp) sẽ tự chạy nốt trong
        # nền — không thể kill an toàn giữa chừng mà không làm hỏng file.
        pool.shutdown(wait=False, cancel_futures=True)

    return ok_count, n