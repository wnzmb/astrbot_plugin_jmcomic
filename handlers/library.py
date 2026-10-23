"""排行榜、下载历史与评论查看。"""

import asyncio
import math

from astrbot.api.event import AstrMessageEvent

from ..core.parsing import parse_positive_page


class LibraryHandler:
    def __init__(self, settings, access, client, history, pagination, messaging, text, catalog, selection, state):
        self.settings = settings
        self.access = access
        self.client = client
        self.history = history
        self.pagination = pagination
        self.messaging = messaging
        self.text = text
        self.catalog = catalog
        self.selection = selection
        self.state = state

    async def ranking(self, event: AstrMessageEvent):
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

            PER_QQ_PAGE = self.settings.per_page
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
                title = self.text.safe_text(title, self.settings.convert_title)
                tags = [self.text.safe_text(t, self.settings.convert_tags) for t in tags]
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

    async def history(self, event: AstrMessageEvent):
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
                    title = self.text.safe_text(item.get('title', ''), self.settings.convert_title)
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

        page_size = self.settings.per_page
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
            title = self.text.safe_text(item.get('title', ''), self.settings.convert_title)
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

    async def comments(self, event: AstrMessageEvent):
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
        if page_num > 1 and self.settings.client_type != 'api':
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
                content = self.text.safe_text(raw_content.replace('\n', ' ').strip()[:120], self.settings.convert_comment)

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
            for i, comment in enumerate(comments[:self.settings.comment_count]):
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
