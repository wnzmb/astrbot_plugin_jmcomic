"""签到、用户面板与 whoami。"""

import asyncio
from datetime import datetime

import jmcomic
from astrbot.api.event import AstrMessageEvent


class AccountHandler:
    def __init__(self, settings, access, client, checkin, messaging):
        self.settings = settings
        self.access = access
        self.client = client
        self.checkin = checkin
        self.messaging = messaging

    async def daily(self, event: AstrMessageEvent):
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
            try:
                daily = await asyncio.to_thread(client.get_daily)
            except Exception as e:
                if not self.client.is_auth_error(e):
                    raise
                client = self.client.refresh_client()
                daily = await asyncio.to_thread(client.get_daily)
            info = self.checkin.parse_daily_info(daily)
            daily_id = self.checkin.daily_payload(daily).get("daily_id")

            # 执行签到
            yield self.messaging.plain(event, "✅ 正在签到...")
            signed_ok = False
            try:
                try:
                    resp = await asyncio.to_thread(self.checkin.daily_checkin, client, daily_id)
                except Exception as e:
                    # 登录态刚好过期时刷新一次再签
                    if not self.client.is_auth_error(e):
                        raise
                    client = self.client.refresh_client()
                    resp = await asyncio.to_thread(self.checkin.daily_checkin, client, daily_id)
                if resp.code == 0:
                    result_msg = f"🎉 签到成功！\n{resp.msg}"
                    signed_ok = True
                elif resp.code == 1:
                    result_msg = f"📌 今日已签到\n{resp.msg}"
                    signed_ok = True
                else:
                    result_msg = f"签到结果：{resp.msg}"
            except Exception as checkin_err:
                # 兼容旧版 jmcomic 的“已签到”异常文案
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
                    final_info = self.checkin.parse_daily_info(daily_after, assume_today_signed=True)
                except Exception:
                    final_info = self.checkin.parse_daily_info(daily, assume_today_signed=True)
            else:
                final_info = info

            if final_info.get('signed_count') is None:
                final_info['signed_count'] = info.get('signed_count')
            if final_info.get('streak') is None:
                final_info['streak'] = info.get('streak')
            if final_info.get('streak') is None:
                final_info['streak'] = self.checkin.local_streak(bool(final_info.get('today_signed')))

            if signed_ok:
                streak_to_save = final_info.get('streak')
                if streak_to_save is None:
                    streak_to_save = 1
                self.checkin.save_state(streak_to_save, final_info.get('signed_count'))

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

    async def user_info(self, event: AstrMessageEvent):
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
                for plugin in self.settings.option.plugins.after_init:
                    if plugin.get('plugin') == 'login':
                        username = plugin.get('kwargs', {}).get('username', '')
                        break
            except Exception:
                pass

            if not username:
                yield self.messaging.plain(event, "⚠️ 未配置登录账号，请在插件配置中填写账号密码")
                return

            # 获取签到信息（jmcomic 2.7.5+ 才支持）
            info = {}
            if hasattr(client, 'get_daily'):
                try:
                    daily = await asyncio.to_thread(client.get_daily)
                    info = self.checkin.parse_daily_info(daily)
                except Exception:
                    info = {}

                # 接口日历更新延迟时，用本机签到记录兜底
                state = self.checkin.load_state()
                today_str = datetime.now().strftime('%Y-%m-%d')
                if state.get('date') == today_str:
                    info['today_signed'] = True
                if info.get('streak') is None:
                    info['streak'] = self.checkin.local_streak(bool(info.get('today_signed')))
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

            reply = "👤 用户面板\n\n"
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
                reply += "\n💡 /jmcheckin 立即签到"
            else:
                reply += "\n💡 签到功能需升级 jmcomic 到最新版本"

            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "获取用户信息"))

    async def whoami(self, event: AstrMessageEvent):
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
        if not self.settings.owner_ids:
            lines.append("\n⚠️ 尚未配置 owner_ids，账号类命令暂时不可用。")
            lines.append("把上面的ID填入插件配置 owner_ids 即可。")
        yield self.messaging.plain(event, "\n".join(lines))
