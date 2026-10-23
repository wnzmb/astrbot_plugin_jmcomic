"""缓存目录：漫画目录定位、metadata 读写、章节目录与图片列表。"""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from ..core.downloader import ProgressDownloader


class CacheService:
    def __init__(self, settings):
        self.settings = settings
        # 后台预取与前台 jmlist 可能同时合并页数，读改写必须串行
        self._metadata_lock = threading.RLock()

    # ========== 漫画目录 ==========
    @staticmethod
    def album_dir_has_images(folder: Path) -> bool:
        """判断目录里是否真的有图片（避免把空目录/半成品当作已下载）"""
        try:
            for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
                for _ in folder.rglob(pattern):
                    return True
        except Exception:
            pass
        return False

    def find_album_dir(self, album_id):
        """按ID查找已下载的漫画目录。
        优先使用下载过程中记录的真实目录，其次按目录名前缀匹配，
        不再依赖 album.save_path（jmcomic 2.7.9 默认 dir_rule 会返回基础目录）。
        返回 Path 或 None
        """
        album_id = str(album_id).strip()
        base_resolved = self.settings.download_base_dir.resolve()

        # 1) 下载钩子记录的目录最准确
        recorded = ProgressDownloader._album_dirs.get(album_id)
        if recorded:
            folder = Path(recorded)
            if folder.is_dir() and self.album_dir_has_images(folder):
                return folder

        # 2) 新版目录是精确的 JM{id}；旧版标题目录作为兜底
        candidates = []
        exact = self.settings.download_base_dir / f"JM{album_id}"
        if exact.is_dir() and exact.resolve() != base_resolved:
            candidates.append(exact)
        legacy = [
            folder for folder in self.settings.download_base_dir.glob(f"JM{album_id}_*")
            if folder.is_dir() and folder.resolve() != base_resolved
        ]
        legacy.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        candidates.extend(legacy)
        for folder in candidates:
            if self.album_dir_has_images(folder):
                return folder

        return None

    # ========== metadata ==========
    @staticmethod
    def metadata_file(folder):
        return Path(folder) / "metadata.json" if folder is not None else None

    def read_metadata(self, folder):
        """读取缓存目录的 metadata.json，失败时返回空字典"""
        if folder is None:
            return {}
        try:
            data = json.loads(self.metadata_file(folder).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write_metadata(self, folder, data):
        """原子写入 metadata.json，失败不影响下载和打包"""
        if folder is None:
            return False
        try:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            path = self.metadata_file(folder)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.warning(f"写入缓存元数据失败: {e}")
            return False

    # ========== 话页数缓存 ==========
    def album_meta_dir(self, album_id):
        """定位 album metadata 目录；没下载过时允许只有 metadata、没有图片。"""
        album_id = str(album_id or "").strip()
        if not album_id:
            return None
        folder = self.find_album_dir(album_id)
        if folder is not None:
            return folder
        return self.settings.download_base_dir / f"JM{album_id}"

    def read_album_page_counts(self, album_id):
        """读取 album metadata 里已缓存的话页数：{photo_id: page_count}。"""
        folder = self.album_meta_dir(album_id)
        data = self.read_metadata(folder)
        result = {}
        for item in data.get("chapters", []) or []:
            if not isinstance(item, dict):
                continue
            photo_id = str(item.get("photo_id", "") or "")
            try:
                count = int(item.get("page_count") or 0)
            except (TypeError, ValueError):
                count = 0
            if photo_id and count > 0:
                result[photo_id] = count
        return result

    def update_album_page_counts(self, album_id, page_counts, indexes=None):
        """把补齐的话页数写回 album metadata，供后续 jmv/jmlist 直接复用。"""
        if not page_counts:
            return False
        with self._metadata_lock:
            return self._update_album_page_counts_locked(album_id, page_counts, indexes)

    def _update_album_page_counts_locked(self, album_id, page_counts, indexes=None):
        folder = self.album_meta_dir(album_id)
        if folder is None:
            return False
        data = self.read_metadata(folder)
        if not data:
            data = {"schema": 1, "album_id": str(album_id), "chapters": []}
        chapters = data.get("chapters")
        if not isinstance(chapters, list):
            chapters = []
            data["chapters"] = chapters
        by_id = {
            str(item.get("photo_id", "")): item
            for item in chapters
            if isinstance(item, dict)
        }
        indexes = indexes or {}
        changed = False
        for photo_id, count in page_counts.items():
            photo_id = str(photo_id)
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            if not photo_id or count <= 0:
                continue
            item = by_id.get(photo_id)
            if item is None:
                item = {"photo_id": photo_id, "page_count": count}
                try:
                    index = int(indexes.get(photo_id) or 0)
                except (TypeError, ValueError):
                    index = 0
                if index > 0:
                    item["index"] = index
                chapters.append(item)
                by_id[photo_id] = item
                changed = True
            elif item.get("page_count") != count:
                item["page_count"] = count
                changed = True
        if not changed:
            return False
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return self.write_metadata(folder, data)

    def known_photo_position(self, photo, folder=None, album_dir=None):
        """只返回可确认的网站话序号；取不到时返回 None。

        纯 metadata 查询，放在 cache 层避免 catalog <-> cache 循环依赖。
        """
        candidates = []
        try:
            candidates.append(getattr(photo, "album_index", None))
        except Exception:
            pass
        photo_id = str(getattr(photo, "photo_id", "") or "")
        if album_dir is not None and photo_id:
            for item in self.read_metadata(album_dir).get("chapters", []) or []:
                if str(item.get("photo_id", "")) == photo_id:
                    candidates.append(item.get("index"))
                    break
        if folder is not None:
            candidates.append(self.read_metadata(folder).get("index"))
        for value in candidates:
            try:
                value = int(value)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return None

    def album_cache_complete(self, folder: Path, chapters) -> bool:
        """确认漫画目录下每个章节都有实际图片，避免半成品缓存被当成完整缓存"""
        if folder is None:
            return False
        chapters = list(chapters or [])
        if not chapters:
            return self.album_dir_has_images(folder)
        for photo in chapters:
            if not self.chapter_complete(photo, album_dir=folder):
                return False
        return True

    # ========== 章节目录 ==========
    def migrate_legacy_chapter_dir(self, folder, photo, album_dir=None):
        """把旧版 P{id} 目录原地改名为 P{id}_第N话，避免升级后重复下载。"""
        if folder is None:
            return None
        try:
            folder = Path(folder)
            photo_id = str(getattr(photo, "photo_id", "") or "")
            if not photo_id or folder.name != f"P{photo_id}":
                return folder
            position = self.known_photo_position(photo, folder, album_dir)
            if position is None:
                return folder
            target = folder.with_name(f"P{photo_id}_第{position}话")
            if target.exists():
                # 目标目录已存在时，只有确实有图片才使用，避免返回空目录。
                return target if self.album_dir_has_images(target) else folder
            folder.rename(target)
            logger.info(f"缓存目录已迁移：{folder.name} -> {target.name}")
            return target
        except Exception as e:
            logger.warning(f"缓存目录迁移失败（继续使用旧目录）: {e}")
            return folder

    def chapter_dir(self, photo, album_dir=None):
        """定位章节图片目录"""
        candidates = []
        try:
            candidates.append(Path(self.settings.option.decide_image_save_dir(photo, ensure_exists=False)))
        except Exception:
            pass
        save_path = getattr(photo, "save_path", "")
        if save_path:
            candidates.append(Path(save_path))

        for folder in candidates:
            try:
                if folder.is_dir() and self.album_dir_has_images(folder):
                    return self.migrate_legacy_chapter_dir(folder, photo, album_dir)
            except Exception:
                continue

        # 兜底：在漫画目录下按章节ID/章节名匹配
        if album_dir is not None:
            name = getattr(photo, "name", "") or ""
            photo_id = str(getattr(photo, "photo_id", "") or "")
            try:
                subs = [sub for sub in album_dir.iterdir() if sub.is_dir()]
                # 1) 新版精确 P{id}_第N话；旧版 P{id}/P{id}_标题 兼容并迁移
                if photo_id:
                    exact = album_dir / f"P{photo_id}"
                    if exact.is_dir() and self.album_dir_has_images(exact):
                        return self.migrate_legacy_chapter_dir(exact, photo, album_dir)
                    for sub in subs:
                        if sub.name.startswith(f"P{photo_id}_") and self.album_dir_has_images(sub):
                            return sub
                # 2) 标题完全匹配
                if name:
                    for sub in subs:
                        if sub.name == name and self.album_dir_has_images(sub):
                            return sub
                    # 3) 最后才允许包含匹配（旧缓存标题可能带前后缀）。
                    #    新版 P{id}_第N话 目录不参与，否则纯数字标题 "37"
                    #    会命中 P1172871_第37话，把第38话误判成已缓存。
                    if not name.isdigit() and len(name) >= 4:
                        for sub in subs:
                            if re.match(r"^P\d+_第\d+话$", sub.name):
                                continue
                            if name in sub.name and self.album_dir_has_images(sub):
                                return sub
            except Exception:
                pass
        return None

    def chapter_complete(self, photo, folder=None, album_dir=None):
        """章节目录是否存在且页数足够；页数未知时用图片编号连续性兜底。"""
        folder = folder or self.chapter_dir(photo, album_dir)
        if folder is None:
            return False
        images = self.chapter_images(folder)
        actual = len(images)
        if actual <= 0:
            return False
        # 图片名是 00001.jpg 这类序号时，缺页会留下编号空洞。
        # 这能识别“metadata.page_count 被上一次残缺下载写成实际页数”的旧缓存。
        numbers = []
        for image in images:
            if image.stem.isdigit():
                numbers.append(int(image.stem))
        if numbers:
            if min(numbers) not in (0, 1):
                return False
            if max(numbers) - min(numbers) + 1 != len(numbers):
                return False
        expected = 0
        page_arr = getattr(photo, "page_arr", None)
        try:
            expected = len(page_arr or [])
        except Exception:
            expected = 0
        if expected <= 0:
            try:
                expected = int(getattr(photo, "page_count", 0) or 0)
            except (TypeError, ValueError):
                expected = 0
        if expected <= 0:
            try:
                expected = int(self.read_metadata(folder).get("page_count") or 0)
            except (TypeError, ValueError):
                expected = 0
        return actual >= expected if expected > 0 else True

    @staticmethod
    def chapter_images(chapter_dir: Path):
        files = []
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
            files.extend(chapter_dir.glob(pattern))
        return sorted(files, key=lambda p: p.name)

    def chapter_image_paths(self, photo, album_dir=None):
        """取章节已下载图片列表；未下载时返回空列表"""
        folder = self.chapter_dir(photo, album_dir)
        if folder is None:
            return []
        return self.chapter_images(folder)
