"""
Đăng nhập Facebook MỘT LẦN bằng chính trình duyệt Playwright (có giao
diện, không headless), rồi lưu lại toàn bộ phiên đăng nhập (cookies +
localStorage) ra file storage_state.json.

Vì sao bền hơn cookies.txt export từ Chrome cá nhân?
  - cookies.txt được "bứng" từ Chrome thật rồi nhét vào Playwright,
    nên User-Agent / fingerprint của 2 trình duyệt khác nhau → Facebook
    dễ nghi ngờ và thu hồi session sớm.
  - storage_state.json được tạo NGAY trên Playwright (cùng UA, cùng
    fingerprint sẽ dùng để crawl sau này) → khớp môi trường, ít bị
    Facebook đánh dấu bất thường hơn.

Cách dùng:
    python login_fb.py
    python login_fb.py --out C:/project/douyin_tool/fb_state.json

Sau khi chạy xong, dán đường dẫn file .json vừa tạo vào ô
"File cookies.txt" của Facebook Crawler (tool tự nhận diện đuôi .json).
"""
import argparse
import asyncio
import sys
from pathlib import Path


async def main(out_path: Path):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
            locale="vi-VN")
        page = await ctx.new_page()
        await page.goto("https://www.facebook.com/", timeout=60000)

        print("\n" + "=" * 60)
        print("  Một cửa sổ Chrome vừa mở.")
        print("  → Đăng nhập Facebook bình thường (kể cả mã 2FA nếu có).")
        print("  → Sau khi vào được trang chủ / News Feed, quay lại đây")
        print("    và nhấn Enter để lưu phiên đăng nhập.")
        print("  (Hoặc cứ để im — script tự phát hiện khi bạn đăng nhập")
        print("   xong và tự lưu.)")
        print("=" * 60 + "\n")

        async def wait_logged_in():
            """Tự phát hiện đăng nhập xong qua cookie c_user."""
            while True:
                cookies = await ctx.cookies("https://www.facebook.com")
                if any(c["name"] == "c_user" for c in cookies):
                    return
                await asyncio.sleep(1)

        loop = asyncio.get_event_loop()
        enter_task = loop.run_in_executor(
            None, input, "Nhấn Enter sau khi đăng nhập xong (hoặc đợi tự nhận)... ")
        login_task = asyncio.create_task(wait_logged_in())

        done, pending = await asyncio.wait(
            [enter_task, login_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

        cookies = await ctx.cookies("https://www.facebook.com")
        logged_in = any(c["name"] == "c_user" for c in cookies)
        if not logged_in:
            print("\n⚠ Chưa thấy đăng nhập thành công (không tìm thấy cookie 'c_user').")
            print("  Vẫn lưu trạng thái hiện tại, nhưng có thể chưa hợp lệ.")
            print("  Nếu lần quét sau vẫn báo 'CHƯA ĐĂNG NHẬP', hãy chạy lại script này.")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(out_path))

        print(f"\n{'✅' if logged_in else '⚠'} Đã lưu phiên đăng nhập vào: {out_path}")
        print("   → Dán đường dẫn này vào ô 'File cookies.txt' trong tab Facebook Crawler.")

        await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="fb_state.json",
                     help="Đường dẫn file lưu phiên đăng nhập (mặc định: fb_state.json)")
    args = ap.parse_args()
    try:
        asyncio.run(main(Path(args.out)))
    except KeyboardInterrupt:
        print("\nĐã hủy.")
        sys.exit(0)