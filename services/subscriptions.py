"""订阅：订阅文件读写、更新快照与后台轮询推送。"""

import asyncio
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp


class SubscriptionService:
    def __init__(self, settings, client, text, catalog, state, context):
        self.settings = settings
        self.client = client
        self.text = text
        self.catalog = catalog
        self.state = state
        self.context = context

    def load(self):
        try:
            data = json.loads(self.settings.subscription_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"读取订阅文件失败: {e}")
            return {}

    def save(self, data):
        try:
            tmp = self.settings.subscription_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.settings.subscription_file)
        except Exception as e:
            logger.warning(f"保存订阅文件失败: {e}")

    @staticmethod
    def album_photo_ids(album):
        result = []
        try:
            for photo in album:
                photo_id = str(getattr(photo, "photo_id", "") or "")
                if photo_id:
                    result.append(photo_id)
        except Exception:
            pass
        return result

    def snapshot(self, album):
        try:
            title = str(getattr(album, "name", "") or "")
        except Exception:
            title = ""
        return {
            "title": title,
            "photo_ids": self.album_photo_ids(album),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def check_once(self):
        """检查所有会话的订阅，有新增章节时推送到对应会话。"""
        data = self.load()
        if not data:
            return
        if self.state.subscription_lock is None:
            self.state.subscription_lock = asyncio.Lock()
        async with self.state.subscription_lock:
            client = await asyncio.to_thread(self.client.get_client)
            push_limit = max(
                1, int(getattr(self.settings, "subscription_push_limit", 10) or 10)
            )
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
                            self.settings.convert_title,
                        ) or f"JM{album_id}"
                        lines = [
                            f"🔔 订阅更新：《{title}》新增 {len(new_photos)} 话"
                        ]
                        for i, photo in enumerate(new_photos[:push_limit], 1):
                            chapter_name = self.text.safe_text(
                                getattr(photo, "name", "") or "",
                                self.settings.convert_title,
                            )
                            index = self.catalog.photo_position(photo, i)
                            lines.append(f"  第{index}话 {chapter_name}".rstrip())
                        if len(new_photos) > push_limit:
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

                        info["photo_ids"] = self.album_photo_ids(album)
                        info["title"] = str(getattr(album, "name", "") or "")
                        info["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        changed = True
                    except Exception as e:
                        logger.warning(f"检查订阅 JM{album_id} 失败: {e}")
                    # 每本之间稍作间隔，避免集中请求触发限流
                    await asyncio.sleep(2)
            if changed:
                self.save(data)

    async def loop(self):
        while True:
            interval = max(0, int(self.settings.subscribe_interval or 0))
            if interval <= 0:
                return
            try:
                await asyncio.sleep(interval)
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"订阅检查失败: {e}")
                await asyncio.sleep(60)
