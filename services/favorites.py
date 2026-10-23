"""收藏夹：收藏状态、列表渲染、CSV 收集/写入与导出。"""

import asyncio
import csv
import math

from astrbot.api import logger
import astrbot.api.message_components as Comp


class FavoritesService:
    def __init__(self, settings, client, text, messaging, quota):
        self.settings = settings
        self.client = client
        self.text = text
        self.messaging = messaging
        self.quota = quota

    @staticmethod
    def clear_client_cache(client):
        """收藏状态是 toggle 接口，操作前后必须清缓存，避免 is_favorite 读到旧值"""
        try:
            cache = client.get_cache_dict()
            if cache:
                cache.clear()
        except Exception:
            pass

    async def _call(self, client, method_name, *args, **kwargs):
        """调用客户端方法；遇到 401 时刷新登录态并重试一次。"""
        try:
            method = getattr(client, method_name)
            return await asyncio.to_thread(method, *args, **kwargs)
        except Exception as e:
            if not self.client.is_auth_error(e):
                raise
            fresh = self.client.refresh_client()
            method = getattr(fresh, method_name)
            return await asyncio.to_thread(method, *args, **kwargs)

    async def fetch_folder(self, client, page=1, folder_id='0'):
        """读取一页收藏夹，供 /jmfav 列表使用。"""
        return await self._call(client, 'favorite_folder', page, folder_id=folder_id)

    async def add_favorite(self, client, album_id, folder_id='0'):
        return await self._call(client, 'add_favorite_album', album_id, folder_id=folder_id)

    async def delete_favorite(self, client, album_id, folder_id='0'):
        return await self._call(client, 'delete_favorite_album', album_id, folder_id=folder_id)

    async def favorite_state(self, client, album_id):
        """返回 (是否已收藏, 漫画详情)"""
        self.clear_client_cache(client)
        album = await self._call(client, 'get_album_detail', album_id)
        return bool(getattr(album, 'is_favorite', False)), album

    async def render_favorites(self, client, folder_id='0', title='⭐ 当前收藏', limit=10):
        """渲染收藏夹内容，操作后自动调用"""
        page = await self._call(client, 'favorite_folder', 1, folder_id=folder_id)
        total = getattr(page, 'total', None)
        items = list(page.iter_id_title_tag())
        if not items:
            return f"{title}：空"

        results = []
        for i, (aid, item_title, tags) in enumerate(items[:limit]):
            item_title = self.text.safe_text(item_title, self.settings.convert_title)
            tags = [self.text.safe_text(t, self.settings.convert_tags) for t in tags]
            safe_tags = tags[:3]
            tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
            results.append(f"{i+1}. [JM{aid}] {item_title}{tag_str}")

        total_text = total if total is not None else len(items)
        reply = f"{title}（共{total_text}本，显示前{len(results)}本）：\n\n" + "\n".join(results)
        reply += "\n\n/jmv <id> 查详情，/jm <id> 下载"
        return reply

    @staticmethod
    def collect_rows(client, folder_id):
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

    async def collect_rows_async(self, client, folder_id):
        """异步收集收藏夹；401 时刷新登录态重试一次。"""
        try:
            return await asyncio.to_thread(self.collect_rows, client, folder_id)
        except Exception as e:
            if not self.client.is_auth_error(e):
                raise
            fresh = self.client.refresh_client()
            return await asyncio.to_thread(self.collect_rows, fresh, folder_id)

    @staticmethod
    def write_csv(path, rows):
        """写 CSV；utf-8-sig 方便 Windows Excel 直接打开。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'author', 'name'])
            writer.writerows(rows)

    async def export(self, event, folder_id: str):
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
                    page = await self._call(client, 'favorite_folder', 1)
                    for fid, fname in page.iter_folder_id_name():
                        if str(fid) == str(folder_id):
                            folder_name = fname or folder_name
                            break
                except Exception as e:
                    logger.warning(f"读取收藏夹名称失败: {e}")

            yield self.messaging.plain(event, f"📤 正在读取收藏夹「{folder_name}」...")
            rows = await self.collect_rows_async(client, folder_id)
            if not rows:
                yield self.messaging.plain(event, f"ℹ️ 收藏夹「{folder_name}」为空，没有可导出的内容")
                return

            export_dir = self.settings.data_dir / "exports"
            safe_name = self.text.safe_filename(folder_name or f"收藏夹{folder_id}")
            output_path = export_dir / f"JM收藏夹_{folder_id}_{safe_name}.csv"
            await asyncio.to_thread(self.write_csv, output_path, rows)

            size_kb = max(1, math.ceil(output_path.stat().st_size / 1024))
            self.quota.record_file_send()
            yield self.messaging.chain(event, [
                Comp.Plain(f"📎 {output_path.name} ({size_kb}KB，共{len(rows)}本)\n"),
                Comp.File(file=str(output_path), name=output_path.name),
            ])
            output_path.unlink(missing_ok=True)
            await asyncio.sleep(self.settings.send_interval)
            yield self.messaging.plain(event, f"✅ 已导出收藏夹「{folder_name}」，共 {len(rows)} 本")
        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "导出收藏夹"))
