"""统一下载流程：目标识别、下载、分册打包、限额与消息预算。"""

import asyncio
import random
import time

from astrbot.api import logger
import astrbot.api.message_components as Comp

from ..core.parsing import format_index_ranges


class DownloadRunner:
    def __init__(self, settings, client, cache, catalog, messaging, quota,
                 history, progress, selection, download, packaging):
        self.settings = settings
        self.client = client
        self.cache = cache
        self.catalog = catalog
        self.messaging = messaging
        self.quota = quota
        self.history = history
        self.progress = progress
        self.selection = selection
        self.download = download
        self.packaging = packaging

    @staticmethod
    def tease_for_count(count):
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

    async def run_download(self, event, target_str, output_format,
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
            if kind == "photo" and chapter_expr and str(chapter_expr).startswith("latest:"):
                failed.append(raw)
                yield self.messaging.plain(
                    event, f"⚠️ {target_label} -latest 仅支持漫画ID，不支持单章节ID"
                )
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
                        event, target_id, holder, chapters=chapters,
                        expected_pages=getattr(album, "page_count", 0),
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
                    folder = self.cache.chapter_dir(photo, album_dir)
                    if folder is not None and await self.download.is_photo_complete(photo, folder):
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
                album_metadata_path = self.cache.metadata_file(album_dir)
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
                    output_path = self.settings.zip_base_dir / f"{stem}.{output_format}"
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
                await asyncio.sleep(self.settings.send_interval)

            if target_sent:
                total_count = self.history.add_user(event.get_sender_id(), {
                    "id": target_id,
                    "title": target_title,
                    "time": time.strftime("%Y-%m-%d %H:%M"),
                    "size_mb": round(sum(target_sizes), 1),
                })
                success += 1
                if not quota_stopped and self.settings.tease_enabled:
                    teases = self.tease_for_count(total_count)
                    if teases:
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
