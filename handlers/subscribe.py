"""订阅增删查。"""

import asyncio

from astrbot.api.event import AstrMessageEvent


class SubscribeHandler:
    def __init__(self, settings, access, client, catalog, selection, subscriptions, text, messaging):
        self.settings = settings
        self.access = access
        self.client = client
        self.catalog = catalog
        self.selection = selection
        self.subscriptions = subscriptions
        self.text = text
        self.messaging = messaging

    async def handle(self, event: AstrMessageEvent):
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
        data = self.subscriptions.load()
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
                    (info or {}).get("title", "") or "未知", self.settings.convert_title
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
            self.subscriptions.save(data)
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
            client = await asyncio.to_thread(self.client.get_client)
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

            snapshot = self.subscriptions.snapshot(album)
            if not snapshot["photo_ids"]:
                yield self.messaging.plain(event,
                    f"⚠️ JM{album_id} 没有话列表，暂时无法订阅"
                )
                return

            existed = album_id in subs
            subs[album_id] = snapshot
            data[umo] = subs
            self.subscriptions.save(data)

            title = self.text.safe_text(
                snapshot["title"] or f"JM{album_id}", self.settings.convert_title
            )
            if self.settings.subscribe_interval > 0:
                interval_text = (
                    f"每 {self.settings.subscribe_interval} 秒检查一次"
                    if self.settings.subscribe_interval < 3600
                    else f"每 {self.settings.subscribe_interval // 3600} 小时检查一次"
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
