"""Registry dùng chung: theo dõi item đã tải, theo từng thư mục output.

Trước đây logic này nằm cứng trong fb_crawler.py (_load_registry /
_save_registry / _registry_path, đều là staticmethod chỉ phục vụ Facebook).
Tách ra đây để TikTok, YouTube, Instagram... dùng chung cùng 1 pattern,
không phải copy-paste lại.
"""
import json
from pathlib import Path
from threading import Lock


class Registry:
    """Sổ theo dõi ID đã tải, lưu dạng JSON trong thư mục output.

    Mỗi nguồn có file riêng để tránh đụng ID trùng giữa các nguồn khác
    nhau, vd: `_da_tai_facebook.json`, `_da_tai_tiktok.json`.
    Nếu không truyền `source`, dùng `_da_tai.json` — tương thích ngược
    100% với registry Facebook hiện có (cùng tên file cũ).
    """

    def __init__(self, out_dir, source=None):
        self.out_dir = Path(out_dir)
        fname = f"_da_tai_{source}.json" if source else "_da_tai.json"
        self.path = self.out_dir / fname
        self._lock = Lock()
        self._ids = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return set(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception:
                return set()
        return set()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(sorted(self._ids), ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def has(self, item_id):
        return item_id in self._ids

    def add(self, item_id):
        """Thêm 1 ID và ghi file ngay (không batch).

        Quan trọng: ghi ngay sau MỖI item, không gom batch — để nếu
        người dùng bấm Dừng giữa chừng, các item đã tải xong trước đó
        không bị mất khỏi registry (đúng pattern cũ trong fb_crawler.py).
        """
        with self._lock:
            self._ids.add(item_id)
            self._save()

    def mark_downloaded(self, items, id_fn):
        """Gắn cờ items[i]['downloaded'] = True/False dựa theo id_fn(item).

        Dùng sau khi crawl xong 1 danh sách reel/video, để UI biết item
        nào đã tải trước đó (tô xanh, tự bỏ tick).
        Trả về số lượng item được đánh dấu đã tải.
        """
        n_marked = 0
        for it in items:
            done = self.has(id_fn(it))
            it["downloaded"] = done
            if done:
                n_marked += 1
        return n_marked

    def all_ids(self):
        return set(self._ids)