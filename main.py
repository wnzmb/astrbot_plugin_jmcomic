import os
import csv
import asyncio
import zipfile
import math
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

import jmcomic
from jmcomic import JmMagicConstants, JmModuleConfig
from .core.client import JmClientManager
from .core.config import PluginSettings
from .core.state import PluginState
from .core.downloader import ProgressDownloader
from .core.parsing import format_index_ranges, parse_positive_page
from .services.access import AccessService
from .services.cache import CacheService
from .services.catalog import CatalogService
from .services.download import DownloadService
from .services.messaging import MessagingService
from .services.packaging import PackagingService
from .services.pagination import PageEventProxy, PageSessionService
from .services.history import HistoryService
from .services.progress import ActiveDownloadService
from .services.quota import FileQuotaService
from .services.selection import SelectionService
from .services.text import TextService

@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.7.8")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}

        # 配置解析与路径/option 构建都在 core.config，这里只做组装。
        # 插件目录由 main.py 传入，避免 core 层用 __file__ 推算导致 data_dir 错两级。
        self.settings = PluginSettings.from_config(
            self.config, Path(os.path.dirname(__file__))
        )
        self.settings.prepare_directories()

        settings = self.settings
        # 兼容层：现有命令实现仍读取插件实例上的这些属性，
        # 后续按批次迁移到 service 后逐项删除。
        self.data_dir = settings.data_dir
        self.download_base_dir = settings.download_base_dir
        self.zip_base_dir = settings.zip_base_dir
        self.send_state_file = settings.send_state_file
        self.history_file = settings.history_file
        self.checkin_file = settings.checkin_file
        self.subscription_file = settings.subscription_file
        self.per_page = settings.per_page
        self.allow_group = settings.allow_group
        self.enabled_groups = settings.enabled_groups
        self.numeric_search_as_album = settings.numeric_search_as_album
        self.subscribe_interval = settings.subscribe_interval
        self.convert_enabled = settings.convert_enabled
        self.convert_title = settings.convert_title
        self.convert_tags = settings.convert_tags
        self.convert_author = settings.convert_author
        self.convert_comment = settings.convert_comment
        self.sensitive_filter = settings.sensitive_filter
        self.tease_enabled = settings.tease_enabled
        self.cache_days = settings.cache_days
        self.comment_count = settings.comment_count
        self.history_max = settings.history_max
        self.dual_search = settings.dual_search
        self.owner_ids = settings.owner_ids
        self.daily_file_limit = settings.daily_file_limit
        self.hourly_file_limit = settings.hourly_file_limit
        self.send_interval = settings.send_interval
        self.zip_group_size = settings.zip_group_size
        self.pdf_page_limit = settings.pdf_page_limit
        self.zip_page_limit = settings.zip_page_limit
        self.page_turn_timeout = settings.page_turn_timeout
        self.use_proxy = settings.use_proxy
        self.proxy_url = settings.proxy_url
        self.client_type = settings.client_type
        self.custom_sensitive_map = settings.custom_sensitive_map
        self.option = settings.option
        self.order_map = settings.order_map

        self.state = PluginState()
        self.access = AccessService(self.settings)
        self.pagination = PageSessionService(self.settings, self.state)
        self.cache = CacheService(self.settings)
        self.text = TextService(self.settings)
        self.catalog = CatalogService(self.settings, self.cache, self.text)
        self.messaging = MessagingService(self.settings, self.text)
        self.quota = FileQuotaService(self.settings)
        self.history = HistoryService(self.settings)
        self.progress = ActiveDownloadService(self.state)
        self.selection = SelectionService(self.settings, self.cache, self.state)
        self.packaging = PackagingService(self.settings, self.catalog)
        self.download = DownloadService(self.settings, self.cache, self.messaging)

        # 登录态客户端缓存：避免每次命令都重新登录
        self.client = JmClientManager(self.settings)

        logger.info(
            f"JMComic 插件初始化完成 v0.7.8，客户端: {self.client_type.upper()}，"
            f"镜像站: {settings.custom_domains}，代理: {'已启用' if self.use_proxy else '直连'}"
        )

    # ========== 工具方法 ==========

    def _zip_folder(self, folder_path: Path, zip_path: Path):
        """打包文件夹为ZIP"""
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_fullpath = Path(root) / file
                    arcname = file_fullpath.relative_to(folder_path)
                    zipf.write(file_fullpath, arcname)
        return zip_path

    # ========== 下载核心：下载进度 / 限额 ==========

    def _tease_for_count(self, count):
        mapping = {
            3: ["兄弟，今天已经下3本了，悠着点"],
            5: ["🚨 5本了！再看下去要樯橹灰飞烟灭了", "🚨 提醒：5本了，身体要紧啊兄弟"],
            7: ["差不多得了，出去看看太阳吧，别一直盯着屏幕"],
            10: ["🚨 警告：10本！你这频率比我上班打卡都勤", "🚨 10本了！再看下去腰都要坐坏了"],
            15: ["你这下载量，网站管理员都要注意到你了"],
            20: ["🚨 严重警告：20本！JM官方都要给你发终身VIP了", "🚨 20本了！你这是准备把整个网站搬回家？"],
            30: ["你这用量，键盘都要被你磨包浆了"],
            50: ["🚨 50本了兄弟！建议今天到此为止，明天再看"],
        }
        return mapping.get(int(count), [])

    async def _run_download(self, event, target_str, output_format,
                            chapter_expr=None):
        """统一下载入口：PDF/ZIP、单本/批量、漫画ID/章节ID、QQ文件限额"""
        raw_targets = [x.strip() for x in str(target_str).split(",") if x.strip()]
        if not raw_targets:
            yield self.messaging.plain(event, "❌ 未解析到有效ID")
            return

        targets = []
        seen = set()
        for raw in raw_targets:
            prefix, target_id = self.selection.normalize_target(raw)
            key = f"{prefix}:{target_id}" if prefix else target_id
            if key in seen:
                continue
            seen.add(key)
            targets.append((raw, prefix, target_id))

        if not targets:
            yield self.messaging.plain(event, "❌ 未解析到有效ID")
            return

        fmt_label = "PDF" if output_format == "pdf" else "ZIP"

        try:
            client = self.client.get_client()
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "登录"))
            return

        success = 0
        failed = []
        total_files = 0
        total_bytes = 0
        quota_stopped = False
        sent_ranges = {}
        user_id = str(event.get_sender_id())
        message_budget = self.messaging.message_budget(event)
        sent_messages = 0
        tease_text = ""

        yield self.messaging.plain(
            event,
            f"⬇️ 开始下载：{len(targets)} 个目标，输出 {fmt_label}。"
            "发送 /jmnow 查看当前进度。",
        )
        sent_messages += 1

        for target_index, (raw, prefix, target_id) in enumerate(targets):
            target_label = f"[{target_index + 1}/{len(targets)}] JM{target_id}"
            try:
                target = await self.selection.resolve_target(client, raw)
            except Exception as e:
                failed.append(raw)
                yield self.messaging.plain(event,
                    f"❌ {target_label} 识别失败：{self.client.handle_exception(e, '识别')}"
                )
                continue

            kind = target["kind"]
            chapters = target["chapters"]
            if not chapters:
                failed.append(raw)
                yield self.messaging.plain(event, f"⚠️ {target_label} 没有话")
                continue

            if kind == "album" and chapter_expr:
                selected, invalid, notes = self.selection.select_chapters(chapters, chapter_expr)
                if notes:
                    yield self.messaging.plain(event, "⚠️ 话提示：" + "；".join(notes))
                if invalid:
                    yield self.messaging.plain(event, f"⚠️ 忽略无效话/页段序号：{', '.join(invalid)}")
                if not selected:
                    failed.append(raw)
                    yield self.messaging.plain(event, f"⚠️ {target_label} 没有选中任何话")
                    continue
            elif kind == "album":
                selected = [(i + 1, ch, None) for i, ch in enumerate(chapters)]
            else:
                if chapter_expr and (":" in chapter_expr or "：" in chapter_expr):
                    selected, invalid, notes = self.selection.select_chapters(chapters, chapter_expr)
                    if notes:
                        yield self.messaging.plain(event, "⚠️ 页码提示：" + "；".join(notes))
                    if invalid:
                        yield self.messaging.plain(event,
                            f"⚠️ 忽略无效页段：{', '.join(invalid)}"
                        )
                    if not selected:
                        failed.append(raw)
                        yield self.messaging.plain(event, f"⚠️ {target_label} 页码段无效")
                        continue
                else:
                    selected = [(1, chapters[0], None)]

            album = target.get("album")
            album_id = str(
                getattr(album, "album_id", "")
                or getattr(target.get("photo"), "album_id", "")
                or (target_id if kind == "album" else "")
            )
            album_title = getattr(album, "name", "") if album is not None else ""
            selected_total = sum(
                self.catalog.photo_range_pages(photo, page_range)
                for _index, photo, page_range in selected
            )
            if selected_total <= 0 and kind == "album" and not chapter_expr:
                try:
                    selected_total = max(0, int(getattr(album, "page_count", 0) or 0))
                except (TypeError, ValueError):
                    selected_total = 0
            self.progress.update(
                user_id,
                album_id=album_id or target_id,
                total=selected_total,
                done=0,
                stage="downloading",
            )
            album_dir = None
            # 选章/单章先找已有漫画目录，便于复用已有图片缓存
            if album_id and not (kind == "album" and not chapter_expr):
                album_dir = self.cache.find_album_dir(album_id)

            # 整本下载走 download_album_async；选章/单章逐章下载
            if kind == "album" and not chapter_expr:
                holder = {}
                try:
                    async for message in self.download.download_album_progress(
                        event, target_id, holder, chapters=chapters
                    ):
                        sent_messages += 1
                        yield message
                except Exception as e:
                    failed.append(raw)
                    yield self.messaging.plain(event,
                        f"❌ {target_label} 下载失败：{self.client.handle_exception(e, '下载')}"
                    )
                    continue
                album_dir = holder.get("dir")
                if album_dir is None:
                    failed.append(raw)
                    continue
                self.progress.update(
                    user_id, done=selected_total, total=selected_total, stage="packaging"
                )
            else:
                # 同一章节可能有多个不连续页段，下载时按章节去重，
                # 避免重复请求同一章、并把刚下载的章误报成“已有缓存”。
                download_items = []
                seen_photos = set()
                selected_pages = {}
                for original_index, photo, page_range in selected:
                    photo_key = str(getattr(photo, "photo_id", original_index))
                    selected_pages[photo_key] = (
                        selected_pages.get(photo_key, 0)
                        + self.catalog.photo_range_pages(photo, page_range)
                    )
                    if photo_key in seen_photos:
                        continue
                    seen_photos.add(photo_key)
                    download_items.append((original_index, photo))

                done_pages = 0
                for original_index, photo in download_items:
                    photo_key = str(getattr(photo, "photo_id", original_index))
                    if self.cache.chapter_dir(photo, album_dir) is not None:
                        done_pages += selected_pages.get(photo_key, 0)
                        self.progress.update(user_id, done=done_pages)
                        continue
                    try:
                        await self.download.ensure_photo_downloaded(photo, album_dir)
                    except Exception as e:
                        yield self.messaging.plain(event,
                            f"❌ 第{original_index}话下载失败：{e}"
                        )
                        continue
                    done_pages += selected_pages.get(photo_key, 0)
                    self.progress.update(user_id, done=done_pages)
                self.progress.update(
                    user_id, done=done_pages, stage="packaging"
                )

            if album_id and album_dir is None:
                album_dir = self.cache.find_album_dir(album_id)
            if album_dir is not None:
                self.catalog.save_target_metadata(
                    album,
                    album_id,
                    chapters,
                    album_dir,
                    selected_photos=[photo for _index, photo, _range in selected],
                )
                album_title = self.catalog.album_display_title(album, album_dir) or album_title

            # 下载完成后按实际图片数夹取页码，再规划输出文件
            selected, range_notes = self.selection.normalize_selected_ranges(selected, album_dir)
            if range_notes:
                yield self.messaging.plain(event, "⚠️ 页码提示：" + "；".join(range_notes))
            selected, duplicate_indexes = self.download.filter_duplicate_selections(
                selected, album_dir, sent_ranges
            )
            if duplicate_indexes:
                yield self.messaging.plain(
                    event,
                    f"ℹ️ {target_label} 第{format_index_ranges(duplicate_indexes)}话"
                    "已在本批次发送过，跳过重复文件"
                )
            if not selected:
                if duplicate_indexes:
                    success += 1
                else:
                    failed.append(raw)
                    yield self.messaging.plain(event, f"⚠️ {target_label} 没有可用的话页段")
                continue

            # PDF：整章优先，单章超限按页切；ZIP：章数 + 页数双上限
            if output_format == "pdf":
                groups = self.selection.plan_pdf_groups(selected, album_dir)
            else:
                groups = self.selection.plan_zip_groups(selected, album_dir)

            target_sizes = []
            target_sent = 0
            target_failed = 0
            if kind == "photo":
                target_title = self.catalog.chapter_display_title(
                    target.get("photo"), 1, None, album_dir
                ) or target_id
            else:
                target_title = album_title or target_id

            for group_index, group in enumerate(groups):
                quota = self.quota.file_quota()
                if self.quota.quota_reached(quota):
                    message = self.quota.block_text(len(groups) - group_index)
                    remaining_expr = self.selection.resume_expr(groups[group_index:])
                    command = "/jm" if output_format == "pdf" else "/jmzip"
                    resume_target = f"p{target_id}" if kind == "photo" else target_id
                    if remaining_expr:
                        message += f"\n稍后继续：{command} {resume_target} -c {remaining_expr}"
                    yield self.messaging.plain(event, message)
                    if target_index + 1 < len(targets):
                        yield self.messaging.plain(event,
                            "未处理的目标：" + ", ".join(x[0] for x in targets[target_index + 1:])
                        )
                    quota_stopped = True
                    break

                chapter_entries = []
                for unit in group:
                    folder = self.cache.chapter_dir(unit.photo, album_dir)
                    if folder is None:
                        try:
                            folder, _ = await self.download.ensure_photo_downloaded(unit.photo, album_dir)
                        except Exception as e:
                            logger.warning(f"第{unit.index}话补下载失败: {e}")
                            folder = None
                    if folder is None:
                        target_failed += 1
                        continue
                    images = self.cache.chapter_images(folder)
                    if not images:
                        target_failed += 1
                        continue
                    # 规划后图片数可能变化，按实际图片再夹一次
                    start = max(1, min(unit.start, len(images)))
                    end = min(unit.end, len(images))
                    if end < start:
                        target_failed += 1
                        continue
                    unit = unit._replace(start=start, end=end, total=len(images))
                    chapter_entries.append((unit, folder, images[start - 1:end]))

                if not chapter_entries:
                    yield self.messaging.plain(event,
                        f"⚠️ {target_label} 第{group[0].index}话没有图片"
                    )
                    continue

                source_dirs = [folder for _, folder, _ in chapter_entries]
                album_metadata_path = self._metadata_file(album_dir)
                if album_metadata_path is not None and album_metadata_path.is_file():
                    source_dirs = source_dirs + [album_metadata_path]
                image_paths = [
                    image for _, _, images in chapter_entries for image in images
                ]
                info_text = self.catalog.album_info_text(
                    album, album_id, album_dir, chapter_entries
                )
                cover_text = None
                pdf_metadata = None
                if output_format == "pdf":
                    pdf_metadata = self.catalog.pdf_metadata(
                        album, album_id, album_dir, chapter_entries
                    )
                    # 每个 PDF 分册都带漫画信息页，便于单独辨认分册。
                    cover_text = self.catalog.album_cover_text(
                        album, album_id, album_dir, chapter_entries
                    )
                try:
                    stem = self.packaging.file_stem_for_entries(
                        chapter_entries, album_id, album_title, album_dir
                    )
                    output_path = self.zip_base_dir / f"{stem}.{output_format}"
                    if output_format == "pdf":
                        built = await asyncio.to_thread(
                            self.packaging.build_pdf, image_paths, output_path, source_dirs,
                            pdf_metadata, cover_text,
                        )
                    else:
                        multi = len(chapter_entries) > 1
                        zip_entries = [
                            (images, self.packaging.zip_arc_dir(unit, folder, multi, album_dir))
                            for unit, folder, images in chapter_entries
                        ]
                        built = await asyncio.to_thread(
                            self.packaging.build_zip, zip_entries, output_path, source_dirs,
                            info_text,
                        )
                except Exception as e:
                    yield self.messaging.plain(event, f"❌ 生成文件失败：{e}")
                    target_failed += len(group)
                    continue

                if not built or not output_path.exists():
                    yield self.messaging.plain(event, "❌ 生成文件失败（没有可用图片）")
                    target_failed += len(group)
                    continue

                size_mb = output_path.stat().st_size / 1024 / 1024
                self.quota.record_file_send()
                total_files += 1
                target_sent += 1
                for unit, _folder, _images in chapter_entries:
                    self.download.add_sent_ranges(
                        sent_ranges, getattr(unit.photo, "photo_id", unit.index),
                        unit.start, unit.end,
                    )
                total_bytes += output_path.stat().st_size
                yield self.messaging.chain(event, [
                    Comp.Plain(f"📎 {output_path.name} ({size_mb:.1f}MB)\n"),
                    Comp.File(file=str(output_path), name=output_path.name),
                ])
                target_sizes.append(size_mb)
                output_path.unlink(missing_ok=True)
                await asyncio.sleep(self.send_interval)

            if target_sent:
                total_count = self.history.add_user(event.get_sender_id(), {
                    "id": target_id,
                    "title": target_title,
                    "time": time.strftime("%Y-%m-%d %H:%M"),
                    "size_mb": round(sum(target_sizes), 1),
                })
                success += 1
                if not quota_stopped and self.tease_enabled:
                    teases = self._tease_for_count(total_count)
                    if teases:
                        import random
                        tease_text = random.choice(teases)
            if target_failed:
                failed.append(f"{raw}(部分话失败)" if target_sent else raw)

            if quota_stopped:
                break

        final_lines = []
        if not quota_stopped:
            final_lines.append(
                f"📊 下载完成：成功 {success}/{len(targets)} 个目标，"
                f"发送 {total_files} 个文件，总计 {total_bytes / 1024 / 1024:.1f}MB\n"
                f"{self.quota.status_text()}"
            )
        if tease_text:
            final_lines.append(tease_text)
        if failed:
            final_lines.append(f"⚠️ 失败目标：{', '.join(failed)}")
        # QQ 官方被动消息有 4/5 条预算；1 条开始状态 + 文件数已占满时，
        # 省略收尾汇总，避免触发主动消息重试和额外延迟。
        if final_lines and (
            message_budget is None
            or sent_messages + total_files < message_budget
        ):
            yield self.messaging.plain(event, "\n".join(final_lines))
        elif final_lines:
            logger.info(
                "QQ 被动消息预算已用尽，省略下载收尾汇总：%s",
                " | ".join(final_lines),
            )

    def _parse_search_args(self, args):
        """
        解析搜索参数：关键词 + 可选页码 + 可选排序。
        关键词允许包含空格：只把末尾的页码/排序当作参数，其余片段合并为关键词，
        避免 `jms 纯  爱` 被拆成关键词“纯” + 非法参数“爱”。
        支持格式：
          /jms 关键词
          /jms 多词 关键词
          /jms 关键词 2
          /jms 关键词 mv
          /jms 关键词 2 mv
        返回：(keyword, qq_page, order_by)
        """
        tokens = [str(x) for x in args[1:] if str(x).strip()]
        if not tokens:
            raise ValueError("关键词不能为空")

        qq_page = 1
        order_by = JmMagicConstants.ORDER_BY_LATEST  # 默认最新
        page_set = False
        sort_set = False

        # 从末尾消费页码/排序；至少保留一个片段作为关键词，
        # 这样 `/jms 2` 仍会被当作关键词“2”，而不是页码。
        while len(tokens) > 1:
            last = tokens[-1]
            if not sort_set and last in self.order_map:
                order_by = last
                sort_set = True
                tokens.pop()
                continue
            if not page_set and re.fullmatch(r"[+-]?\d+", last):
                value = int(last)
                if value < 1:
                    raise ValueError("页码必须是大于 0 的整数")
                qq_page = value
                page_set = True
                tokens.pop()
                continue
            break

        keyword = re.sub(r"\s+", " ", " ".join(tokens)).strip()
        if not keyword:
            raise ValueError("关键词不能为空")
        return keyword, qq_page, order_by

    async def _render_search_page(self, event, client, search_method, keyword, qq_page, order_by, title_prefix="", command_name="jms"):
        """通用搜索结果渲染：分页 + 切片 + 输出"""
        self.pagination.clear(event)
        PER_QQ_PAGE = self.per_page
        PER_WEB_PAGE = 80
        QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

        web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
        offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

        order_name = self.order_map.get(order_by, order_by)
        display_keyword = self.text.safe_text(keyword, self.convert_title)
        no_result_text = f"没有找到与「{display_keyword}」相关的漫画，请换个关键词试试"
        yield self.messaging.plain(event, f"🔍 {title_prefix}{display_keyword}（{order_name}，第{qq_page}页）...")

        try:
            total = None
            totals = []
            if self.dual_search:
                # 搜两次：原词 + 繁体词，合并去重，适配半简半繁标题
                import zhconv
                keyword_traditional = zhconv.convert(keyword, 'zh-tw')
                # 如果原词和繁体词相同，只搜一次
                search_keywords = [keyword] if keyword == keyword_traditional else [keyword, keyword_traditional]
                all_items = []
                for kw in search_keywords:
                    page = await asyncio.to_thread(search_method, kw, web_page, order_by)
                    page_total = getattr(page, 'total', None)
                    if page_total is not None:
                        totals.append(int(page_total))
                    all_items.extend(list(page.iter_id_title_tag()))
                if totals:
                    total = max(totals)
                # 去重
                seen = set()
                unique_items = []
                for item in all_items:
                    if item[0] not in seen:
                        seen.add(item[0])
                        unique_items.append(item)
                all_items = unique_items
            else:
                page = await asyncio.to_thread(search_method, keyword, web_page, order_by)
                total = getattr(page, 'total', None)
                all_items = list(page.iter_id_title_tag())
            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            # 上游 API 对超大页码会 clamp 到最后一页，不能只看返回是否为空。
            total_int = None
            if total is not None:
                try:
                    total_int = int(total)
                except (TypeError, ValueError):
                    total_int = None
            if total_int is not None and total_int <= 0:
                self.pagination.clear(event)
                yield self.messaging.plain(event, no_result_text)
                return
            if total_int is not None:
                max_page = math.ceil(total_int / PER_QQ_PAGE)
                if qq_page > max_page:
                    self.pagination.clear(event)
                    yield self.messaging.plain(event,
                        f"第{qq_page}页没有更多结果了（共{total_int}条，最多{max_page}页）"
                    )
                    return

            if not page_items:
                self.pagination.clear(event)
                if total_int is not None:
                    yield self.messaging.plain(event, f"第{qq_page}页没有更多结果了")
                else:
                    yield self.messaging.plain(event, no_result_text)
                return

            results = []
            for i, (aid, item_title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                item_title = self.text.safe_text(item_title, self.convert_title)
                tags = [self.text.safe_text(t, self.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {item_title}{tag_str}")

            reply = f"{title_prefix}：{display_keyword}（{order_name}）\n"
            global_start = (qq_page - 1) * PER_QQ_PAGE
            range_text = f"第{qq_page}页，第{global_start + 1}-{global_start + len(page_items)}条"
            if total_int is not None:
                range_text += f"，共{total_int}条"
            reply += range_text + "\n\n" + "\n".join(results)
            reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"

            # 下一页提示：保留当前搜索命令和排序参数。
            # 关键词若会被敏感词转换，就不把原词写进命令，避免命令本身触发违规。
            next_keyword = keyword if self.text.safe_text(keyword, self.convert_title) == keyword else "<关键词>"
            next_cmd = f"/{command_name} {next_keyword} {qq_page + 1}"
            internal_next_cmd = f"/{command_name} {keyword} {qq_page + 1}"
            if order_by != JmMagicConstants.ORDER_BY_LATEST:
                next_cmd += f" {order_by}"
                internal_next_cmd += f" {order_by}"
            reply += f"\n下一页：{next_cmd}"

            max_page = (
                math.ceil(total_int / PER_QQ_PAGE)
                if total_int is not None
                else None
            )
            self.pagination.remember(
                event, internal_next_cmd, qq_page, max_page
            )
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "搜索"))

    # ========== /jmv 查详情 ==========
    @filter.command("jmv")
    async def jm_view(self, event: AstrMessageEvent):
        """查看漫画详情，用法：/jmv <id> [选项]
        选项：
          -author  去掉作者
          -work    去掉作品
          -role    去掉人物
          -tag     去掉标签
          -pic     不发送封面
        示例：/jmv 123456 -role -work
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：/jmv <漫画id> [选项]\n"
                "选项：-author 去掉作者 | -work 去掉作品 | -role 去掉人物 | -tag 去掉标签\n"
                "      -pic 不发送封面\n"
                "示例：/jmv 350234 或 /jmv 350234 -role -work"
            )
            return

        raw_id = args[1].strip()
        if raw_id.lower().startswith("jm"):
            raw_id = raw_id[2:].strip()
        if raw_id.lower().startswith("a"):
            raw_id = raw_id[1:].strip()
        if not raw_id.isdigit():
            yield self.messaging.plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "示例：/jmv 350234 -role"
            )
            return
        album_id = raw_id

        valid_options = {"-author", "-work", "-role", "-tag", "-pic"}
        unknown_options = [token for token in args[2:] if token.lower() not in valid_options]
        if unknown_options:
            yield self.messaging.plain(event,
                f"❌ 无法识别的选项：{', '.join(unknown_options)}\n"
                "可用选项：-author 不带作者 | -work 不带作品 | -role 不带角色 | "
                "-tag 不带标签 | -pic 不发封面"
            )
            return
        options = {token.lower() for token in args[2:]}

        option_labels = []
        for opt, label in (
            ("-author", "不带作者"),
            ("-work", "不带作品"),
            ("-role", "不带角色"),
            ("-tag", "不带标签"),
            ("-pic", "不发封面"),
        ):
            if opt in options:
                option_labels.append(label)
        option_suffix = f"（{'、'.join(option_labels)}）" if option_labels else ""
        yield self.messaging.plain(event, f"🔍 查询 JM{album_id} ...{option_suffix}")

        try:
            client = self.client.get_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)

            if self.catalog.looks_like_chapter_album(album):
                parent_id = await self.catalog.resolve_chapter_album_parent(client, album, album_id)
                if parent_id:
                    parent_line = (
                        f"该话所属漫画：JM{parent_id}\n"
                        f"请用 /jm {album_id} 下载该话，或用 /jmv {parent_id} 查看漫画。"
                    )
                else:
                    parent_line = (
                        f"请用 /jm {album_id} 下载该话；"
                        "章节所属漫画ID未能识别，可先执行 /jm 后查看缓存目录。"
                    )
                yield self.messaging.plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                )
                return

            chapters = list(album) if hasattr(album, '__iter__') else []
            authors = getattr(album, 'authors', [])
            works = getattr(album, 'works', [])
            actors = getattr(album, 'actors', [])
            tags = getattr(album, 'tags', [])
            page_count = getattr(album, 'page_count', 0) or 0
            comment_count = getattr(album, 'comment_count', 0) or 0
            pub_date = getattr(album, 'pub_date', '') or ''
            update_date = getattr(album, 'update_date', '') or ''
            likes = getattr(album, 'likes', '') or ''
            views = getattr(album, 'views', '') or ''

            # API客户端 page_count 可能为0，从章节累加计算
            if page_count == 0 and chapters:
                page_count = sum(len(getattr(p, 'page_arr', []) or []) for p in chapters)

            def join_values(values, simplify):
                if not values:
                    return "无"
                return ", ".join(self.text.safe_text(str(v), simplify) for v in values)

            def display_date(value):
                text = str(value or "").strip()
                return "未知" if text in ("", "0", "None") else text

            title = self.text.safe_text(getattr(album, 'name', ''), self.convert_title) or "未知"
            author_str = join_values(authors, self.convert_author)
            work_str = join_values(works, self.convert_title)
            actor_str = join_values(actors, self.convert_tags)
            tag_str = join_values(tags, self.convert_tags)

            sep = "━━━━━━━━━━━━━━━"
            pub_text = display_date(pub_date)
            update_text = display_date(update_date)
            if update_text == "未知" and pub_text != "未知":
                # API 不提供 update_date 时退回发布日期，避免所有漫画都显示“未知”
                update_text = pub_text
            lines = [
                sep,
                f"  📖 标题:  {title}",
                f"  🆔 ID:    JM{album_id}",
            ]
            if '-author' not in options:
                lines.append(f"  ✍️ 作者:  {author_str}")
            lines.append(sep)
            lines.append(f"  📅 发布日期:  {pub_text}")
            if update_text != "未知":
                lines.append(f"  📅 更新日期:  {update_text}")
            lines.append(f"  📄 总页数:    {page_count if page_count else '未知'}")
            lines.append(f"  👀 观看:      {views or '未知'}")
            lines.append(f"  ❤️ 点赞:      {likes or '未知'}")
            lines.append(f"  💬 评论:      {comment_count if comment_count else '未知'}")
            lines.append(sep)
            if '-tag' not in options:
                lines.append(f"  🏷️ 标签:  {tag_str}")
            if '-role' not in options:
                lines.append(f"  🎭 人物:  {actor_str}")
            if '-work' not in options:
                lines.append(f"  📚 作品:  {work_str}")
            lines.append(sep)
            lines.append(f"  📑 共{len(chapters)}话：")
            for i, photo in enumerate(chapters[:10]):
                lines.append(f"     {self.catalog.format_chapter_line(photo, i + 1)}")
            if len(chapters) > 10:
                lines.append(
                    f"     ... 共{len(chapters)}话，仅显示前10话"
                    f"（查看全部：/jmlist {album_id}）"
                )
            if not chapters:
                lines.append("     暂无话")
            else:
                lines.append("     💡 前方编号为网站话序号，方括号后为漫画自身的章节标题（两者可能不同）")
            lines.append(sep)
            lines.append(
                f"💡 /jmlist {album_id} 话列表 | /jm {album_id} PDF | /jmzip {album_id} ZIP"
            )

            reply = "\n".join(lines)
            yield self.messaging.plain(event, reply)

            # 下载封面并发送；-pic 不发送封面
            # 封面转成 PDF 后走文件通道，避免 QQ 官方对图片/JPEG 做图像审核
            if '-pic' not in options:
                cover_path = self.data_dir / f"cover_{album_id}.jpg"
                cover_pdf = self.data_dir / f"cover_{album_id}.pdf"
                try:
                    await asyncio.to_thread(client.download_album_cover, album_id, str(cover_path))
                    if cover_path.exists():
                        def image_to_pdf():
                            from PIL import Image
                            with Image.open(cover_path) as img:
                                img.convert("RGB").save(cover_pdf, "PDF", resolution=150.0)

                        await asyncio.to_thread(image_to_pdf)
                        cover_path.unlink(missing_ok=True)
                        yield self.messaging.chain(event, [
                            Comp.File(
                                file=str(cover_pdf),
                                name=f"JM{album_id}_封面.pdf",
                            )
                        ])
                        # 发完就删，不存着
                        cover_pdf.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"封面下载/转换失败: {e}")
                    cover_path.unlink(missing_ok=True)
                    cover_pdf.unlink(missing_ok=True)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "查询"))

    # ========== /jmlist 话列表 ==========
    @filter.command("jmlist")
    async def jm_chapter_list(self, event: AstrMessageEvent):
        """查看漫画话列表，用法：/jmlist <漫画id> [页码]
        只发送话文本，不发送封面；封面请用 /jmv <漫画id> 查看
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)

        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：/jmlist <漫画id> [页码]\n"
                "示例：/jmlist 123456\n"
                "      /jmlist 123456 2     查看第2页话"
            )
            return

        prefix, album_id = self.selection.normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit():
            yield self.messaging.plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "章节ID请用 /jm <章节id> 下载"
            )
            return
        if len(args) > 3:
            yield self.messaging.plain(event, "用法：/jmlist <漫画id> [页码]\n页码只能填一个")
            return
        try:
            page_num = parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmlist {album_id} [页码]")
            return

        yield self.messaging.plain(event, f"📚 获取 JM{album_id} 的话列表...")

        try:
            client = self.client.get_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)

            if self.catalog.looks_like_chapter_album(album):
                parent_id = await self.catalog.resolve_chapter_album_parent(client, album, album_id)
                if parent_id:
                    parent_line = (
                        f"该话所属漫画：JM{parent_id}\n"
                        f"请用 /jm {album_id} 下载该话，或用 /jmv {parent_id} 查看漫画。"
                    )
                else:
                    parent_line = (
                        f"请用 /jm {album_id} 下载该话；"
                        "章节所属漫画ID未能识别，可先执行 /jm 后查看缓存目录。"
                    )
                yield self.messaging.plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                )
                return

            chapters = list(album) if hasattr(album, '__iter__') else []

            if not chapters:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 没有话列表。\n"
                    "请检查 ID 是否正确；如果是章节ID，请用 /jm <章节id> 下载。"
                )
                return

            page_size = self.per_page
            total_pages = max(1, math.ceil(len(chapters) / page_size))
            if page_num > total_pages:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 共 {len(chapters)} 话，只有 {total_pages} 页，"
                    f"已到最后一页（/jmlist {album_id} {total_pages}）"
                )
                return

            start = (page_num - 1) * page_size
            page_items = chapters[start:start + page_size]
            album_title = self.text.safe_text(album.name, self.convert_title)

            lines = [
                f"📚 {album_title}",
                f"🆔 JM{album_id} | 共{len(chapters)}话 | 第{page_num}/{total_pages}页",
                "━━━━━━━━━━━━━━━",
            ]
            for i, photo in enumerate(page_items):
                lines.append(self.catalog.format_chapter_line(photo, start + i + 1))

            lines.append(
                "💡 前方编号为网站话序号，方括号后为漫画自身的章节标题（两者可能不同）"
            )
            if page_num < total_pages:
                lines.append(f"\n下一页：/jmlist {album_id} {page_num + 1}")
            lines.append(f"💡 下载单话：/jm <章节id>；下载全本：/jm {album_id}")
            text = self.messaging.output_text(event, "\n".join(lines))

            # 直接发送，便于捕获 QQ 的“消息内容违规”异常；再用日志兜底检测静默拦截
            mark = self.messaging.log_pos()
            try:
                await event.send(MessageChain(chain=[Comp.Plain(text)]))
            except Exception as send_err:
                violation = self.messaging.is_violation_error(send_err) or self.messaging.log_has_violation(mark)
                if violation:
                    yield self.messaging.plain(event,
                        "⚠️ 话列表被 QQ 判定为消息内容违规，已拦截。\n"
                        "标题已做敏感词转拼音；可以换一页重试，或用 /jmv <漫画id> -pic 看详情。"
                    )
                else:
                    logger.warning(f"jmlist 发送失败: {send_err}")
                    yield self.messaging.plain(event, f"❌ 话列表发送失败：{send_err}")
                return

            # 给平台发送/日志落盘留一点时间，再检查是否静默拦截
            await asyncio.sleep(1.5)
            if self.messaging.log_has_violation(mark):
                yield self.messaging.plain(event,
                    "⚠️ 日志检测到话列表触发了“消息内容违规”，可能已被 QQ 拦截。\n"
                    "可以换一页重试，标题已做敏感词转拼音。"
                )
                return

            if page_num < total_pages:
                self.pagination.remember(
                    event,
                    f"/jmlist {album_id} {page_num + 1}",
                    page_num,
                    total_pages,
                )

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取话列表"))

    # ========== /jm 下载 PDF（/jmphoto 为兼容别名） ==========
    @filter.command("jm", alias={"jmphoto"})
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 PDF，用法：/jm <id> [-c 章节]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()

        # 不带参数或 help，输出帮助
        if len(args) < 2 or args[1].lower() == 'help':
            help_text = (
                "📖 漫画站插件 帮助 v0.7.8\n"
                "━━━━━━━━━━━━━━━\n"
                "🔍 搜索：\n"
                "  /jms 关键词 [页码] [排序]   全站搜索\n"
                "  /jma 作者名 [页码] [排序]   按作者搜索\n"
                "  /jmt 标签名 [页码] [排序]   按标签搜索\n"
                "  /jmr 角色名 [页码] [排序]   按角色搜索\n"
                "  /jmw 原作名 [页码] [排序]   按原作搜索\n"
                "━━━━━━━━━━━━━━━\n"
                "📖 详情：\n"
                "  /jmv <漫画id> [选项]       查详情(含话列表/标签/人物/作品)\n"
                "    选项：-author -work -role -tag -pic(不发封面)\n"
                "  /jmlist <漫画id> [页码]    查看全部话（纯文本，不发封面）\n"
                "  /jmc <漫画id> [页码]        看评论(翻页/回复/剧透，超页自动提示)\n"
                "━━━━━━━━━━━━━━━\n"
                "⬇️ 下载：\n"
                "  /jm <漫画id>                下载整本，PDF按后台页数上限自动分册\n"
                "  /jm <章节id>                自动识别为单话，发一个PDF\n"
                "  /jm <章节id> -c 1:201-      章节ID页段续传（话序号固定写1）\n"
                "  /jm id1,id2,id3             批量下载，支持漫画/章节ID混合\n"
                "  /jm <id> -c 31              只下第31话\n"
                "  /jm <id> -c 31-40           下第31到40话（范围倒置会自动纠正并提示）\n"
                "  /jm <id> -c 31,35,40        下指定话\n"
                "  /jm <id> -c 5:201-          从第5话第201页继续（页段续传）\n"
                "  /jm <id> -c 5:201-400       只下第5话第201-400页\n"
                "  /jmzip <漫画id>             下载整本，按后台话数自动合并ZIP\n"
                "  /jmzip <章节id>             自动识别为单话，发一个ZIP\n"
                "  /jmzip id1,id2,id3          批量下载，支持漫画/章节ID混合\n"
                "  /jmzip <id> -c 31-40        下第31到40话\n"
                "  /jmzip <id> -c 31,35,40     下指定话\n"
                "  /jmphoto <id>               等同 /jm <id>（兼容旧命令）\n"
                "  /jmnow                      查看自己当前下载进度\n"
                "  /jmstop                     停止当前用户正在进行的下载\n"
                "━━━━━━━━━━━━━━━\n"
                "⭐ 账号（仅号主）：\n"
                "  /jmcheckin                  每日签到领金币经验\n"
                "  /jmme                       用户信息面板(签到/收藏/进度)\n"
                "  /jmfav                      收藏夹文件夹列表\n"
                "  /jmfav <文件夹id>           查看指定收藏夹\n"
                "  /jmfav add <漫画id>         添加收藏\n"
                "  /jmfav del <漫画id>         移除收藏\n"
                "  /jmfav export [文件夹id]    导出收藏夹CSV（30秒内再输一次确认）\n"
                "━━━━━━━━━━━━━━━\n"
                "🔔 订阅：\n"
                "  /jmsub <漫画id>             订阅漫画更新\n"
                "  /jmsub list                 查看当前会话订阅\n"
                "  /jmsub del <漫画id>         取消订阅\n"
                "━━━━━━━━━━━━━━━\n"
                "📊 其他：\n"
                "  /jmwhoami                   查看自己的ID（配置号主用，所有人可用）\n"
                "  /jmrank day/week/month [页码]  排行榜\n"
                "  /jmhis [页码]               下载历史（每页{per_page}条）\n"
                "    用法：/jmhis clear 清空历史 | /jmhis -all [页码] 查看全部成员(管理员，每用户每页10条)\n"
                "  /jmdomain                   查看可用域名\n"
                "  /jmupdate                   查看更新日志\n"
                "  /jmrand [关键词]            随机抽一个\n"
                "  /jmnext                     翻到最近一次列表的下一页\n"
                "    也可在 {page_turn_timeout} 秒内直接发送“下一页/下页”\n"
                "━━━━━━━━━━━━━━━\n"
                "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)\n"
                "💡 搜索小提示：建议只打前几个关键词，打全容易因简繁差异漏结果\n"
                "💾 下载缓存：JM{{漫画id}}/P{{章节id}}_第N话/图片，标题存入 metadata.json，默认保留 {cache_days} 天\n"
                "📎 打包信息：ZIP 附漫画信息.txt；PDF 写入标题属性，每册都加漫画信息页\n"
                "📄 PDF：每 {pdf_page_limit} 页一册（整话优先，单话超限按页切分）\n"
                "🗜️ ZIP：最多 {zip_group_size} 话/册，{zip_page_text}\n"
                "⚙️ 当前客户端：{client_type}（插件配置可切换 api/html）\n"
                "📦 需 jmcomic 2.7.9+（requirements 会自动安装/升级）\n"
                "⏳ 文件限额：每日{daily_limit_text}、每小时{hourly_limit_text}（0=不限）".format(
                    client_type=self.client_type.upper(),
                    per_page=self.per_page,
                    page_turn_timeout=self.page_turn_timeout,
                    cache_days=self.cache_days,
                    pdf_page_limit=self.pdf_page_limit,
                    zip_group_size=self.zip_group_size,
                    zip_page_text=self.settings.zip_page_hint(),
                    daily_limit_text=self.quota.limit_text(self.daily_file_limit),
                    hourly_limit_text=self.quota.limit_text(self.hourly_file_limit),
                )
            )
            yield self.messaging.plain(event, help_text)
            return

        try:
            target, chapter_expr = self.selection.parse_download_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jm <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        if not target:
            yield self.messaging.plain(event, "用法：/jm <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        async for message in self._consume_download(
            event, target, "pdf", chapter_expr
        ):
            yield message

    async def _consume_download(self, event, target, output_format, chapter_expr):
        """包装下载生成器，登记当前任务供 /jmstop 取消。"""
        task = asyncio.current_task()
        user_id = str(event.get_sender_id())
        if task is not None:
            self.state.download_tasks.setdefault(user_id, set()).add(task)
            album_hint = str(target).strip() if str(target).strip().isdigit() else ""
            self.progress.set_active(user_id, task, album_hint)
        try:
            async for message in self._run_download(
                event, target, output_format, chapter_expr
            ):
                yield message
        except asyncio.CancelledError:
            yield self.messaging.plain(event, "⏹️ 当前下载已停止")
        finally:
            tasks = self.state.download_tasks.get(user_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    self.state.download_tasks.pop(user_id, None)
            self.progress.clear(user_id, task)

    # ========== /jmstop 停止下载 ==========
    @filter.command("jmstop")
    async def jm_stop(self, event: AstrMessageEvent):
        """停止当前用户正在进行的下载"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        user_id = str(event.get_sender_id())
        tasks = [
            task for task in self.state.download_tasks.get(user_id, set())
            if not task.done()
        ]
        if not tasks:
            yield self.messaging.plain(event, "当前没有正在进行的下载")
            return
        for task in tasks:
            task.cancel()
        yield self.messaging.plain(event, f"⏹️ 已请求停止 {len(tasks)} 个下载任务")

    # ========== /jmnow 当前下载进度 ==========
    @filter.command("jmnow")
    async def jm_now(self, event: AstrMessageEvent):
        """查看当前用户正在进行的下载进度"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        state = self.progress.get_active(event.get_sender_id())
        if not state:
            yield self.messaging.plain(event, "当前还没有下载")
            return

        album_id = str(state.get("album_id") or "")
        done = max(0, int(state.get("done") or 0))
        total = max(0, int(state.get("total") or 0))
        prog = ProgressDownloader._progress.get(album_id) if album_id else None
        if prog:
            done = max(done, int(prog.get("done") or 0))
            total = max(total, int(prog.get("total") or 0))

        if not album_id:
            yield self.messaging.plain(event, "当前下载：正在识别漫画，请稍候")
            return
        if total > 0:
            done = min(done, total)
            yield self.messaging.plain(
                event,
                f"当前下载 JM{album_id}：已完成 {done}/{total} 页，"
                f"未完成 {total - done} 页",
            )
        else:
            yield self.messaging.plain(
                event,
                f"当前下载 JM{album_id}：已完成 {done} 页，总页数统计中",
            )

    # ========== /jmnext 翻页 ==========
    async def _follow_page_session(self, event, session):
        next_command = session.get("next_command", "")
        parts = next_command.strip().split()
        if not parts:
            return
        command = parts[0].lstrip("/").lower()
        handler_name = self.pagination.page_handler_name(command)
        handler = getattr(self, handler_name, None) if handler_name else None
        if handler is None:
            return
        proxy = PageEventProxy(event, next_command)
        async for message in handler(proxy):
            yield message

    @filter.command("jmnext")
    async def jm_next(self, event: AstrMessageEvent):
        """翻到最近一次列表的下一页"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        session = self.pagination.get(event)
        if session is None:
            yield self.messaging.plain(
                event,
                "当前没有可翻页的列表，或已超时。请先使用 /jmlist、/jmhis、"
                "/jmc、/jmrank 或搜索命令。",
            )
            return
        self.pagination.clear(event)
        async for message in self._follow_page_session(event, session):
            yield message

    @filter.regex(r"^(?:下一页|下页)$")
    async def jm_next_keyword(self, event: AstrMessageEvent):
        """在有效期内发送“下一页/下页”自动翻页；无会话时静默。"""
        if not self.access.check_permission(event):
            return
        session = self.pagination.get(event)
        if session is None:
            return
        self.pagination.clear(event)
        async for message in self._follow_page_session(event, session):
            yield message

    # ========== /jmrand 随机漫画 ==========
    @filter.command("jmrand")
    async def jm_random(self, event: AstrMessageEvent):
        """随机抽一个漫画，用法：/jmrand [标签名]
        不填标签就从日榜随机，填了就从指定标签里随机
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        tag_name = " ".join(args[1:]) if len(args) > 1 else None

        if tag_name:
            yield self.messaging.plain(event, f"🎲 正在从「{tag_name}」标签随机抽一个...")
        else:
            yield self.messaging.plain(event, "🎲 正在从日榜随机抽一个漫画...")

        try:
            import random
            client = self.client.get_client()

            if tag_name:
                # 全站搜索，关键词可以是标题/标签/作者
                page = await asyncio.to_thread(client.search_site, tag_name, 1)
                items = list(page.iter_id_title_tag())
                if not items:
                    yield self.messaging.plain(event,
                        f"🎲 没有找到与「{tag_name}」相关的漫画，请换个关键词。\n"
                        "特殊符号（如 #）可能无法被搜索接口识别。"
                    )
                    return
            else:
                # 日榜可能暂时为空，依次尝试日/周/月榜的前3页增加随机范围
                items = []
                for method_name in ("day_ranking", "week_ranking", "month_ranking"):
                    method = getattr(client, method_name, None)
                    if method is None:
                        continue
                    for page_num in range(1, 4):
                        try:
                            page = await asyncio.to_thread(method, page_num)
                            page_items = list(page.iter_id_title_tag())
                        except Exception:
                            page_items = []
                        if page_items:
                            items.extend(page_items)
                    if items:
                        break
                if not items:
                    yield self.messaging.plain(event,
                        "🎲 暂时没有抽到漫画：日榜/周榜/月榜均未返回结果。\n"
                        "请稍后再试，或使用 /jmrand 关键词 从搜索中随机抽取。"
                    )
                    return

            album_id, title, tags = random.choice(items)
            title = self.text.safe_text(title, self.convert_title)
            tags = [self.text.safe_text(t, self.convert_tags) for t in tags]
            safe_tags = tags[:5]

            # 查一下详情，拿章节数和页数
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            chapters = list(album) if hasattr(album, '__iter__') else []
            page_count = getattr(album, 'page_count', '?')

            tag_str = f"\n🏷️ {', '.join(safe_tags)}" if safe_tags else ""
            yield self.messaging.plain(event,
                f"🎲 抽到了！\n\n"
                f"📖 {title}\n"
                f"🆔 JM{album_id}"
                f"{tag_str}\n"
                f"📄 {len(chapters)}话 / {page_count}页\n\n"
                f"发 /jm {album_id} 下载"
            )

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "随机抽漫"))

    # ========== /jms 全站搜索 ==========
    @filter.command("jms")
    async def jm_search(self, event: AstrMessageEvent):
        """全站搜索，用法：/jms <关键词> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：\n"
                "  /jms <关键词>\n"
                "  /jms <关键词> 2\n"
                "  /jms <关键词> mv   (mv=最多浏览, tf=最多收藏)"
            )
            return

        # 纯数字搜索可选直接按车号看详情（默认关闭，避免改变普通搜索语义）
        if self.numeric_search_as_album and len(args) == 2 and args[1].isdigit():
            async for result in self.jm_view(event):
                yield result
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jms <关键词> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self._render_search_page(
            event, client, client.search_site, keyword, qq_page, order_by, "全站搜索", "jms"
        ):
            yield result

    # ========== /jma 按作者搜索 ==========
    @filter.command("jma")
    async def jm_search_author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jma <作者名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jma <作者名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self._render_search_page(
            event, client, client.search_author, keyword, qq_page, order_by, "作者搜索", "jma"
        ):
            yield result

    # ========== /jmt 按标签搜索 ==========
    @filter.command("jmt")
    async def jm_search_tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmt <标签名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmt <标签名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self._render_search_page(
            event, client, client.search_tag, keyword, qq_page, order_by, "标签搜索", "jmt"
        ):
            yield result

    # ========== /jmr 按角色搜索 ==========
    @filter.command("jmr")
    async def jm_search_actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmr <角色名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmr <角色名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self._render_search_page(
            event, client, client.search_actor, keyword, qq_page, order_by, "角色搜索", "jmr"
        ):
            yield result

    # ========== /jmw 按原作搜索 ==========
    @filter.command("jmw")
    async def jm_search_work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmw <原作名>")
            return

        try:
            keyword, qq_page, order_by = self._parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmw <原作名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self._render_search_page(
            event, client, client.search_work, keyword, qq_page, order_by, "原作搜索", "jmw"
        ):
            yield result

    # ========== /jmzip 下载 ZIP ==========
    @filter.command("jmzip")
    async def jm_download_zip(self, event: AstrMessageEvent):
        """下载漫画/章节并发送 ZIP，用法：/jmzip <id> [-c 章节]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]\n"
                "示例：/jmzip 123456\n"
                "      /jmzip 123456 -c 31-40\n"
                "      /jmzip 123456 -c 5:201-\n"
                "      /jmzip 123,456\n"
                f"说明：多话按后台配置合并 ZIP（当前最多 {self.zip_group_size} 话、"
                f"{self.settings.zip_page_hint()}）；单话目标发一个 ZIP"
            )
            return

        try:
            target, chapter_expr = self.selection.parse_download_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        if not target:
            yield self.messaging.plain(event, "用法：/jmzip <漫画id|章节id> [-c 章节|章节:起页-止页]")
            return
        async for message in self._consume_download(
            event, target, "zip", chapter_expr
        ):
            yield message

    # ========== /jmcheckin 每日签到 ==========
    @staticmethod
    def _parse_record_day(value):
        """把签到记录里的日期（"01" / 1 / "2026-10-01"）统一成几号"""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if 1 <= value <= 31 else None
        text = str(value).strip()
        if not text:
            return None
        if '-' in text or '/' in text:
            text = re.split(r'[-/]', text)[-1]
        digits = re.sub(r'\D', '', text)
        if not digits:
            return None
        day = int(digits)
        return day if 1 <= day <= 31 else None

    def _load_checkin_state(self):
        """读取本机签到状态，接口日历延迟时用于兜底"""
        try:
            if self.checkin_file.exists():
                data = json.loads(self.checkin_file.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_checkin_state(self, streak, signed_count=None):
        """保存本机签到状态"""
        try:
            data = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "streak": int(streak or 0),
            }
            if signed_count is not None:
                data["signed_count"] = signed_count
            self.checkin_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存签到状态失败: {e}")

    def _local_streak(self, today_signed):
        """接口未返回连续天数时，用本机记录估算"""
        state = self._load_checkin_state()
        streak = int(state.get("streak", 0) or 0)
        if today_signed:
            return max(1, streak)
        return max(0, streak)

    @staticmethod
    def _daily_payload(daily):
        """取出签到接口的字典数据，兼容 API（model_data）与 HTML（json()）"""
        try:
            if hasattr(daily, 'model_data'):
                data = daily.model_data
            elif hasattr(daily, 'json'):
                data = daily.json()
            else:
                data = None
        except Exception:
            data = None
        # jmcomic 的 AdvancedDict 不是 dict 子类，必须取 src_dict 后才能当字典用
        if hasattr(data, 'src_dict'):
            data = data.src_dict
        return data if isinstance(data, dict) else {}

    def _parse_daily_info(self, daily, assume_today_signed=False):
        """解析签到信息。
        HTML 客户端有 oldStep / error 字段，API 客户端没有，
        连续天数只能从 record 签到日历里按日期往前逐日统计。
        """
        data = self._daily_payload(daily)
        info = {
            'streak': None,
            'today_signed': False,
            'signed_count': None,
            'signed_days': set(),
            'progress': None,
        }
        if not data:
            return info

        progress = data.get('currentProgress') or data.get('current_progress')
        if progress is not None:
            info['progress'] = str(progress)

        # HTML 客户端直接给连续天数
        for key in ('oldStep', 'old_step', 'continuous', 'continuous_days', 'streak'):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                info['streak'] = int(value)
                break
            if isinstance(value, str) and value.strip().isdigit():
                info['streak'] = int(value.strip())
                break

        # 今日是否已签
        if data.get('error') == 'finished' or data.get('today_signed') is True:
            info['today_signed'] = True

        # 签到日历：API 是 record 二维数组，HTML 是 dateArray
        record = data.get('record')
        if hasattr(record, 'src_dict'):
            record = record.src_dict
        if isinstance(record, dict):
            record = list(record.values())
        if isinstance(record, list):
            for week in record:
                if hasattr(week, 'src_dict'):
                    week = week.src_dict
                days = week if isinstance(week, list) else [week]
                for day in days:
                    if hasattr(day, 'src_dict'):
                        day = day.src_dict
                    if not isinstance(day, dict) or not day.get('signed'):
                        continue
                    day_num = self._parse_record_day(day.get('date'))
                    if day_num is not None:
                        info['signed_days'].add(day_num)
        elif isinstance(data.get('dateArray'), list):
            for value in data['dateArray']:
                day_num = self._parse_record_day(value)
                if day_num is not None:
                    info['signed_days'].add(day_num)

        if info['signed_days']:
            info['signed_count'] = len(info['signed_days'])
            if datetime.now().day in info['signed_days']:
                info['today_signed'] = True

        if assume_today_signed:
            info['today_signed'] = True
            info['signed_days'].add(datetime.now().day)

        # 连续天数兜底：从今天（今天没签则从昨天）往前逐日数
        if info['streak'] is None and info['signed_days']:
            today = datetime.now().day
            cursor = today if today in info['signed_days'] else today - 1
            streak = 0
            while cursor >= 1 and cursor in info['signed_days']:
                streak += 1
                cursor -= 1
            info['streak'] = streak

        return info

    @filter.command("jmcheckin")
    async def jm_checkin(self, event: AstrMessageEvent):
        """每日签到领金币经验，用法：/jmcheckin"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self.access.is_owner(event):
            yield self.messaging.plain(event, self.access.owner_required_text())
            return
        try:
            client = self.client.get_client()

            # jmcomic 2.7.5 及以下版本不支持签到接口
            jm_version = getattr(jmcomic, '__version__', '未知')
            if not hasattr(client, 'get_daily') or not hasattr(client, 'daily_checkin'):
                yield self.messaging.plain(event,
                    f"⚠️ 当前 jmcomic 版本（{jm_version}）不支持每日签到功能\n"
                    f"需要 jmcomic >= 2.7.7（推荐升级到 2.7.9），请升级后重启 AstrBot：\n"
                    f"  D:\\Program Files\\AstrBot\\backend\\python\\python.exe -m pip install -U jmcomic"
                )
                return

            # 先获取签到信息
            yield self.messaging.plain(event, "📅 正在获取签到信息...")
            daily = await asyncio.to_thread(client.get_daily)
            info = self._parse_daily_info(daily)

            # 执行签到
            yield self.messaging.plain(event, "✅ 正在签到...")
            signed_ok = False
            try:
                resp = await asyncio.to_thread(client.daily_checkin)
                if resp.code == 0:
                    result_msg = f"🎉 签到成功！\n{resp.msg}"
                    signed_ok = True
                elif resp.code == 1:
                    result_msg = f"📌 今日已签到\n{resp.msg}"
                    signed_ok = True
                else:
                    result_msg = f"签到结果：{resp.msg}"
            except Exception as checkin_err:
                # jmcomic 2.7.9 的关键词匹配只支持繁体，API返回简体"今天已经签到过了"时会抛异常
                err_str = str(checkin_err)
                if any(kw in err_str for kw in ('已签', '签到过', '已经签', '今日已')):
                    result_msg = f"📌 今日已签到\n{err_str.replace('签到失败：', '')}"
                    signed_ok = True
                else:
                    raise

            # 签到后重新拉取状态：API 客户端的连续天数要从 record 日历反推
            if signed_ok:
                try:
                    daily_after = await asyncio.to_thread(client.get_daily)
                    final_info = self._parse_daily_info(daily_after, assume_today_signed=True)
                except Exception:
                    final_info = self._parse_daily_info(daily, assume_today_signed=True)
            else:
                final_info = info

            if final_info.get('signed_count') is None:
                final_info['signed_count'] = info.get('signed_count')
            if final_info.get('streak') is None:
                final_info['streak'] = info.get('streak')
            if final_info.get('streak') is None:
                final_info['streak'] = self._local_streak(bool(final_info.get('today_signed')))

            if signed_ok:
                streak_to_save = final_info.get('streak')
                if streak_to_save is None:
                    streak_to_save = 1
                self._save_checkin_state(streak_to_save, final_info.get('signed_count'))

            streak_text = f"{final_info['streak']} 天" if final_info.get('streak') is not None else "获取失败"
            reply = f"📅 每日签到\n\n{result_msg}\n\n"
            reply += f"🔥 连续签到：{streak_text}\n"
            if final_info.get('signed_count') is not None:
                reply += f"📆 本月已签：{final_info['signed_count']} 天"
            if final_info.get('today_signed'):
                reply += "\n\n✅ 今日已完成签到"

            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "签到"))

    # ========== /jmme 用户面板 ==========
    @filter.command("jmme")
    async def jm_user_info(self, event: AstrMessageEvent):
        """查看用户信息面板，用法：/jmme"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self.access.is_owner(event):
            yield self.messaging.plain(event, self.access.owner_required_text())
            return
        try:
            client = self.client.get_client()

            # 从配置读取登录用户名
            username = ""
            try:
                for plugin in self.option.plugins.after_init:
                    if plugin.get('plugin') == 'login':
                        username = plugin.get('kwargs', {}).get('username', '')
                        break
            except:
                pass

            if not username:
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，请在插件配置中填写账号密码")
                return

            # 获取签到信息（jmcomic 2.7.5+ 才支持）
            info = {}
            if hasattr(client, 'get_daily'):
                try:
                    daily = await asyncio.to_thread(client.get_daily)
                    info = self._parse_daily_info(daily)
                except Exception:
                    info = {}

                # 接口日历更新延迟时，用本机签到记录兜底
                state = self._load_checkin_state()
                today_str = datetime.now().strftime('%Y-%m-%d')
                if state.get('date') == today_str:
                    info['today_signed'] = True
                if info.get('streak') is None:
                    info['streak'] = self._local_streak(bool(info.get('today_signed')))
                if info.get('signed_count') is None and state.get('signed_count') is not None:
                    info['signed_count'] = state.get('signed_count')

            # 获取收藏数（通过收藏夹第一页）
            fav_count = "未知"
            try:
                fav_page = await asyncio.to_thread(client.favorite_folder, 1)
                fav_total = getattr(fav_page, 'total', None)
                if fav_total is not None:
                    fav_count = str(fav_total)
            except Exception:
                pass

            reply = f"👤 用户面板\n\n"
            reply += f"📛 用户名：{username}\n"
            if hasattr(client, 'get_daily'):
                streak = info.get('streak')
                streak_text = f"{streak} 天" if streak is not None else "获取失败"
                reply += f"🔥 连续签到：{streak_text}\n"
                reply += f"📅 今日签到：{'✅ 已签' if info.get('today_signed') else '❌ 未签'}\n"
                if info.get('progress'):
                    reply += f"📈 活动进度：{info['progress']}\n"
            reply += f"⭐ 收藏数量：{fav_count}\n"
            if hasattr(client, 'daily_checkin'):
                reply += f"\n💡 /jmcheckin 立即签到"
            else:
                reply += f"\n💡 签到功能需升级 jmcomic 到最新版本"

            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取用户信息"))

    # ========== /jmrank 排行榜 ==========
    @filter.command("jmrank")
    async def jm_ranking(self, event: AstrMessageEvent):
        """排行榜，用法：/jmrank day|week|month [页码]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event,
                "用法：\n"
                "  /jmrank day [页码]    日榜\n"
                "  /jmrank week [页码]   周榜\n"
                "  /jmrank month [页码]  月榜"
            )
            return

        rank_type = args[1].lower()

        rank_map = {
            'day': ('日榜', 'day_ranking'),
            'week': ('周榜', 'week_ranking'),
            'month': ('月榜', 'month_ranking'),
        }

        if rank_type not in rank_map:
            yield self.messaging.plain(event, "类型只能是 day / week / month")
            return
        if len(args) > 3:
            yield self.messaging.plain(event, "用法：/jmrank day|week|month [页码]\n页码只能填一个")
            return
        try:
            qq_page = parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmrank {rank_type} [页码]")
            return

        rank_name, method_name = rank_map[rank_type]
        yield self.messaging.plain(event, f"🏆 获取{rank_name} 第{qq_page}页...")

        try:
            client = self.client.get_client()
            method = getattr(client, method_name)

            PER_QQ_PAGE = self.per_page
            PER_WEB_PAGE = 80
            QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

            web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
            offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

            page = await asyncio.to_thread(method, web_page)
            all_items = list(page.iter_id_title_tag())

            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            if not page_items:
                self.pagination.clear(event)
                yield self.messaging.plain(event, "没有更多了")
                return

            results = []
            for i, (aid, title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                title = self.text.safe_text(title, self.convert_title)
                tags = [self.text.safe_text(t, self.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {title}{tag_str}")

            reply = f"🏆 {rank_name}（第{qq_page}页）\n\n" + "\n".join(results)
            reply += f"\n\n下一页：/jmrank {rank_type} {qq_page + 1}"
            self.pagination.remember(
                event, f"/jmrank {rank_type} {qq_page + 1}", qq_page, None
            )
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取排行榜"))

    # ========== /jmhis 下载历史 ==========
    @filter.command("jmhis")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhis 或 /jmhis clear 清空历史"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)
        args = event.message_str.split()
        user_id = event.get_sender_id()

        if len(args) >= 2 and args[1].lower() == "clear":
            data = self.history.load()
            if user_id in data:
                del data[user_id]
            self.history.save(data)
            yield self.messaging.plain(event, "🧹 你的下载历史已清空")
            return

        # /jmhis -all：管理员查看所有成员的历史，页码与 -all 可任意顺序
        show_all = any(token.lower() in ("-all", "all") for token in args[1:])
        page_num = 1
        page_seen = False
        if show_all:
            for token in args[1:]:
                if token.lower() in ("-all", "all"):
                    continue
                if page_seen:
                    yield self.messaging.plain(event, "用法：/jmhis [页码] -all\n页码只能填一个")
                    return
                try:
                    page_num = parse_positive_page(token)
                    page_seen = True
                except ValueError as e:
                    yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmhis [页码] -all")
                    return

            is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
            if not is_admin:
                yield self.messaging.plain(event, "⚠️ 只有管理员可以使用 /jmhis -all")
                return
            data = self.history.load()
            if not data:
                yield self.messaging.plain(event, "还没有任何下载记录")
                return
            page_size = 10
            active_users = [(uid, items) for uid, items in data.items() if items]
            if not active_users:
                yield self.messaging.plain(event, "还没有任何下载记录")
                return
            total_users = len(active_users)
            total_records = sum(len(items) for _uid, items in active_users)
            total_pages = max(
                1,
                max(math.ceil(len(items) / page_size) for _uid, items in active_users),
            )
            if page_num > total_pages:
                yield self.messaging.plain(event,
                    f"⚠️ 全部成员历史共 {total_records} 条，只有 {total_pages} 页，"
                    f"已到最后一页（/jmhis -all {total_pages}）"
                )
                return

            lines = []
            for uid, items in active_users:
                start = (page_num - 1) * page_size
                page_items = items[start:start + page_size]
                lines.append(
                    f"👤 用户 {uid}（共{len(items)}条，本页{len(page_items)}条）"
                )
                if not page_items:
                    lines.append("  （本页无记录）")
                for offset, item in enumerate(page_items, 1):
                    title = self.text.safe_text(item.get('title', ''), self.convert_title)
                    lines.append(
                        f"  {start + offset}. [JM{item.get('id', '')}] {title[:30]}  "
                        f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
                    )
                lines.append("")
            reply = (
                f"📜 全部成员下载历史（{total_users}人，共{total_records}条，"
                f"第{page_num}/{total_pages}页）\n\n"
                + "\n".join(lines)
            ).rstrip()
            nav = []
            if page_num > 1:
                nav.append(f"上一页：/jmhis -all {page_num - 1}")
            if page_num < total_pages:
                nav.append(f"下一页：/jmhis -all {page_num + 1}")
            if nav:
                reply += "\n\n" + "\n".join(nav)
            if page_num < total_pages:
                self.pagination.remember(
                    event, f"/jmhis -all {page_num + 1}", page_num, total_pages
                )
            yield self.messaging.plain(event, reply)
            return

        if len(args) > 2:
            yield self.messaging.plain(event, "用法：/jmhis [页码]\n页码只能填一个")
            return
        try:
            page_num = parse_positive_page(args[1]) if len(args) >= 2 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmhis [页码]")
            return

        history = self.history.get_user(user_id)
        if not history:
            yield self.messaging.plain(event, "还没有下载记录")
            return

        page_size = self.per_page
        total_pages = max(1, math.ceil(len(history) / page_size))
        if page_num > total_pages:
            yield self.messaging.plain(event,
                f"⚠️ 下载历史共 {len(history)} 条，只有 {total_pages} 页，已到最后一页\n"
                f"🔙 回到最后一页：/jmhis {total_pages}"
            )
            return

        start = (page_num - 1) * page_size
        page_items = history[start:start + page_size]
        results = []
        for offset, item in enumerate(page_items, 1):
            title = self.text.safe_text(item.get('title', ''), self.convert_title)
            results.append(
                f"{start + offset}. [JM{item.get('id', '')}] {title[:30]}  "
                f"({item.get('size_mb', '?')}MB)  {item.get('time', '')}"
            )

        reply = (
            f"📜 下载历史（第{page_num}/{total_pages}页，共{len(history)}条）：\n\n"
            + "\n".join(results)
        )
        if page_num < total_pages:
            reply += f"\n\n下一页：/jmhis {page_num + 1}"
        if page_num > 1:
            reply += f"\n上一页：/jmhis {page_num - 1}"
        reply += "\n\n/jmhis clear 清空历史"
        if page_num < total_pages:
            self.pagination.remember(
                event, f"/jmhis {page_num + 1}", page_num, total_pages
            )
        yield self.messaging.plain(event, reply)

    # ========== /jmc 评论查看 ==========
    @filter.command("jmc")
    async def jm_comments(self, event: AstrMessageEvent):
        """查看评论，用法：/jmc <id> [页码]
        支持翻页，展示回复楼、剧透标记、点赞数
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        self.pagination.clear(event)
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmc <漫画id> [页码]\n示例：/jmc 123456 或 /jmc 123456 2")
            return

        prefix, album_id = self.selection.normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit():
            yield self.messaging.plain(event,
                f"❌ 漫画ID必须是数字：{args[1]}\n"
                "章节ID不能查评论，请用 /jm <章节id> 下载"
            )
            return
        if len(args) > 3:
            yield self.messaging.plain(event, "用法：/jmc <漫画id> [页码]\n页码只能填一个")
            return
        try:
            page_num = parse_positive_page(args[2]) if len(args) >= 3 else 1
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmc {album_id} [页码]")
            return

        # HTML 客户端不支持评论翻页，最多10条
        if page_num > 1 and self.client_type != 'api':
            yield self.messaging.plain(event, "⚠️ 评论翻页仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
            return

        yield self.messaging.plain(event, f"💬 获取 JM{album_id} 的评论（第{page_num}页）...")

        try:
            client = self.client.get_client()

            # 先校验漫画是否存在，避免无效ID被上游当成“0页评论”
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            if self.catalog.looks_like_chapter_album(album):
                parent_id = await self.catalog.resolve_chapter_album_parent(client, album, album_id)
                parent_line = (
                    f"该话所属漫画：JM{parent_id}，可用 /jmv {parent_id} 查看。\n"
                    if parent_id else ""
                )
                yield self.messaging.plain(event,
                    f"❌ JM{album_id} 看起来是章节ID，不是漫画ID。\n"
                    f"{parent_line}"
                    f"章节ID不能查评论，请用 /jm {album_id} 下载。"
                )
                return

            # 超页检测：如果已知总页数且超过，直接提示（避免API返回重复内容）
            comment_cache_key = f"comment_pages_{album_id}"
            known_max_page = self.state.comment_page_cache.get(comment_cache_key)
            if known_max_page and page_num > known_max_page:
                yield self.messaging.plain(event, f"⚠️ JM{album_id} 评论共 {known_max_page} 页，已到最后一页")
                return

            page = await asyncio.to_thread(client.album_pagination, album_id, page=page_num)

            # 先拿到总页数并缓存（API 的 page_count 由 total 计算，最多500页）
            total = getattr(page, 'total', None)
            page_count = getattr(page, 'page_count', None)
            if page_count:
                self.state.comment_page_cache[comment_cache_key] = page_count

            # 0 条评论不是“超页”，优先显示自然文案
            if page_count == 0:
                yield self.messaging.plain(event, "还没有评论")
                return

            # 请求页码超过总页数时，上游会重复返回第一页内容，这里直接拦截
            if page_count is not None and page_num > page_count:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 评论共 {page_count} 页，已到最后一页\n"
                    f"🔙 回到第1页：/jmc {album_id} 1"
                )
                return

            comments = list(page)
            if not comments:
                if page_num > 1:
                    yield self.messaging.plain(event,
                        f"⚠️ 第{page_num}页没有评论，已经到最后一页了\n"
                        f"🔙 上一页：/jmc {album_id} {page_num - 1}"
                    )
                else:
                    yield self.messaging.plain(event, "还没有评论")
                return

            id_to_name = {}

            def collect_names(items):
                for item in items:
                    name = getattr(item, 'nickname', None) or getattr(item, 'username', None) or '匿名'
                    cid = getattr(item, 'comment_id', None)
                    if cid is not None:
                        id_to_name[str(cid)] = name
                    collect_names(getattr(item, 'replies', []) or [])

            collect_names(comments)

            def format_comment(comment, indent=0, is_reply=False):
                """格式化单条评论，回复用 ↳ 单独标记并显示回复对象"""
                prefix = "  " * indent
                nickname = getattr(comment, 'nickname', None) or getattr(comment, 'username', None) or '匿名'
                nickname = self.text.safe_text(nickname, False)
                raw_content = getattr(comment, 'content', '') or ''
                content = self.text.safe_text(raw_content.replace('\n', ' ').strip()[:120], self.convert_comment)

                reply_to = ""
                parent_id = getattr(comment, 'parent_comment_id', None)
                if is_reply:
                    parent_name = id_to_name.get(str(parent_id)) if parent_id is not None else None
                    if parent_name:
                        parent_name = self.text.safe_text(parent_name, False)
                    reply_to = f" 回复 @{parent_name}" if parent_name else " 回复"

                spoiler = " ⚠️剧透" if getattr(comment, 'is_spoiler', False) else ""
                likes = getattr(comment, 'likes', None)
                likes_str = f" 👍{likes}" if likes is not None else ""
                created = getattr(comment, 'created_at', '')
                time_str = f" ({created})" if created else ""

                arrow = "↳ " if is_reply else ""
                line = f"{prefix}{arrow}{nickname}{reply_to}{spoiler}{likes_str}{time_str}：{content}"
                lines = [line]

                # 嵌套回复
                for reply in getattr(comment, 'replies', []) or []:
                    lines.extend(format_comment(reply, indent + 1, True))

                return lines

            results = []
            for i, comment in enumerate(comments[:self.comment_count]):
                block = format_comment(comment)
                results.append(f"{i+1}. {block[0]}")
                results.extend(block[1:])

            # 分页信息（total/page_count 已在上方获取并缓存）
            header = f"💬 JM{album_id} 评论（第{page_num}页"
            if total is not None:
                header += f"，共{total}条"
                # 上游API评论接口最多返回5000条，超过会被截断
                if total >= 5000:
                    header += "（上游API上限5000条）"
            if page_count is not None:
                header += f"，共{page_count}页"
            header += "）"

            reply = header + "\n\n" + "\n".join(results)

            # 下一页提示
            if page_count is None or page_num < page_count:
                reply += f"\n\n下一页：/jmc {album_id} {page_num + 1}"
                self.pagination.remember(
                    event,
                    f"/jmc {album_id} {page_num + 1}",
                    page_num,
                    page_count,
                )

            yield self.messaging.plain(event, reply[:3500])

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取评论"))

    # ========== /jmwhoami 查看自己的ID ==========
    @filter.command("jmwhoami")
    async def jm_whoami(self, event: AstrMessageEvent):
        """查看自己的发送者ID，用于配置 owner_ids"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        sender_id = str(event.get_sender_id())
        is_admin = event.is_admin() if hasattr(event, 'is_admin') else getattr(event, 'role', '') == 'admin'
        is_owner = self.access.is_owner(event)
        lines = [
            f"🆔 你的发送者ID：{sender_id}",
            f"👑 AstrBot管理员：{'是' if is_admin else '否'}",
            f"🔑 JM账号号主：{'是' if is_owner else '否'}",
        ]
        if not self.owner_ids:
            lines.append("\n⚠️ 尚未配置 owner_ids，账号类命令暂时不可用。")
            lines.append(f"把上面的ID填入插件配置 owner_ids 即可。")
        yield self.messaging.plain(event, "\n".join(lines))

    # ========== /jmdomain 查看可用域名 ==========
    @filter.command("jmdomain")
    async def jm_domain(self, event: AstrMessageEvent):
        """查看 API 与 HTML 两套可用域名"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        yield self.messaging.plain(event, "🌐 正在获取可用域名...")
        try:
            client = self.client.get_client()
            client_label = "API" if self.client_type == "api" else "HTML"

            api_domains = []
            html_domains = []

            # 当前客户端已知域名
            try:
                current_domains = list(client.get_domain_list())
                if self.client_type == "api":
                    api_domains = current_domains
                else:
                    html_domains = current_domains
            except Exception:
                pass

            # API 域名：优先使用库自动更新后的列表
            if not api_domains:
                api_domains = list(
                    JmModuleConfig.DOMAIN_API_UPDATED_LIST
                    or JmModuleConfig.DOMAIN_API_LIST
                    or []
                )

            # HTML 域名：通过 jmcomic 发布页获取，失败则回退到库缓存
            if not html_domains:
                try:
                    html_domains = list(await asyncio.to_thread(JmModuleConfig.get_html_domain_all))
                except Exception:
                    html_domains = []
                if not html_domains:
                    fallback = JmModuleConfig.DOMAIN_HTML_LIST or JmModuleConfig.DOMAIN_HTML
                    if isinstance(fallback, str):
                        html_domains = [fallback]
                    elif fallback:
                        html_domains = list(fallback)

            domain_str = self.config.get("domain", "")
            custom_domains = [d.strip() for d in domain_str.split(",") if d.strip()]

            reply = f"🌐 JM 域名信息（当前客户端：{client_label}）\n\n"
            reply += "📡 API 域名（接口请求用，浏览器打开无画面）：\n"
            if api_domains:
                for i, d in enumerate(api_domains[:10]):
                    reply += f"  {i+1}. {d}\n"
            else:
                reply += "  暂未获取到\n"

            reply += "\n🌍 HTML 域名（浏览器可直接访问）：\n"
            if html_domains:
                for i, d in enumerate(html_domains[:10]):
                    reply += f"  {i+1}. {d}\n"
            else:
                reply += "  暂未获取到\n"

            if custom_domains:
                reply += f"\n⚙️ 你配置的自定义域名：{', '.join(custom_domains)}\n"
            reply += (
                "\n💡 API 客户端会自动选择最快域名并更新；"
                "HTML 客户端可在配置中填写可用域名\n"
            )

            yield self.messaging.plain(event, reply)
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取域名"))

    # ========== /jmupdate 查看更新日志 ==========
    @filter.command("jmupdate")
    async def jm_changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        changelog_path = Path(__file__).parent / "CHANGELOG.md"
        if not changelog_path.exists():
            yield self.messaging.plain(event, "❌ 更新日志文件不存在")
            return
        content = changelog_path.read_text(encoding="utf-8")
        # 只发最新版本：从第一个二级标题开始，到下一个二级标题前结束
        match = re.search(r"^##\s+.*?(?=^##\s+|\Z)", content, re.S | re.M)
        reply = match.group(0).strip() if match else content.strip()
        reply = re.sub(r"\n+---\s*$", "", reply).strip()
        if len(reply) > 1800:
            reply = reply[:1750] + "\n...（已截断）"
        yield self.messaging.plain(event, f"📝 最新更新日志：\n\n{reply}")

    # ========== /jmfav 收藏夹 ==========
    def _clear_client_cache(self, client):
        """收藏状态是 toggle 接口，操作前后必须清缓存，避免 is_favorite 读到旧值"""
        try:
            cache = client.get_cache_dict()
            if cache:
                cache.clear()
        except Exception:
            pass

    async def _favorite_state(self, client, album_id):
        """返回 (是否已收藏, 漫画详情)"""
        self._clear_client_cache(client)
        album = await asyncio.to_thread(client.get_album_detail, album_id)
        return bool(getattr(album, 'is_favorite', False)), album

    async def _render_favorites(self, client, folder_id='0', title='⭐ 当前收藏', limit=10):
        """渲染收藏夹内容，操作后自动调用"""
        page = await asyncio.to_thread(client.favorite_folder, 1, folder_id=folder_id)
        total = getattr(page, 'total', None)
        items = list(page.iter_id_title_tag())
        if not items:
            return f"{title}：空"

        results = []
        for i, (aid, item_title, tags) in enumerate(items[:limit]):
            item_title = self.text.safe_text(item_title, self.convert_title)
            tags = [self.text.safe_text(t, self.convert_tags) for t in tags]
            safe_tags = tags[:3]
            tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
            results.append(f"{i+1}. [JM{aid}] {item_title}{tag_str}")

        total_text = total if total is not None else len(items)
        reply = f"{title}（共{total_text}本，显示前{len(results)}本）：\n\n" + "\n".join(results)
        reply += "\n\n/jmv <id> 查详情，/jm <id> 下载"
        return reply

    @staticmethod
    def _collect_favorite_rows(client, folder_id):
        """同步收集收藏夹全部条目，供 asyncio.to_thread 调用。"""
        rows = []
        seen = set()
        for page in client.favorite_folder_gen(folder_id=folder_id):
            for aid, extra in page.content:
                aid = str(aid)
                if aid in seen:
                    continue
                seen.add(aid)
                rows.append((
                    aid,
                    str(extra.get('author', '') or ''),
                    str(extra.get('name', '') or ''),
                ))
        return rows

    @staticmethod
    def _write_favorites_csv(path, rows):
        """写 CSV；utf-8-sig 方便 Windows Excel 直接打开。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'author', 'name'])
            writer.writerows(rows)

    async def _export_favorites(self, event: AstrMessageEvent, folder_id: str):
        """导出收藏夹为 CSV 文件（仅号主，无加密）。"""
        try:
            client = self.client.get_client()
            if not self.client.configured_username():
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            folder_name = "全部"
            if folder_id != "0":
                folder_name = f"收藏夹{folder_id}"
                try:
                    page = await asyncio.to_thread(client.favorite_folder, 1)
                    for fid, fname in page.iter_folder_id_name():
                        if str(fid) == str(folder_id):
                            folder_name = fname or folder_name
                            break
                except Exception as e:
                    logger.warning(f"读取收藏夹名称失败: {e}")

            yield self.messaging.plain(event, f"📤 正在读取收藏夹「{folder_name}」...")
            rows = await asyncio.to_thread(
                self._collect_favorite_rows, client, folder_id
            )
            if not rows:
                yield self.messaging.plain(event, f"ℹ️ 收藏夹「{folder_name}」为空，没有可导出的内容")
                return

            export_dir = self.data_dir / "exports"
            safe_name = self.text.safe_filename(folder_name or f"收藏夹{folder_id}")
            output_path = export_dir / f"JM收藏夹_{folder_id}_{safe_name}.csv"
            await asyncio.to_thread(self._write_favorites_csv, output_path, rows)

            size_kb = max(1, math.ceil(output_path.stat().st_size / 1024))
            self.quota.record_file_send()
            yield self.messaging.chain(event, [
                Comp.Plain(f"📎 {output_path.name} ({size_kb}KB，共{len(rows)}本)\n"),
                Comp.File(file=str(output_path), name=output_path.name),
            ])
            output_path.unlink(missing_ok=True)
            await asyncio.sleep(self.send_interval)
            yield self.messaging.plain(event, f"✅ 已导出收藏夹「{folder_name}」，共 {len(rows)} 本")
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "导出收藏夹"))

    # ========== /jmsub 订阅更新 ==========
    def _load_subscriptions(self):
        try:
            data = json.loads(self.subscription_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"读取订阅文件失败: {e}")
            return {}

    def _save_subscriptions(self, data):
        try:
            tmp = self.subscription_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.subscription_file)
        except Exception as e:
            logger.warning(f"保存订阅文件失败: {e}")

    @staticmethod
    def _album_photo_ids(album):
        result = []
        try:
            for photo in album:
                photo_id = str(getattr(photo, "photo_id", "") or "")
                if photo_id:
                    result.append(photo_id)
        except Exception:
            pass
        return result

    def _subscription_snapshot(self, album):
        try:
            title = str(getattr(album, "name", "") or "")
        except Exception:
            title = ""
        return {
            "title": title,
            "photo_ids": self._album_photo_ids(album),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def _check_subscriptions_once(self):
        """检查所有会话的订阅，有新增章节时推送到对应会话。"""
        data = self._load_subscriptions()
        if not data:
            return
        if self.state.subscription_lock is None:
            self.state.subscription_lock = asyncio.Lock()
        async with self.state.subscription_lock:
            client = await asyncio.to_thread(self._new_logged_client)
            changed = False
            for umo, subs in list(data.items()):
                if not isinstance(subs, dict):
                    continue
                for album_id, info in list(subs.items()):
                    if not isinstance(info, dict):
                        continue
                    try:
                        album = await asyncio.to_thread(
                            client.get_album_detail, album_id
                        )
                        old_ids = {str(x) for x in (info.get("photo_ids") or [])}
                        photos = list(album)
                        new_photos = [
                            p for p in photos
                            if str(getattr(p, "photo_id", "")) not in old_ids
                        ]
                        if not new_photos:
                            continue

                        title = self.text.safe_text(
                            getattr(album, "name", "") or info.get("title", ""),
                            self.convert_title,
                        ) or f"JM{album_id}"
                        lines = [
                            f"🔔 订阅更新：《{title}》新增 {len(new_photos)} 话"
                        ]
                        for i, photo in enumerate(new_photos[:10], 1):
                            chapter_name = self.text.safe_text(
                                getattr(photo, "name", "") or "",
                                self.convert_title,
                            )
                            index = self.catalog.photo_position(photo, i)
                            lines.append(f"  第{index}话 {chapter_name}".rstrip())
                        if len(new_photos) > 10:
                            lines.append(f"  ... 共 {len(new_photos)} 话")
                        lines.append("")
                        lines.append(f"/jmv {album_id} 查看详情")
                        text = self.text.safe_text("\n".join(lines))

                        sent = await self.context.send_message(
                            umo, MessageChain(chain=[Comp.Plain(text)])
                        )
                        if sent is False:
                            logger.warning(f"订阅推送未找到可用平台: {umo}")
                            continue

                        info["photo_ids"] = self._album_photo_ids(album)
                        info["title"] = str(getattr(album, "name", "") or "")
                        info["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        changed = True
                    except Exception as e:
                        logger.warning(f"检查订阅 JM{album_id} 失败: {e}")
                    # 每本之间稍作间隔，避免集中请求触发限流
                    await asyncio.sleep(2)
            if changed:
                self._save_subscriptions(data)

    async def _subscription_loop(self):
        while True:
            interval = max(0, int(self.subscribe_interval or 0))
            if interval <= 0:
                return
            try:
                await asyncio.sleep(interval)
                await self._check_subscriptions_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"订阅检查失败: {e}")
                await asyncio.sleep(60)

    @filter.command("jmsub")
    async def jm_subscribe(self, event: AstrMessageEvent):
        """订阅漫画更新，用法：
        /jmsub <漫画id> → 订阅/更新基线
        /jmsub list → 查看当前会话订阅
        /jmsub del <漫画id> → 取消订阅
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        umo = event.unified_msg_origin
        data = self._load_subscriptions()
        subs = data.get(umo)
        if not isinstance(subs, dict):
            subs = {}

        action = args[1].lower() if len(args) >= 2 else "list"
        if action in ("list", "ls"):
            if not subs:
                yield self.messaging.plain(event,
                    "当前会话还没有订阅。\n"
                    "用法：/jmsub <漫画id> 订阅更新"
                )
                return
            lines = [f"🔔 当前会话订阅（{len(subs)}）："]
            for album_id, info in subs.items():
                title = self.text.safe_text(
                    (info or {}).get("title", "") or "未知", self.convert_title
                )
                count = len((info or {}).get("photo_ids") or [])
                lines.append(f"  JM{album_id} {title}（{count}话）")
            lines.append("")
            lines.append("💡 /jmsub <漫画id> 更新基线，/jmsub del <漫画id> 取消")
            yield self.messaging.plain(event, "\n".join(lines))
            return

        if action in ("del", "remove", "unsub"):
            if len(args) != 3:
                yield self.messaging.plain(event, "用法：/jmsub del <漫画id>")
                return
            _, album_id = self.selection.normalize_target(args[2])
            if not album_id.isdigit() or int(album_id) <= 0:
                yield self.messaging.plain(event, f"❌ 漫画ID必须是大于 0 的数字：{args[2]}")
                return
            if album_id not in subs:
                yield self.messaging.plain(event, f"ℹ️ 当前会话没有订阅 JM{album_id}")
                return
            subs.pop(album_id, None)
            if subs:
                data[umo] = subs
            else:
                data.pop(umo, None)
            self._save_subscriptions(data)
            yield self.messaging.plain(event, f"✅ 已取消订阅 JM{album_id}")
            return

        if len(args) != 2:
            yield self.messaging.plain(event,
                "用法：\n"
                "  /jmsub <漫画id>        订阅更新\n"
                "  /jmsub list            查看当前会话订阅\n"
                "  /jmsub del <漫画id>    取消订阅"
            )
            return

        prefix, album_id = self.selection.normalize_target(args[1])
        if prefix == "p" or not album_id.isdigit() or int(album_id) <= 0:
            yield self.messaging.plain(event,
                f"❌ 漫画ID必须是大于 0 的数字：{args[1]}\n"
                "章节ID不能订阅，请填所属漫画ID"
            )
            return

        yield self.messaging.plain(event, f"🔔 正在读取 JM{album_id} 的话信息...")
        try:
            client = await asyncio.to_thread(self._new_logged_client)
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            if self.catalog.looks_like_chapter_album(album):
                parent_id = await self.catalog.resolve_chapter_album_parent(
                    client, album, album_id
                )
                if parent_id:
                    yield self.messaging.plain(event,
                        f"❌ JM{album_id} 是章节ID，不能订阅。\n"
                        f"所属漫画：JM{parent_id}\n"
                        f"请用 /jmsub {parent_id} 订阅整本更新。"
                    )
                else:
                    yield self.messaging.plain(event,
                        f"❌ JM{album_id} 是章节ID，不能订阅。请改用所属漫画ID。"
                    )
                return

            snapshot = self._subscription_snapshot(album)
            if not snapshot["photo_ids"]:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 没有话列表，暂时无法订阅"
                )
                return

            existed = album_id in subs
            subs[album_id] = snapshot
            data[umo] = subs
            self._save_subscriptions(data)

            title = self.text.safe_text(
                snapshot["title"] or f"JM{album_id}", self.convert_title
            )
            if self.subscribe_interval > 0:
                interval_text = (
                    f"每 {self.subscribe_interval} 秒检查一次"
                    if self.subscribe_interval < 3600
                    else f"每 {self.subscribe_interval // 3600} 小时检查一次"
                )
                hint = f"后台会{interval_text}，发现新话后推送到当前会话。"
            else:
                hint = "后台自动检查已关闭（subscribe_interval=0），当前只保存订阅，不会推送。"
            yield self.messaging.plain(event,
                f"{'🔄 已更新订阅基线' if existed else '✅ 已订阅'}："
                f"《{title}》JM{album_id}\n"
                f"当前 {len(snapshot['photo_ids'])} 话。{hint}"
            )
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "订阅"))

    @filter.command("jmfav")
    async def jm_favorite(self, event: AstrMessageEvent):
        """收藏夹功能：
        /jmfav → 查看收藏夹文件夹列表
        /jmfav <文件夹id> → 查看文件夹里的内容
        /jmfav add <漫画id> → 把漫画加入收藏夹
        /jmfav del <漫画id> → 从收藏夹移除
        /jmfav export [文件夹id] → 导出收藏夹 CSV（30 秒内二次确认）
        """
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        if not self.access.is_owner(event):
            yield self.messaging.plain(event, self.access.owner_required_text())
            return
        args = event.message_str.split()

        operation = args[1].lower() if len(args) >= 2 else ""
        if operation in ("add", "del"):
            if len(args) != 3:
                yield self.messaging.plain(event,
                    f"用法：/jmfav {operation} <漫画id>\n"
                    "示例：/jmfav add 123456"
                )
                return
            _, album_id = self.selection.normalize_target(args[2])
            if not album_id.isdigit() or int(album_id) <= 0:
                yield self.messaging.plain(event, f"❌ 漫画ID必须是大于 0 的数字：{args[2]}")
                return
        elif operation == "export":
            if len(args) > 3:
                yield self.messaging.plain(event,
                    "用法：/jmfav export [文件夹id]\n"
                    "不带文件夹id时导出“全部”收藏"
                )
                return
            folder_id = "0"
            if len(args) == 3:
                token = args[2]
                if not re.fullmatch(r"\d+", token):
                    yield self.messaging.plain(event, f"❌ 文件夹ID必须是数字：{token}")
                    return
                folder_id = str(int(token))

            # 二次确认：首次输入只提示，30 秒内再次输入相同命令才真正导出
            user_key = (str(event.get_sender_id()), event.unified_msg_origin)
            action_key = f"export:{folder_id}"
            key = (*user_key, action_key)
            now = time.monotonic()
            self.state.pending_exports = {
                k: v for k, v in self.state.pending_exports.items()
                if now - v <= 30
                and not (
                    k[0] == user_key[0]
                    and k[1] == user_key[1]
                    and k[2] != action_key
                )
            }
            command_text = "/jmfav export"
            if len(args) == 3:
                command_text += f" {args[2]}"
            if key not in self.state.pending_exports:
                self.state.pending_exports[key] = now
                yield self.messaging.plain(event,
                    f"⚠️ 即将导出收藏夹 [ID:{folder_id}] 为 CSV 文件。\n"
                    "导出会把账号收藏数据写入文件并发送到当前会话，请确认不是误触。\n"
                    f"请在 30 秒内再次输入完全相同的命令确认：\n{command_text}"
                )
                return
            self.state.pending_exports.pop(key, None)
            async for item in self._export_favorites(event, folder_id):
                yield item
            return
        elif len(args) >= 2:
            if len(args) > 2:
                yield self.messaging.plain(event, "用法：/jmfav [文件夹id]\n文件夹ID只能填一个")
                return
            folder_token = args[1]
            if not re.fullmatch(r"\d+", folder_token):
                yield self.messaging.plain(event,
                    f"❌ 无法识别的收藏夹参数：{folder_token}\n"
                    "用法：/jmfav | /jmfav <文件夹id> | /jmfav add <漫画id> | "
                    "/jmfav del <漫画id> | /jmfav export [文件夹id]"
                )
                return
            if int(folder_token) < 0:
                yield self.messaging.plain(event, "❌ 文件夹ID不能是负数")
                return

        try:
            client = self.client.get_client()

            # 从配置里读登录的用户名
            username = ""
            try:
                for plugin in self.option.plugins.after_init:
                    if plugin.get('plugin') == 'login':
                        username = plugin.get('kwargs', {}).get('username', '')
                        break
            except:
                pass

            if not username:
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            # /jmfav add <id> — 添加收藏（仅API客户端支持）
            if len(args) >= 3 and args[1].lower() == 'add':
                if self.client_type != 'api':
                    yield self.messaging.plain(event, "⚠️ 添加收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                _, album_id = self.selection.normalize_target(args[2])
                yield self.messaging.plain(event, f"⭐ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self._favorite_state(client, album_id)
                if is_fav:
                    yield self.messaging.plain(event, f"ℹ️ JM{album_id} 已经在收藏夹里了")
                else:
                    await asyncio.to_thread(client.add_favorite_album, album_id, folder_id='0')
                    self._clear_client_cache(client)
                    yield self.messaging.plain(event, f"✅ 已收藏 JM{album_id}")
                try:
                    yield self.messaging.plain(event, await self._render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav del <id> — 删除收藏（仅API客户端支持，且需jmcomic较新版本）
            if len(args) >= 3 and args[1].lower() == 'del':
                if self.client_type != 'api':
                    yield self.messaging.plain(event, "⚠️ 移除收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                if not hasattr(client, 'delete_favorite_album'):
                    yield self.messaging.plain(event, "⚠️ 当前 jmcomic 版本不支持移除收藏，请升级 jmcomic 到最新版本")
                    return
                _, album_id = self.selection.normalize_target(args[2])
                yield self.messaging.plain(event, f"🗑️ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self._favorite_state(client, album_id)
                if not is_fav:
                    yield self.messaging.plain(event, f"ℹ️ JM{album_id} 本来就不在收藏夹里，无需移除")
                else:
                    await asyncio.to_thread(client.delete_favorite_album, album_id, folder_id='0')
                    self._clear_client_cache(client)
                    yield self.messaging.plain(event, f"✅ 已移除收藏 JM{album_id}")
                try:
                    yield self.messaging.plain(event, await self._render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav <folder_id> — 查看指定文件夹内容
            if len(args) >= 2 and args[1].isdigit():
                folder_id = args[1]
                yield self.messaging.plain(event, f"⭐ 获取收藏夹 [ID:{folder_id}] 的内容...")
                yield self.messaging.plain(event,
                    await self._render_favorites(
                        client, folder_id, f"⭐ 收藏夹 [ID:{folder_id}]", 20
                    )
                )
                return

            # /jmfav — 列出收藏夹文件夹
            yield self.messaging.plain(event, "⭐ 获取我的收藏夹列表...")
            page = await asyncio.to_thread(client.favorite_folder, 1)

            # 列出所有收藏夹文件夹
            folders = list(page.iter_folder_id_name())
            if not folders:
                # 如果没有文件夹列表，直接显示全部收藏
                yield self.messaging.plain(event,
                    await self._render_favorites(client, '0', "⭐ 我的收藏", 20)
                )
                return

            # 显示文件夹列表
            folder_lines = []
            for fid, fname in folders:
                fname_cn = self.text.safe_text(fname, self.convert_title)
                folder_lines.append(f"  📁 [ID:{fid}] {fname_cn}")

            reply = "⭐ 我的收藏夹文件夹：\n\n" + "\n".join(folder_lines)
            reply += "\n\n💡 /jmfav <文件夹id> 查看内容，/jmfav add <漫画id> 添加收藏"
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "收藏夹操作"))

    async def initialize(self):
        """插件激活时启动订阅更新检查。"""
        if self.subscribe_interval > 0 and self.state.subscribe_task is None:
            if self.state.subscription_lock is None:
                self.state.subscription_lock = asyncio.Lock()
            self.state.subscribe_task = asyncio.create_task(self._subscription_loop())
            logger.info(f"订阅更新检查已启动，间隔 {self.subscribe_interval} 秒")

    async def terminate(self):
        task = self.state.subscribe_task
        self.state.subscribe_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"停止订阅检查任务时出错: {e}")
