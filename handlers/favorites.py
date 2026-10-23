"""收藏夹增删查与 CSV 导出确认。"""

import asyncio
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


class FavoritesHandler:
    def __init__(self, settings, access, client, selection, favorites, text, state, messaging):
        self.settings = settings
        self.access = access
        self.client = client
        self.selection = selection
        self.favorites = favorites
        self.text = text
        self.state = state
        self.messaging = messaging

    async def handle(self, event: AstrMessageEvent):
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
            async for item in self.favorites.export(event, folder_id):
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
                for plugin in self.settings.option.plugins.after_init:
                    if plugin.get('plugin') == 'login':
                        username = plugin.get('kwargs', {}).get('username', '')
                        break
            except Exception:
                pass

            if not username:
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            # /jmfav add <id> — 添加收藏（仅API客户端支持）
            if len(args) >= 3 and args[1].lower() == 'add':
                if self.settings.client_type != 'api':
                    yield self.messaging.plain(event, "⚠️ 添加收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                _, album_id = self.selection.normalize_target(args[2])
                yield self.messaging.plain(event, f"⭐ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self.favorites.favorite_state(client, album_id)
                if is_fav:
                    yield self.messaging.plain(event, f"ℹ️ JM{album_id} 已经在收藏夹里了")
                else:
                    await asyncio.to_thread(client.add_favorite_album, album_id, folder_id='0')
                    self.favorites.clear_client_cache(client)
                    yield self.messaging.plain(event, f"✅ 已收藏 JM{album_id}")
                try:
                    yield self.messaging.plain(event, await self.favorites.render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav del <id> — 删除收藏（仅API客户端支持，且需jmcomic较新版本）
            if len(args) >= 3 and args[1].lower() == 'del':
                if self.settings.client_type != 'api':
                    yield self.messaging.plain(event, "⚠️ 移除收藏仅支持 API 客户端，请在插件配置中将客户端类型改为 api")
                    return
                if not hasattr(client, 'delete_favorite_album'):
                    yield self.messaging.plain(event, "⚠️ 当前 jmcomic 版本不支持移除收藏，请升级 jmcomic 到最新版本")
                    return
                _, album_id = self.selection.normalize_target(args[2])
                yield self.messaging.plain(event, f"🗑️ 正在检查 JM{album_id} 的收藏状态...")
                is_fav, _album = await self.favorites.favorite_state(client, album_id)
                if not is_fav:
                    yield self.messaging.plain(event, f"ℹ️ JM{album_id} 本来就不在收藏夹里，无需移除")
                else:
                    await asyncio.to_thread(client.delete_favorite_album, album_id, folder_id='0')
                    self.favorites.clear_client_cache(client)
                    yield self.messaging.plain(event, f"✅ 已移除收藏 JM{album_id}")
                try:
                    yield self.messaging.plain(event, await self.favorites.render_favorites(client, '0'))
                except Exception as list_err:
                    logger.warning(f"刷新收藏夹失败: {list_err}")
                return

            # /jmfav <folder_id> — 查看指定文件夹内容
            if len(args) >= 2 and args[1].isdigit():
                folder_id = args[1]
                yield self.messaging.plain(event, f"⭐ 获取收藏夹 [ID:{folder_id}] 的内容...")
                yield self.messaging.plain(event,
                    await self.favorites.render_favorites(
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
                    await self.favorites.render_favorites(client, '0', "⭐ 我的收藏", 20)
                )
                return

            # 显示文件夹列表
            folder_lines = []
            for fid, fname in folders:
                fname_cn = self.text.safe_text(fname, self.settings.convert_title)
                folder_lines.append(f"  📁 [ID:{fid}] {fname_cn}")

            reply = "⭐ 我的收藏夹文件夹：\n\n" + "\n".join(folder_lines)
            reply += "\n\n💡 /jmfav <文件夹id> 查看内容，/jmfav add <漫画id> 添加收藏"
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "收藏夹操作"))
