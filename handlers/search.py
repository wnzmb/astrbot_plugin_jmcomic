"""随机抽漫与五类搜索命令。"""

import asyncio

from astrbot.api.event import AstrMessageEvent


class SearchHandler:
    def __init__(self, settings, access, client, search, text, messaging, album):
        self.settings = settings
        self.access = access
        self.client = client
        self.search = search
        self.text = text
        self.messaging = messaging
        self.album = album

    async def random(self, event: AstrMessageEvent):
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
            title = self.text.safe_text(title, self.settings.convert_title)
            tags = [self.text.safe_text(t, self.settings.convert_tags) for t in tags]
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

    async def site(self, event: AstrMessageEvent):
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
        if self.settings.numeric_search_as_album and len(args) == 2 and args[1].isdigit():
            async for result in self.album.view(event):
                yield result
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jms <关键词> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_site, keyword, qq_page, order_by, "全站搜索", "jms"
        ):
            yield result

    async def author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jma <作者名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jma <作者名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_author, keyword, qq_page, order_by, "作者搜索", "jma"
        ):
            yield result

    async def tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmt <标签名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmt <标签名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_tag, keyword, qq_page, order_by, "标签搜索", "jmt"
        ):
            yield result

    async def actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmr <角色名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmr <角色名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_actor, keyword, qq_page, order_by, "角色搜索", "jmr"
        ):
            yield result

    async def work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        if not self.access.check_permission(event):
            yield self.messaging.plain(event, self.access.permission_denied_text(event))
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield self.messaging.plain(event, "用法：/jmw <原作名>")
            return

        try:
            keyword, qq_page, order_by = self.search.parse_search_args(args)
        except ValueError as e:
            yield self.messaging.plain(event, f"⚠️ {e}\n用法：/jmw <原作名> [页码] [排序]")
            return
        client = self.client.get_client()

        async for result in self.search.render_search_page(
            event, client, client.search_work, keyword, qq_page, order_by, "原作搜索", "jmw"
        ):
            yield result
