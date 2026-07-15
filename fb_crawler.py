import asyncio, traceback, threading, re as _re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import *
from registry import Registry
from downloader import download_batch

# ── Thu thập reel URL + caption + views từ DOM ──────────────────
JS_COLLECT = """() => {
    const out = [];
    const seen = new Set();
    // Các nhãn chung chung Facebook gắn cho khung preview/thumbnail,
    // KHÔNG phải caption thật → phải loại bỏ, nếu không sẽ bị nhận
    // nhầm làm tiêu đề (vd "Bản xem trước ô thước phim").
    const GENERIC = /^(bản xem trước( ô thước phim)?|video preview|filmstrip preview|thước phim|xem video|watch video|reels?|video)$/i;
    // Số lượt xem: "731K", "126", "2 triệu", "2,8 triệu", "1,1 tr",
    // "500 nghìn"... – KHÔNG phải caption, dù qua được bộ lọc cũ vì có
    // dấu cách + chữ tiếng Việt. Đây chính là nguyên nhân tool lấy
    // nhầm số lượt xem làm tiêu đề.
    const isCountLike = (t) => /^[\d]+([.,]\d+)?\s*(triệu|nghìn|tỷ|tr|k|m|b)?$/i.test(t.trim());
    document.querySelectorAll('a[href*="/reel/"]').forEach(a => {
        const url = a.href.split('?')[0].replace(/\/+$/, '');
        if (!url || seen.has(url)) return;
        seen.add(url);

        // Thumbnail
        let thumb = '';
        for (const img of a.querySelectorAll('img')) {
            const s = img.src || img.getAttribute('data-src') || '';
            if (s.startsWith('http')) { thumb = s; break; }
        }

        // Caption: trang lưới (grid) của Facebook KHÔNG hiển thị caption
        // thật, chỉ có overlay số lượt xem → CHỈ tin aria-label/title đã
        // qua lọc generic/số lượt xem. KHÔNG dò text ở phần tử cha/anh em
        // nữa, vì ở các cấp cao sẽ dính text của nhiều ô reel khác nhau
        // ghép liền thành 1 chuỗi vô nghĩa (lỗi đã gặp). Nếu không có
        // aria-label hợp lệ, để trống — tool sẽ tự lấy caption thật qua
        // og:title ở bước fetch riêng từng reel (chính xác hơn nhiều).
        let caption = a.getAttribute('aria-label') || a.getAttribute('title') || '';
        if (caption && (GENERIC.test(caption.trim()) || isCountLike(caption))) caption = '';

        // Views – dùng cùng isCountLike để nhận đúng cả dạng "2,8 triệu"
        let views = '';
        let box2 = a.parentElement;
        for (let i = 0; i < 12 && box2; i++) {
            const t = [...box2.querySelectorAll('span,div')]
                .map(el => el.textContent.trim())
                .find(t => isCountLike(t) && t.length < 14);
            if (t) { views = t; break; }
            box2 = box2.parentElement;
        }

        out.push({url, thumb, views, caption});
    });
    return out;
}"""

# ── Scroll toàn bộ scrollable containers xuống đáy ─────────────
JS_SCROLL_BOTTOM = """() => {
    // Scroll window
    window.scrollTo(0, document.documentElement.scrollHeight);
    // Scroll tất cả div có overflow: scroll/auto (Facebook virtual scroll)
    document.querySelectorAll('*').forEach(el => {
        const st = window.getComputedStyle(el);
        if ((st.overflow === 'scroll' || st.overflow === 'auto'
             || st.overflowY === 'scroll' || st.overflowY === 'auto')
            && el.scrollHeight > el.clientHeight) {
            el.scrollTop = el.scrollHeight;
        }
    });
    return document.documentElement.scrollHeight;
}"""


