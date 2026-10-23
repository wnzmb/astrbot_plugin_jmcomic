"""缓存维护：磁盘占用统计与图片缓存清理。

清理只删除图片，保留 metadata.json；专辑/章节目录也不删除，便于后续
`jmv`/`jmlist` 直接复用已缓存的话页数。
"""

import shutil
import time
from pathlib import Path

from astrbot.api import logger


class MaintenanceService:
    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def human_size(size):
        size = float(size or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
            size /= 1024
        return f"{size:.1f}TB"

    @staticmethod
    def _iter_files(root):
        root = Path(root)
        if not root.exists():
            return
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    yield path
            except OSError:
                continue

    def disk_report(self):
        """统计下载缓存、ZIP 临时目录、CSV 导出目录的占用。"""
        roots = (
            ("下载缓存", self.settings.download_base_dir),
            ("ZIP 临时", self.settings.zip_base_dir),
            ("CSV 临时", self.settings.data_dir / "exports"),
        )
        report = []
        for label, root in roots:
            root = Path(root)
            files = 0
            total = 0
            images = 0
            image_bytes = 0
            metadata = 0
            metadata_bytes = 0
            for path in self._iter_files(root):
                files += 1
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                total += size
                if path.suffix.lower() in self.IMAGE_SUFFIXES:
                    images += 1
                    image_bytes += size
                elif path.name == "metadata.json":
                    metadata += 1
                    metadata_bytes += size
            albums = 0
            if label == "下载缓存" and root.exists():
                try:
                    albums = sum(1 for item in root.iterdir() if item.is_dir())
                except OSError:
                    albums = 0
            report.append({
                "label": label,
                "root": root,
                "files": files,
                "bytes": total,
                "images": images,
                "image_bytes": image_bytes,
                "metadata": metadata,
                "metadata_bytes": metadata_bytes,
                "albums": albums,
            })
        usage = shutil.disk_usage(self.settings.download_base_dir)
        return report, usage

    def scan_clean(self, days, active_album_ids=None):
        """统计可清理图片；days=None 表示全部图片。不删除任何文件。"""
        cutoff = None
        if days is not None:
            cutoff = time.time() - max(0, int(days)) * 86400
        active = {str(item) for item in (active_album_ids or set())}
        files = 0
        total = 0
        skipped_active = []
        root = Path(self.settings.download_base_dir)
        if not root.exists():
            return {"files": 0, "bytes": 0, "skipped_active": []}
        for album_dir in root.iterdir():
            if not album_dir.is_dir() or album_dir.name.startswith("."):
                continue
            album_id = album_dir.name[2:] if album_dir.name.startswith("JM") else ""
            if album_id and album_id in active:
                skipped_active.append(album_dir.name)
                continue
            for path in album_dir.rglob("*"):
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    if path.suffix.lower() not in self.IMAGE_SUFFIXES:
                        continue
                    if cutoff is not None and path.stat().st_mtime >= cutoff:
                        continue
                    total += path.stat().st_size
                    files += 1
                except OSError:
                    continue
        return {
            "files": files,
            "bytes": total,
            "skipped_active": skipped_active,
        }

    def clean(self, days, active_album_ids=None):
        """删除符合条件的图片文件，保留 metadata.json。"""
        cutoff = None
        if days is not None:
            cutoff = time.time() - max(0, int(days)) * 86400
        active = {str(item) for item in (active_album_ids or set())}
        deleted_files = 0
        deleted_bytes = 0
        failed = 0
        skipped_active = []
        root = Path(self.settings.download_base_dir)
        if not root.exists():
            return {
                "files": 0,
                "bytes": 0,
                "failed": 0,
                "skipped_active": [],
            }
        for album_dir in root.iterdir():
            if not album_dir.is_dir() or album_dir.name.startswith("."):
                continue
            album_id = album_dir.name[2:] if album_dir.name.startswith("JM") else ""
            if album_id and album_id in active:
                skipped_active.append(album_dir.name)
                continue
            for path in album_dir.rglob("*"):
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    if path.suffix.lower() not in self.IMAGE_SUFFIXES:
                        continue
                    if cutoff is not None and path.stat().st_mtime >= cutoff:
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    deleted_files += 1
                    deleted_bytes += size
                except OSError as e:
                    failed += 1
                    logger.warning(f"清理缓存图片失败 {path}: {e}")
        return {
            "files": deleted_files,
            "bytes": deleted_bytes,
            "failed": failed,
            "skipped_active": skipped_active,
        }
