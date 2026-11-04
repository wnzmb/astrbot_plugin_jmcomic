"""下载执行：整本/单章下载、重试与部分失败处理、已发送页段去重。"""

import asyncio

from astrbot.api import logger

from ..core import jmcompat
from ..core.downloader import ProgressDownloader, page_limit


class DownloadService:
    def __init__(self, settings, client, cache, messaging):
        self.settings = settings
        self.client = client
        self.cache = cache
        self.messaging = messaging

    def _retry_attempts(self):
        return max(
            1, int(getattr(self.settings, "download_retry_times", 3) or 1)
        )

    def _retry_interval(self):
        return max(
            0.0,
            float(getattr(self.settings, "download_retry_interval", 3.0) or 0.0),
        )

    # ========== 整本下载 ==========
    def album_cached_pages(self, folder, chapters):
        total = 0
        for photo in chapters or []:
            chapter = self.cache.chapter_dir(photo, folder)
            if chapter is not None:
                total += len(self.cache.chapter_images(chapter))
        return total

    def album_cache_ready(self, folder, chapters, expected_pages=0):
        if folder is None or not self.cache.album_cache_complete(folder, chapters):
            return False
        try:
            expected = int(expected_pages or 0)
        except (TypeError, ValueError):
            expected = 0
        if expected > 0 and chapters:
            return self.album_cached_pages(folder, chapters) >= expected
        return True

    async def download_album_progress(self, event, album_id, holder, chapters=None,
                                      expected_pages=0):
        """下载整本，结果写入 holder['dir']。

        - 只有所有章节都已有图片时才跳过下载，避免半成品缓存产出残缺 PDF；
        - expected_pages 用 album.page_count 对账，识别“每章都有目录但总页数不足”的连续缺页；
        - 图片/CDN 瞬时失败时自动重试，已下载图片会复用缓存；
        - 重试后仍有失败时，允许发送已完成章节，并由上层报告部分失败。
        """
        folder = self.cache.find_album_dir(album_id)
        if self.album_cache_ready(folder, chapters, expected_pages):
            holder["dir"] = folder
            return

        attempts = self._retry_attempts()
        for attempt in range(1, attempts + 1):
            jm = jmcompat.module()
            if jm is None:
                raise RuntimeError("未找到可用的 jmcomic，请检查插件依赖")
            partial_exc = jmcompat.exceptions(
                "PartialDownloadFailedException", "RequestRetryAllFailException"
            )
            cancelled_exc = jmcompat.exceptions("DownloadCancelledException")
            ProgressDownloader._progress.pop(str(album_id), None)
            # 2.7.9+ 的协作式取消：只 cancel asyncio 任务无法中断
            # to_thread 里的同步下载，必须同时置位 DownloadControl。
            control = jm.DownloadControl()
            task = asyncio.create_task(
                jm.download_album_async(
                    album_id,
                    self.client.current_option(),
                    downloader=ProgressDownloader.current_impl(),
                    control=control,
                )
            )
            try:
                await task
            except asyncio.CancelledError:
                # 用户执行 /jmstop 时，先发协作式取消信号让下载线程尽快退出，
                # 再取消 asyncio 任务并等待其收敛。
                control.cancel("用户执行 /jmstop")
                if not task.done():
                    task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise
            except cancelled_exc:
                # 库侧检测到 control 已取消，转成标准取消信号，
                # 交给 handlers/download.py 统一回复「已停止」。
                raise asyncio.CancelledError() from None
            except partial_exc as e:
                folder = self.cache.find_album_dir(album_id)
                if attempt < attempts:
                    yield self.messaging.plain(event,
                        f"⚠️ 第{attempt}次下载有图片失败（{type(e).__name__}），"
                        "正在重试，已下载图片会复用缓存..."
                    )
                    await asyncio.sleep(self._retry_interval())
                    continue
                if folder is not None and self.cache.album_dir_has_images(folder):
                    holder["dir"] = folder
                    holder["partial"] = True
                    yield self.messaging.plain(event,
                        "⚠️ 重试后仍有图片下载失败，先发送已完成话；"
                        "缺失图片已保留缓存，稍后重跑命令可继续补齐。"
                    )
                    return
                raise

            folder = self.cache.find_album_dir(album_id)
            holder["dir"] = folder
            if folder is None:
                yield self.messaging.plain(event, "⚠️ 下载完成但找不到漫画目录")
            elif chapters and not self.album_cache_ready(folder, chapters, expected_pages):
                holder["partial"] = True
                yield self.messaging.plain(event,
                    "⚠️ 下载结束但仍有话缺失，将先发送已完成话；"
                    "稍后重跑命令可继续补齐。"
                )
            return

    # ========== 单章下载 ==========
    async def is_photo_complete(self, photo, folder, max_page=None):
        """章节缓存是否完整。

        album 列表里的 photo 通常没有 page_arr，只有拉过详情才知道总页数；
        因此缓存目录已存在时补一次详情请求，避免把 5/38 页的半成品当成完整缓存。
        max_page 用于页段下载：此时只缓存了 1..N 的连续前缀，按 N 判定完整。
        """
        if folder is None:
            return False
        actual = len(self.cache.chapter_images(folder))
        if actual <= 0:
            return False
        expected = len(getattr(photo, "page_arr", None) or [])
        if expected <= 0:
            try:
                expected = int(getattr(photo, "page_count", 0) or 0)
            except (TypeError, ValueError):
                expected = 0
        if expected <= 0:
            try:
                client = await asyncio.to_thread(self.client.get_client)
                detail = await asyncio.to_thread(
                    client.get_photo_detail,
                    photo.photo_id,
                    fetch_album=False,
                    fetch_scramble_id=False,
                )
                page_arr = getattr(detail, "page_arr", None)
                expected = len(page_arr or [])
                if expected:
                    photo.page_arr = page_arr
                if not getattr(photo, "name", "") and getattr(detail, "name", ""):
                    photo.name = detail.name
            except Exception as e:
                logger.warning(f"校验章节页数失败，按已有缓存继续: {e}")
        if expected <= 0:
            try:
                expected = int(self.cache.read_metadata(folder).get("page_count") or 0)
            except (TypeError, ValueError):
                expected = 0
        if expected <= 0:
            return True
        return self.cache.chapter_complete(photo, folder=folder, max_page=max_page)

    async def ensure_photo_downloaded(self, photo, album_dir=None, max_page=None):
        """确保单章已下载且页数完整，返回章节目录。

        max_page 不为空时只下载 1..max_page 的连续前缀（页段请求），避免整话
        下载；后续请求更大页码时会复用已有图片并补齐差额。
        """
        folder = self.cache.chapter_dir(photo, album_dir)
        if folder is not None and await self.is_photo_complete(
            photo, folder, max_page=max_page
        ):
            return folder

        attempts = self._retry_attempts()
        result = None
        for attempt in range(1, attempts + 1):
            jm = jmcompat.module()
            if jm is None:
                raise RuntimeError("未找到可用的 jmcomic，请检查插件依赖")
            partial_exc = jmcompat.exceptions(
                "PartialDownloadFailedException", "RequestRetryAllFailException"
            )
            cancelled_exc = jmcompat.exceptions("DownloadCancelledException")
            control = jm.DownloadControl()
            try:
                with page_limit(max_page):
                    result = await jm.download_photo_async(
                        photo.photo_id,
                        self.client.current_option(),
                        downloader=ProgressDownloader.current_impl(),
                        control=control,
                    )
                break
            except asyncio.CancelledError:
                # 单章下载直接 await，取消时同样要通知库侧停止线程内下载
                control.cancel("用户执行 /jmstop")
                raise
            except cancelled_exc:
                raise asyncio.CancelledError() from None
            except partial_exc as e:
                folder = self.cache.chapter_dir(photo, album_dir)
                if attempt < attempts:
                    logger.warning(
                        f"第{getattr(photo, 'photo_id', '')}话第{attempt}次下载失败，"
                        f"稍后重试: {e}"
                    )
                    await asyncio.sleep(self._retry_interval())
                    continue
                if folder is not None and self.cache.album_dir_has_images(folder):
                    logger.warning(
                        f"第{getattr(photo, 'photo_id', '')}话部分图片失败，"
                        f"先使用已下载图片: {e}"
                    )
                    return folder
                raise
        if result is None:
            return None
        detail = getattr(result, "detail", photo)
        if getattr(detail, "from_album", None) is None and getattr(photo, "from_album", None) is not None:
            detail.from_album = photo.from_album
        # 把服务端总页数带回 album 列表对象，供 metadata 记录“应有页数”，
        # 后续判断半成品缓存时不需要再请求详情。
        if getattr(detail, "page_arr", None) and not getattr(photo, "page_arr", None):
            photo.page_arr = detail.page_arr
        if not getattr(photo, "name", "") and getattr(detail, "name", ""):
            photo.name = detail.name
        folder = self.cache.chapter_dir(detail, album_dir) or self.cache.chapter_dir(photo, album_dir)
        return folder

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