class FBCrawler:
    def __init__(self, log, done, progress):
        self.log = log; self.done = done; self.progress = progress
        self._stop = False

    def stop(self): self._stop = True

    # ── Parse cookies.txt HOẶC storage_state.json → requests Session ──
    @staticmethod
    def _make_session(cookies_file):
        import requests
        from http.cookiejar import MozillaCookieJar
        s = requests.Session()
        s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if not cookies_file or not Path(cookies_file).exists():
            return s
        try:
            if str(cookies_file).lower().endswith(".json"):
                # storage_state.json (Playwright) – cookie nằm trong key "cookies"
                import json as _json
                data = _json.loads(Path(cookies_file).read_text(encoding="utf-8"))
                for c in data.get("cookies", []):
                    s.cookies.set(c["name"], c["value"],
                                  domain=c.get("domain", ".facebook.com"),
                                  path=c.get("path", "/"))
            else:
                jar = MozillaCookieJar()
                # Đảm bảo file có header Netscape
                raw = Path(cookies_file).read_text(encoding="utf-8", errors="ignore")
                if not raw.startswith("# Netscape"):
                    tmp = Path(cookies_file).parent / "_cookies_fixed.txt"
                    tmp.write_text("# Netscape HTTP Cookie File\n" + raw, encoding="utf-8")
                    jar.load(str(tmp), ignore_discard=True, ignore_expires=True)
                    tmp.unlink(missing_ok=True)
                else:
                    jar.load(cookies_file, ignore_discard=True, ignore_expires=True)
                s.cookies = jar
        except Exception:
            pass  # Tiếp tục không có cookies
        return s

    # storage_state→netscape & cookies_for_ytdlp giờ dùng chung từ
    # downloader.py (import ở đầu file) — không định nghĩa lại ở đây nữa.

    _GENERIC_TITLES = {
        "bản xem trước ô thước phim", "bản xem trước", "video preview",
        "filmstrip preview", "thước phim", "xem video", "watch video",
        "reels", "reel", "video",
    }

    @staticmethod
    def _fetch_title(url, session):
        try:
            r = session.get(url, timeout=12, allow_redirects=True)
            html = r.text

            # 1. og:title – Facebook đặt content TRƯỚC property
            for pat in [
                r'<meta\s+content=["\']([^"\']{3,}?)["\']\s+property=["\']og:title["\']',
                r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']{3,}?)["\']',
            ]:
                m = _re.search(pat, html, _re.IGNORECASE)
                if m:
                    t = m.group(1).strip()
                    # Unescape HTML entities cơ bản
                    t = t.replace("&amp;", "&").replace("&lt;", "<") \
                         .replace("&gt;", ">").replace("&#039;", "'") \
                         .replace("&quot;", '"')
                    if (t and len(t) > 2 and "facebook" not in t.lower()
                            and t.lower() not in FBCrawler._GENERIC_TITLES):
                        return t[:300]

            # 2. JSON-LD description
            m = _re.search(r'"description"\s*:\s*"([^"]{5,300}?)"', html)
            if m:
                t = m.group(1).strip()
                if (t and "facebook" not in t.lower()
                        and t.lower() not in FBCrawler._GENERIC_TITLES):
                    return t[:300]

            # 3. <title> tag
            m = _re.search(r'<title[^>]*>([^<]+)</title>', html, _re.IGNORECASE)
            if m:
                t = (m.group(1)
                     .replace(" | Facebook", "").replace("Facebook", "").strip())
                if t and len(t) > 2 and t.lower() not in FBCrawler._GENERIC_TITLES:
                    return t[:300]

        except Exception:
            pass
        return ""

    # ── Phase 2: lấy title thật bằng cách MỞ từng reel trong cùng
    #    phiên Playwright đã đăng nhập (đáng tin cậy hơn nhiều so với
    #    gọi HTTP trực tiếp bằng `requests` — Facebook phát hiện request
    #    không có dấu vân tay trình duyệt thật/không chạy JS và trả về
    #    trang lỗi chung "Error" thay vì nội dung thật, đúng như bạn gặp) ──
    # Hậu tố Facebook tự dính vào NGAY SAU caption rút gọn khi caption dài
    # ("...Xem thêm", "Ẩn bản dịch"...) – đây là nguyên nhân chính gây lấy
    # tiêu đề KHÔNG ĐẦY ĐỦ: text lấy được là bản dịch tự động đã bị Facebook
    # cắt ngắn + dính liền chữ của nút bấm, không phải caption gốc đầy đủ.
    _TRAILING_JUNK = _re.compile(
        r"\s*(\.\.\.)?\s*(xem thêm|see more|ẩn bản dịch|xem bản dịch( gốc)?|"
        r"hiển thị bản dịch|hide translation|show translation|"
        r"được dịch từ[^.]*|translated from[^.]*)\s*$", _re.IGNORECASE)

    @staticmethod
    def _clean_caption(t):
        if not t:
            return t
        prev = None
        while prev != t:           # lặp vì có thể dính nhiều cụm nối tiếp nhau
            prev = t
            t = FBCrawler._TRAILING_JUNK.sub("", t).strip()
        return t

    @staticmethod
    async def _extract_reel_title(page):
        # 1) ƯU TIÊN meta og:description/og:title – Facebook LUÔN nhúng
        # caption gốc ĐẦY ĐỦ vào đây để phục vụ link-preview, không hề bị
        # cắt bởi nút "Xem thêm" như phần hiển thị trên giao diện. Đây là
        # nguồn đáng tin cậy nhất để lấy full caption.
        try:
            meta = await page.evaluate("""() => {
                const get = (sel) => document.querySelector(sel)?.content || '';
                return { desc: get('meta[property="og:description"]'),
                         title: get('meta[property="og:title"]') };
            }""")
            for raw in (meta.get("desc", ""), meta.get("title", "")):
                t = FBCrawler._clean_caption((raw or "").strip())
                low = t.lower()
                if (t and len(t) > 3
                        and low not in FBCrawler._GENERIC_TITLES
                        and "facebook" not in low and low != "error"):
                    return t[:300]
        except Exception:
            pass

        # 2) <title> trang
        try:
            title = (await page.title() or "").strip()
            title = FBCrawler._clean_caption(title.replace(" | Facebook", "").strip())
            low = title.lower()
            if (title and len(title) > 3
                    and low not in FBCrawler._GENERIC_TITLES
                    and "facebook" not in low and low != "error"):
                return title[:300]
        except Exception:
            pass

        # 3) Fallback cuối: bấm "Xem thêm" để Facebook tự mở caption đầy đủ
        # trong DOM rồi mới dò text, tránh lấy đúng bản rút gọn dính nút bấm.
        try:
            await page.evaluate("""() => {
                const cand = [...document.querySelectorAll('div[role="button"], span')];
                const btn = cand.find(b => /^(xem thêm|see more)$/i.test((b.textContent||'').trim()));
                if (btn) btn.click();
            }""")
            await page.wait_for_timeout(400)
        except Exception:
            pass
        try:
            text = await page.evaluate("""() => {
                const GENERIC = /^(bản xem trước|video preview|error|facebook|reel|video|đăng nhập|log in|thích|like|share|bình luận|comment)/i;
                const isCountLike = t => /^[\\d]+([.,]\\d+)?\\s*(triệu|nghìn|tỷ|tr|k|m|b)?$/i.test(t.trim());
                const els = [...document.querySelectorAll('[dir="auto"]')];
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (t.length > 8 && t.length < 600
                        && !GENERIC.test(t) && !isCountLike(t)) {
                        return t;
                    }
                }
                return '';
            }""")
            if text:
                return FBCrawler._clean_caption(text.strip())[:300]
        except Exception:
            pass
        return ""

    async def _fetch_titles_playwright(self, ctx, videos, max_concurrent=3):
        need = [v for v in videos if v.get("title", "").startswith("Reel #")]
        already = len(videos) - len(need)
        if already:
            self.log(f"  {already} reel đã có caption từ DOM ✅", SUCCESS)
        if not need:
            return

        n = len(need)
        self.log(f"  Lấy tiêu đề {n} reel còn lại "
                f"(mở từng reel, {max_concurrent} tab song song)...", ACCENT2)
        sem = asyncio.Semaphore(max_concurrent)
        done_n = [0]

        async def _one(vid, idx):
            if self._stop: return
            async with sem:
                # Fix 2026-06-21: vài reel ĐẦU TIÊN hay lỗi title hơn hẳn
                # phần còn lại — do cold-start ngay lúc Phase 2 vừa mở tab
                # song song trong khi browser context còn đang "nguội"
                # sau Phase 1 (scroll). Giãn nhẹ thời điểm mở batch đầu
                # tiên (idx < max_concurrent) để giảm dồn request cùng lúc.
                if idx < max_concurrent:
                    await asyncio.sleep(idx * 0.4)

                page = None
                try:
                    page = await ctx.new_page()
                    await page.route(
                        "**/*.{gif,woff,woff2,ttf,mp4,webm,jpg,jpeg,png,webp}",
                        lambda r: r.abort())
                    await page.goto(vid["url"], timeout=25000,
                                    wait_until="domcontentloaded")

                    # Chờ og:description xuất hiện (tối đa 8s — tăng từ 5s)
                    # trước khi extract.
                    try:
                        await page.wait_for_selector(
                            'meta[property="og:description"]',
                            timeout=8000, state="attached")
                    except Exception:
                        await page.wait_for_timeout(1000)

                    await page.wait_for_timeout(1500)
                    title = await FBCrawler._extract_reel_title(page)

                    # Retry: RELOAD THẬT (không chỉ chờ thêm trên trang cũ)
                    # — nếu lần đầu bị cold-start/timeout, trang có thể
                    # chưa từng load đúng nội dung; chờ thêm trên cùng 1
                    # trang lỗi sẽ không giúp gì, phải tải lại từ đầu.
                    if not title:
                        try:
                            await page.reload(timeout=20000,
                                              wait_until="domcontentloaded")
                            try:
                                await page.wait_for_selector(
                                    'meta[property="og:description"]',
                                    timeout=8000, state="attached")
                            except Exception:
                                await page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(2000)
                        title = await FBCrawler._extract_reel_title(page)

                    if title:
                        vid["title"] = title
                    else:
                        # Debug: dump raw meta để biết Facebook trả về gì
                        try:
                            raw = await page.evaluate("""() => {
                                const get = s => document.querySelector(s)?.content || '';
                                return {
                                    desc:  get('meta[property="og:description"]'),
                                    title: get('meta[property="og:title"]'),
                                    pgTitle: document.title,
                                    url: location.href,
                                };
                            }""")
                            self.log(
                                f"  ⚠ Không lấy được title: {vid['url'].split('/reel/')[-1][:20]}"
                                f" | og:desc={repr(raw.get('desc',''))[:60]}"
                                f" | og:title={repr(raw.get('title',''))[:60]}"
                                f" | pageTitle={repr(raw.get('pgTitle',''))[:40]}",
                                WARN)
                        except Exception:
                            self.log(f"  ⚠ Không lấy được title + không debug được: {vid['url'][-40:]}", WARN)
                except Exception as e:
                    self.log(f"  ❌ Lỗi tab {vid['url'][-40:]}: {e}", ERR)
                finally:
                    if page:
                        try: await page.close()
                        except Exception: pass
                    done_n[0] += 1
                    self.progress(0.55 + done_n[0] / max(n, 1) * 0.42,
                                 f"Lấy tiêu đề {done_n[0]}/{n}...")

        await asyncio.gather(*[_one(v, i) for i, v in enumerate(need)])

    # ── (Dự phòng) Phase 2 cũ bằng requests – giữ lại để dùng khi cần ──
    def _fetch_all_titles(self, videos, cookies_file):
        need = [v for v in videos if v.get("title", "").startswith("Reel #")]
        already = len(videos) - len(need)
        if already:
            self.log(f"  {already} reel đã có caption từ DOM ✅", SUCCESS)
        if not need:
            return videos

        n = len(need)
        self.log(f"  Lấy tiêu đề {n} reel còn lại (requests, 6 luồng)...", ACCENT2)
        session = FBCrawler._make_session(cookies_file)
        done_n = [0]

        def _one(idx_vid):
            idx, vid = idx_vid
            if self._stop: return idx, ""
            title = FBCrawler._fetch_title(vid["url"], session)
            done_n[0] += 1
            self.progress(0.55 + done_n[0] / max(n, 1) * 0.42,
                          f"Lấy tiêu đề {done_n[0]}/{n}...")
            return idx, title

        idx_map = {id(v): i for i, v in enumerate(videos)}
        with ThreadPoolExecutor(max_workers=6) as exe:
            futures = [exe.submit(_one, (idx_map[id(v)], v)) for v in need]
            for future in as_completed(futures):
                idx, title = future.result()
                if title:
                    videos[idx]["title"] = title
        return videos

    # ── Phase 1: Playwright scroll ─────────────────────────────────
    async def _playwright_crawl(self, page_url, max_videos, cookies_file):
        from playwright.async_api import async_playwright

        # Xây URL reels tab
        pid = _re.search(r"id=(\d+)", page_url)
        if pid:
            reels_url = (f"https://www.facebook.com/profile.php"
                         f"?id={pid.group(1)}&sk=reels_tab")
        elif "sk=reels" in page_url or "/reels" in page_url:
            reels_url = page_url
        else:
            sep = "&" if "?" in page_url.rstrip("/") else "?"
            reels_url = page_url.rstrip("/") + sep + "sk=reels_tab"

        self.log(f"  Mở: {reels_url}", SUBTEXT)
        videos = []; seen = set()

        is_storage_state = (cookies_file and Path(cookies_file).exists()
                             and str(cookies_file).lower().endswith(".json"))

        # Parse cookies.txt cho Playwright (chỉ cần khi KHÔNG dùng storage_state.json)
        pw_cookies = []
        if cookies_file and Path(cookies_file).exists() and not is_storage_state:
            try:
                for line in open(cookies_file, encoding="utf-8", errors="ignore"):
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    p = line.split("\t")
                    if len(p) < 7: continue
                    domain, _, fpath, secure, _, name, value = p[:7]
                    is_secure = secure.upper() == "TRUE"
                    pw_cookies.append({
                        "name": name, "value": value,
                        # GIỮ NGUYÊN dấu "." đầu domain (vd ".facebook.com")
                        # để cookie áp dụng cho mọi subdomain (www., m., …).
                        # Nếu bỏ dấu chấm, Playwright coi đây là cookie
                        # host-only → www.facebook.com sẽ KHÔNG nhận được
                        # cookie này, khiến trang luôn ở trạng thái
                        # chưa đăng nhập (chỉ thấy ~10 reel công khai).
                        "domain": domain if domain.startswith(".") else "." + domain,
                        "path": fpath,
                        "secure": is_secure,
                        # sameSite="None" bắt buộc secure=True, nếu không
                        # Chromium sẽ âm thầm loại bỏ cookie. Dùng "Lax"
                        # khi cookie không secure để tránh mất cookie.
                        "sameSite": "None" if is_secure else "Lax",
                    })
            except Exception as e:
                self.log(f"  Lỗi đọc cookies: {e}", WARN)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-web-security"])
            ctx_kwargs = dict(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
                locale="vi-VN")
            if is_storage_state:
                # storage_state.json khớp ĐÚNG fingerprint trình duyệt đã
                # đăng nhập (cookies + localStorage) → bền hơn nhiều so
                # với cookies.txt export thủ công.
                ctx_kwargs["storage_state"] = str(cookies_file)
            ctx = await browser.new_context(**ctx_kwargs)

            if is_storage_state:
                self.log(f"  Loaded phiên đăng nhập từ storage_state.json ✅", SUBTEXT)
            elif pw_cookies:
                await ctx.add_cookies(pw_cookies)
                self.log(f"  Loaded {len(pw_cookies)} cookies ✅", SUBTEXT)
            else:
                self.log("  ⚠ Không có cookies – có thể bị giới hạn nội dung", WARN)

            page = await ctx.new_page()

            # Chặn tài nguyên nặng để tăng tốc
            await page.route(
                "**/*.{gif,woff,woff2,ttf,mp4,webm}",
                lambda r: r.abort()
            )

            try:
                await page.goto(reels_url, timeout=40000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(5000)  # Đợi React render xong
            except Exception as e:
                self.log(f"  Lỗi load trang: {e}", WARN)

            # ── Kiểm tra trạng thái đăng nhập ─────────────────────
            try:
                login_check = await page.evaluate("""() => {
                    const txt = document.body.innerText || '';
                    const hasLoginForm = !!document.querySelector(
                        'input[name="email"], input[name="pass"], #email, #pass');
                    const hasLoginText = /Log in to Facebook|Đăng nhập vào Facebook|You must log in/i.test(txt);
                    return hasLoginForm || hasLoginText;
                }""")
                if login_check:
                    self.log("  ⚠ Phát hiện trang đang ở dạng CHƯA ĐĂNG NHẬP! "
                             "Cookies không hợp lệ/hết hạn → FB chỉ trả về vài "
                             "reel công khai rồi dừng. Hãy xuất lại cookies.txt "
                             "từ Chrome đang đăng nhập sẵn vào Facebook.", ERR)
            except Exception:
                pass

            # Click vào giữa trang để focus
            await page.mouse.move(640, 450)
            await page.mouse.click(640, 450)
            await page.wait_for_timeout(800)

            # ── Thu thập lần đầu ──────────────────────────────────
            def _collect():
                """Chạy JS_COLLECT và thêm reel mới vào danh sách."""
                return  # Dùng async bên dưới

            no_new_streak = 0          # Số vòng liên tiếp không có reel mới
            prev_height   = 0          # scrollHeight vòng trước
            max_scroll    = max(max_videos * 2 + 20, 60)  # Tăng giới hạn vòng lặp

            for i in range(max_scroll):
                if self._stop: break

                # ── 1. Thu thập reels hiện có trong DOM ──────────
                items = await page.evaluate(JS_COLLECT)
                prev_count = len(seen)
                for item in items:
                    if len(videos) >= max_videos: break  # Đủ số lượng → dừng thêm
                    url = item["url"]
                    if url in seen: continue
                    seen.add(url)
                    num    = len(seen)
                    rid    = url.split("/reel/")[-1].split("/")[0] or str(num)
                    cap    = (item.get("caption") or "").strip()
                    # Nếu Facebook tự rút gọn caption ở lưới (kết thúc bằng
                    # "...") → coi như CHƯA ĐẦY ĐỦ, bỏ qua để bắt buộc Phase 2
                    # lấy lại full caption qua og:description (tránh lỗi chỉ
                    # lấy được một phần tiêu đề).
                    if cap.endswith("...") or cap.endswith("…"):
                        cap = ""
                    # Dùng caption từ DOM nếu đủ dài, không thì placeholder
                    title  = cap[:300] if len(cap) > 5 else f"Reel #{num}"
                    videos.append({
                        "id":    rid,
                        "title": title,
                        "url":   url,
                        "thumb": item.get("thumb", ""),
                        "views": item.get("views", ""),
                    })

                newly = len(seen) - prev_count
                if newly:
                    no_new_streak = 0
                    self.progress(
                        min(len(videos) / max_videos * 0.50, 0.50),
                        f"Tìm thấy {len(videos)} reel (vòng {i+1})")
                    self.log(
                        f"  Vòng {i+1}: +{newly} reel → tổng {len(videos)}", SUBTEXT)
                else:
                    no_new_streak += 1

                if len(videos) >= max_videos:
                    self.log(f"  Đã đủ {max_videos} reel!", SUCCESS)
                    break

                # ── 2. Dừng nếu quá nhiều vòng trắng ────────────
                if no_new_streak >= 18:
                    self.log("  18 vòng không có reel mới → dừng scroll", SUBTEXT)
                    break

                # ── 3. Scroll xuống đáy tuyệt đối ────────────────
                new_height = await page.evaluate(JS_SCROLL_BOTTOM)

                # Nếu height không tăng → nội dung chưa load thêm
                height_grew = new_height > prev_height
                prev_height = new_height

                # Bổ sung: cuộn từ từ bằng mouse wheel để trigger lazy-load
                for _ in range(3):
                    await page.mouse.wheel(0, 600)
                    await page.wait_for_timeout(300)

                # Đợi Facebook load batch tiếp theo
                # – Nếu height vừa tăng: đợi ngắn hơn
                # – Nếu height không đổi: đợi lâu hơn (đang fetch)
                wait_ms = 2500 if height_grew else 4000
                try:
                    await page.wait_for_load_state("networkidle",
                                                   timeout=wait_ms + 1000)
                except Exception:
                    await page.wait_for_timeout(wait_ms)

                # Đôi khi Facebook cần keyboard trigger
                await page.keyboard.press("End")
                await page.wait_for_timeout(500)

            total_scroll = i + 1
            self.log(
                f"  Phase 1 xong: {len(videos)} reel ({total_scroll} vòng cuộn)",
                SUCCESS)

            # ── Phase 2: lấy title thật bằng chính phiên đã đăng nhập ──
            # (thay vì requests — Facebook chặn request không có dấu
            # vân tay trình duyệt thật, trả về trang lỗi "Error")
            if videos:
                self.progress(0.55,
                              f"Có {len(videos)} reel, đang lấy tiêu đề...")
                await self._fetch_titles_playwright(ctx, videos)

            await browser.close()

        return videos

    # ── Tiện ích chung ────────────────────────────────────────────
    @staticmethod
    def _reel_id_from_url(url):
        return url.split("/reel/")[-1].split("/")[0].split("?")[0]

    # Registry (sổ theo dõi reel đã tải) giờ dùng chung từ registry.py
    # (import ở đầu file) thay vì 3 staticmethod riêng ở đây.

    # ── Entry point ────────────────────────────────────────────────
    def crawl(self, page_url, max_videos, cookies_file, result_fn, out_dir=None):
        self.log(f"Quét: {page_url[:65]}", ACCENT2)
        self.progress(0.02, "Khởi động trình duyệt...")
        try:
            import playwright
        except ImportError:
            self.log("Playwright chưa cài!", ERR)
            self.log("  pip install playwright", WARN)
            self.log("  python -m playwright install chromium", WARN)
            result_fn([]); self.done(); return

        videos = []
        try:
            videos = asyncio.run(
                self._playwright_crawl(page_url, max_videos, cookies_file))
            if videos:
                got = sum(1 for v in videos
                          if not v["title"].startswith("Reel #"))
                self.log(f"Lấy được tiêu đề: {got}/{len(videos)} reel",
                         SUCCESS)

                # Đánh dấu reel đã tải trước đó (nếu có thư mục output)
                # LƯU Ý: KHÔNG truyền source="facebook" ở đây — giữ nguyên
                # tên file "_da_tai.json" như code cũ, để không mất registry
                # bạn đã tích luỹ trước đó. Khi thêm nguồn mới (TikTok...),
                # nguồn đó dùng source="tiktok" → file riêng, không đụng.
                if out_dir:
                    reg = Registry(out_dir)
                    n_marked = reg.mark_downloaded(
                        videos, id_fn=lambda v: FBCrawler._reel_id_from_url(v["url"]))
                    if n_marked:
                        self.log(f"  {n_marked} reel đã tải trước đó "
                                f"(đánh dấu ✅, bỏ tick sẵn)", SUBTEXT)
            else:
                self.log(
                    "Không tìm thấy reel. Kiểm tra cookies & URL.", WARN)
        except Exception as e:
            self.log(f"Lỗi: {e}", ERR)
            self.log(traceback.format_exc(), ERR)

        self.progress(1.0, f"Quét xong: {len(videos)} reel")
        result_fn(videos)
        self.done()

    # ── Tải video đã chọn ──────────────────────────────────────────
    def download(self, items, out_dir, cookies_file, max_concurrent=3):
        """items: list[(url, title)] — giữ nguyên kiểu app.py đang truyền vào.

        Trước đây tải TUẦN TỰ từng video một (for loop + subprocess.run),
        dù file đã import ThreadPoolExecutor mà không hề dùng. Giờ tải
        song song max_concurrent video cùng lúc qua download_batch
        (downloader.py) — nhanh hơn đáng kể khi danh sách dài.
        """
        out_dir = Path(out_dir)
        reg = Registry(out_dir)  # cùng file "_da_tai.json" như trước
        # Chuyển (url, title) → dict id={reel_id} để download_batch dùng
        # chung được cho mọi nguồn (TikTok/YouTube sau này cũng theo dạng
        # dict {id, url, title}).
        dict_items = [
            {"id": FBCrawler._reel_id_from_url(url), "url": url, "title": title}
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
        self.log(f"\nTải xong: {ok}/{n} video",
                 SUCCESS if ok == n else WARN)
        self.progress(1.0, f"Hoàn tất {ok}/{n}")
        self.done()