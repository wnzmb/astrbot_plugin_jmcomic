"""下载执行：整本/单章下载、重试与部分失败处理、已发送页段去重。"""

import asyncio

import jmcomic
from astrbot.api import logger
from jmcomic.jm_exception import (
    PartialDownloadFailedException,
    RequestRetryAllFailException,
)

from ..core.downloader import ProgressDownloader


class DownloadService:
    def __init__(self, settings, cache, messaging):
        self.settings = settings
        self.cache = cache
        self.messaging = messaging

    # ========== 整本下载 ==========
    async def download_album_progress(self, event, album_id, holder, chapters=None):
        """下载整本，结果写入 holder['dir'] / holder['duration']。

        - 只有所有章节都已有图片时才跳过下载，避免半成品缓存产出残缺 PDF；
        - 图片/CDN 瞬时失败时自动重试，已下载图片会复用缓存；
        - 重试后仍有失败时，允许发送已完成章节，并由上层报告部分失败。
        """
        folder = self.cache.find_album_dir(album_id)
        if folder is not None and self.cache.album_cache_complete(folder, chapters):
            holder["dir"] = folder
            holder["duration"] = 0.0
            return

        attempts = 3
        for attempt in range(1, attempts + 1):
            ProgressDownloader._progress.pop(str(album_id), None)
            task = asyncio.create_task(
                jmcomic.download_album_async(
                    album_id, self.settings.option, downloader=ProgressDownloader
                )
            )
            try:
                result = await task
            except asyncio.CancelledError:
                # 用户执行 /jmstop 时，把底层整本下载任务一起取消
                if not task.done():
                    task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise
            except (PartialDownloadFailedException, RequestRetryAllFailException) as e:
                folder = self.cache.find_album_dir(album_id)
                if attempt < attempts:
                    yield self.messaging.plain(event,
                        f"⚠️ 第{attempt}次下载有图片失败（{type(e).__name__}），"
                        "正在重试，已下载图片会复用缓存..."
                    )
                    await asyncio.sleep(3)
                    continue
                if folder is not None and self.cache.album_dir_has_images(folder):
                    holder["dir"] = folder
                    holder["duration"] = 0.0
                    holder["partial"] = True
                    yield self.messaging.plain(event,
                        "⚠️ 重试后仍有图片下载失败，先发送已完成话；"
                        "缺失图片已保留缓存，稍后重跑命令可继续补齐。"
                    )
                    return
                raise

            folder = self.cache.find_album_dir(album_id)
            holder["dir"] = folder
            holder["duration"] = float(getattr(result, "duration", 0) or 0)
            if folder is None:
                yield self.messaging.plain(event, "⚠️ 下载完成但找不到漫画目录")
            elif chapters and not self.cache.album_cache_complete(folder, chapters):
                holder["partial"] = True
                yield self.messaging.plain(event,
                    "⚠️ 下载结束但仍有话缺失，将先发送已完成话；"
                    "稍后重跑命令可继续补齐。"
                )
            return

    # ========== 单章下载 ==========
    async def ensure_photo_downloaded(self, photo, album_dir=None):
        """确保单章已下载，返回 (目录, 耗时)"""
        folder = self.cache.chapter_dir(photo, album_dir)
        if folder is not None:
            return folder, 0.0

        try:
            result = await jmcomic.download_photo_async(
                photo.photo_id, self.settings.option
            )
        except (PartialDownloadFailedException, RequestRetryAllFailException) as e:
            folder = self.cache.chapter_dir(photo, album_dir)
            if folder is not None and self.cache.album_dir_has_images(folder):
                logger.warning(f"第{getattr(photo, 'photo_id', '')}话部分图片失败，先使用已下载图片: {e}")
                return folder, 0.0
            raise
        detail = getattr(result, "detail", photo)
        if getattr(detail, "from_album", None) is None and getattr(photo, "from_album", None) is not None:
            detail.from_album = photo.from_album
        if not getattr(photo, "name", "") and getattr(detail, "name", ""):
            photo.name = detail.name
        folder = self.cache.chapter_dir(detail, album_dir) or self.cache.chapter_dir(photo, album_dir)
        return folder, float(getattr(result, "duration", 0) or 0)

    # ========== 已发送页段去重 ==========
    @staticmethod
    def add_sent_ranges(sent_ranges, photo_id, start, end):
        """记录本次批量已经发送的章节页段，并合并相邻/重叠区间"""
        key = str(photo_id)
        ranges = sent_ranges.setdefault(key, [])
        ranges.append((int(start), int(end)))
        ranges.sort()
        merged = []
        for item_start, item_end in ranges:
            if merged and item_start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], item_end))
            else:
                merged.append((item_start, item_end))
        sent_ranges[key] = merged

    @staticmethod
    def range_covered(sent_ranges, photo_id, start, end):
        for item_start, item_end in sent_ranges.get(str(photo_id), []):
            if item_start <= start and item_end >= end:
                return True
        return False

    def filter_duplicate_selections(self, selected, album_dir, sent_ranges):
        """过滤本批次中已经发送过的章节/页段，避免批量目标重复发同一文件"""
        result = []
        skipped = []
        for index, photo, page_range in selected:
            total = len(self.cache.chapter_image_paths(photo, album_dir))
            if total <= 0:
                result.append((index, photo, page_range))
                continue
            start = int(page_range[0]) if page_range else 1
            end = int(page_range[1]) if page_range and page_range[1] is not None else total
            if self.range_covered(sent_ranges, getattr(photo, "photo_id", index), start, end):
                skipped.append(index)
                continue
            result.append((index, photo, page_range))
        return result, skipped
