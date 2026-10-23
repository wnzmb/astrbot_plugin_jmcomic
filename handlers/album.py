"""漫画详情与话列表。"""

import asyncio
import math

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
import astrbot.api.message_components as Comp

from ..core.parsing import parse_positive_page


class AlbumHandler:
    def __init__(self, settings, access, client, catalog, pagination, messaging, text, selection):
        self.settings = settings
        self.access = access
        self.client = client
        self.catalog = catalog
        self.pagination = pagination
        self.messaging = messaging
        self.text = text
        self.selection = selection

    async def view(self, event: AstrMessageEvent):
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

            title = self.text.safe_text(getattr(album, 'name', ''), self.settings.convert_title) or "未知"
            author_str = join_values(authors, self.settings.convert_author)
            work_str = join_values(works, self.settings.convert_title)
            actor_str = join_values(actors, self.settings.convert_tags)
            tag_str = join_values(tags, self.settings.convert_tags)

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
                cover_path = self.settings.data_dir / f"cover_{album_id}.jpg"
                cover_pdf = self.settings.data_dir / f"cover_{album_id}.pdf"
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

    async def chapter_list(self, event: AstrMessageEvent):
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

            page_size = self.settings.per_page
            total_pages = max(1, math.ceil(len(chapters) / page_size))
            if page_num > total_pages:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 共 {len(chapters)} 话，只有 {total_pages} 页，"
                    f"已到最后一页（/jmlist {album_id} {total_pages}）"
                )
                return

            start = (page_num - 1) * page_size
            page_items = chapters[start:start + page_size]
            album_title = self.text.safe_text(album.name, self.settings.convert_title)

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
