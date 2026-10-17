import os
import asyncio
import zipfile
import shutil
import math
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp

import jmcomic
from jmcomic import JmMagicConstants


@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.4.0")
class JmComicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None):
        super().__init__(context)
        self.config = config or {}

        # 路径配置：放到 plugin_data 下，插件更新不会丢
        # 插件目录: .../data/plugins/astrbot_plugin_jmcomic/
        # 往上两级到 data/，再进 plugin_data/astrbot_plugin_jmcomic/
        plugin_dir = Path(os.path.dirname(__file__))
        self.data_dir = plugin_dir.parent.parent / "plugin_data" / "astrbot_plugin_jmcomic"
        self.download_base_dir = self.data_dir / "downloads"
        self.zip_base_dir = self.data_dir / "zips"
        self.download_base_dir.mkdir(parents=True, exist_ok=True)
        self.zip_base_dir.mkdir(parents=True, exist_ok=True)

        # 从插件配置读取参数
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        domain = self.config.get("domain", "comic18j-guta.site")
        photo_threads = int(self.config.get("photo_threads", 8))
        image_threads = int(self.config.get("image_threads", 20))

        # 动态生成 jmcomic 配置
        option_dict = {
            "log": False,
            "client": {
                "impl": "html",
                "retry_times": 5,
                "domain": {
                    "html": [domain, "18comic.vip"]
                },
                "postman": {
                    "meta_data": {
                        "proxies": None,
                        "timeout": 15
                    }
                }
            },
            "download": {
                "cache": True,
                "image": {
                    "decode": True,
                    "suffix": ".jpg"
                },
                "threading": {
                    "image": image_threads,
                    "photo": photo_threads
                }
            },
            "plugins": {
                "after_init": []
            }
        }

        # 如果填了账号密码，加登录插件
        if username and password:
            option_dict["plugins"]["after_init"].append({
                "plugin": "login",
                "kwargs": {
                    "username": username,
                    "password": password
                }
            })

        # 转成 yml 字符串加载
        import yaml
        option_str = yaml.dump(option_dict, allow_unicode=True)
        self.option = jmcomic.create_option_by_str(option_str)
        self.option.dir_rule.base_dir = str(self.download_base_dir)

        # 排序方式映射
        self.order_map = {
            'mr': '最新',
            'mv': '最多浏览',
            'mp': '最多页数',
            'tf': '最多收藏',
            'tr': '评分最高',
            'md': '最多评论',
        }

        logger.info(f"JMComic 插件初始化完成 v0.4.0，镜像站: {domain}")

        # 历史记录文件
        self.history_file = self.data_dir / "history.json"
        if not self.history_file.exists():
            self.history_file.write_text("[]", encoding="utf-8")

    def _load_history(self):
        """加载下载历史"""
        try:
            return json.loads(self.history_file.read_text(encoding="utf-8"))
        except:
            return []

    def _save_history(self, history):
        """保存下载历史"""
        self.history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

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

    def _parse_search_args(self, args):
        """
        解析搜索参数：关键词 + 可选页码 + 可选排序
        支持格式：
          /jms 关键词
          /jms 关键词 2
          /jms 关键词 mv
          /jms 关键词 2 mv
        返回：(keyword, qq_page, order_by)
        """
        keyword = args[1]
        qq_page = 1
        order_by = JmMagicConstants.ORDER_BY_LATEST  # 默认最新

        # 解析后面的参数（页码/排序）
        extra = args[2:]
        for arg in extra:
            if arg.isdigit():
                qq_page = int(arg)
            elif arg in self.order_map:
                order_by = arg

        return keyword, qq_page, order_by

    async def _render_search_page(self, event, client, search_method, keyword, qq_page, order_by, title_prefix=""):
        """通用搜索结果渲染：分页 + 切片 + 输出"""
        PER_QQ_PAGE = 10
        PER_WEB_PAGE = 80
        QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE  # = 8

        web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
        offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

        order_name = self.order_map.get(order_by, order_by)
        yield event.plain_result(f"🔍 {title_prefix}{keyword}（{order_name}，第{qq_page}页）...")

        try:
            page = await asyncio.to_thread(search_method, keyword, web_page, order_by)

            all_items = list(page.iter_id_title())
            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            if not page_items:
                yield event.plain_result(f"第{qq_page}页没有更多结果了")
                return

            results = []
            for i, (aid, item_title) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                results.append(f"{global_index}. [JM{aid}] {item_title}")

            reply = f"{title_prefix}：{keyword}（{order_name}）\n"
            reply += f"第{qq_page}页，第{start+1}-{start+len(page_items)}条\n\n" + "\n".join(results)
            reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"

            # 下一页提示
            next_cmd = f"/jms {keyword} {qq_page + 1}"
            if order_by != JmMagicConstants.ORDER_BY_LATEST:
                next_cmd += f" {order_by}"
            reply += f"\n下一页：{next_cmd}"

            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"搜索失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 搜索失败：{e}")

    # ========== 工具：从消息里提取数字ID ==========
    def _extract_id(self, message_str: str) -> str:
        """从消息字符串里提取第一个连续数字，作为漫画ID"""
        import re
        match = re.search(r'\d+', message_str)
        return match.group() if match else None

    # ========== /jmv 查详情 ==========
    @filter.command("jmv")
    async def jm_view(self, event: AstrMessageEvent):
        """查看漫画详情，用法：/jmv <id> [选项]
        选项：
          -author  去掉作者
          -work    去掉原作
          -role    去掉角色
        示例：/jmv 123456 -role -work
        """
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：/jmv <漫画id> [选项]\n"
                "选项：-author 去掉作者 | -work 去掉原作 | -role 去掉角色\n"
                "示例：/jmv 350234 或 /jmv 350234 -role -work"
            )
            return

        album_id = args[1]
        options = set(args[2:])  # 后面的参数都是选项

        yield event.plain_result(f"🔍 查询 JM{album_id} ...")

        try:
            client = self.option.new_jm_client()
            album = await asyncio.to_thread(client.get_album_detail, album_id)

            chapters = list(album) if hasattr(album, '__iter__') else []
            authors = getattr(album, 'authors', [])
            works = getattr(album, 'works', [])
            actors = getattr(album, 'actors', [])
            page_count = getattr(album, 'page_count', '?')

            lines = [
                f"📖 {album.name}",
                f"🆔 JM{album_id}",
            ]

            if '-author' not in options:
                lines.append(f"✍️ 作者：{', '.join(authors) if authors else '未知'}")
            if '-work' not in options:
                lines.append(f"🎬 原作：{', '.join(works) if works else '无'}")
            if '-role' not in options:
                lines.append(f"👥 角色：{', '.join(actors[:3]) if actors else '无'}")

            lines.append(f"📄 章节：{len(chapters)}章")
            lines.append(f"🖼️ 页数：{page_count}页")

            reply = "\n".join(lines)
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"查询失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败：{e}")

    # ========== /jm 下载并发ZIP ==========
    @filter.command("jm")
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画并打包ZIP发到QQ，用法：/jm <id> 或 /jm help"""
        args = event.message_str.split()

        # 不带参数或 help，输出帮助
        if len(args) < 2 or args[1].lower() == 'help':
            help_text = (
                "📖 漫画站插件 帮助\n"
                "━━━━━━━━━━━━━━━\n"
                "🔍 搜索：\n"
                "  /jms 关键词 [页码] [排序]   全站搜索\n"
                "  /jma 作者名 [页码] [排序]   按作者搜索\n"
                "  /jmt 标签名 [页码] [排序]   按标签搜索\n"
                "  /jmr 角色名 [页码] [排序]   按角色搜索\n"
                "━━━━━━━━━━━━━━━\n"
                "📖 详情：\n"
                "  /jmv <漫画id> [选项]       查详情\n"
                "    选项：-author -work -role 去掉对应字段\n"
                "━━━━━━━━━━━━━━━\n"
                "⬇️ 下载：\n"
                "  /jm <漫画id>                下载并发ZIP\n"
                "━━━━━━━━━━━━━━━\n"
                "📊 其他：\n"
                "  /jmrank day/week/month     排行榜\n"
                "  /jmfav                      我的收藏夹\n"
                "  /jmhistory                  下载历史\n"
                "  /jmrand [关键词]            随机抽一个\n"
                "━━━━━━━━━━━━━━━\n"
                "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)"
            )
            yield event.plain_result(help_text)
            return

        album_id = args[1]
        yield event.plain_result(f"⬇️ 开始下载 JM{album_id} ...")

        try:
            result = await jmcomic.download_album_async(album_id, self.option)
            album = result.detail

            album_dir = Path(album.save_path)
            if not album_dir.exists():
                yield event.plain_result("⚠️ 下载完成但找不到目录")
                return

            yield event.plain_result(f"📦 正在打包ZIP ...")
            zip_name = f"JM{album_id}_{album.name[:30]}.zip"
            for ch in r'\/:*?"<>|':
                zip_name = zip_name.replace(ch, '_')
            zip_path = self.zip_base_dir / zip_name

            await asyncio.to_thread(self._zip_folder, album_dir, zip_path)
            size_mb = zip_path.stat().st_size / 1024 / 1024

            yield event.plain_result(
                f"✅ 完成！耗时 {result.duration:.1f}s，大小 {size_mb:.1f}MB\n"
                f"正在发送文件..."
            )
            yield event.chain_result([
                Comp.Plain(f"📎 {zip_name}\n"),
                Comp.File(file=str(zip_path), name=zip_name)
            ])

            # 发送完删除原始图片文件夹，只保留ZIP
            await asyncio.to_thread(lambda: shutil.rmtree(album_dir, ignore_errors=True))
            logger.info(f"已清理原始图片目录: {album_dir}")

            # 写入下载历史
            history = self._load_history()
            history.insert(0, {
                "id": album_id,
                "title": album.name,
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "size_mb": round(size_mb, 1)
            })
            history = history[:50]  # 最多保留50条
            self._save_history(history)

        except Exception as e:
            logger.error(f"下载失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 失败：{e}")

    # ========== /jmrand 随机漫画 ==========
    @filter.command("jmrand")
    async def jm_random(self, event: AstrMessageEvent):
        """随机抽一个漫画，用法：/jmrand [标签名]
        不填标签就从日榜随机，填了就从指定标签里随机
        """
        args = event.message_str.split()
        tag_name = " ".join(args[1:]) if len(args) > 1 else None

        if tag_name:
            yield event.plain_result(f"🎲 正在从「{tag_name}」标签随机抽一个...")
        else:
            yield event.plain_result("🎲 正在从日榜随机抽一个漫画...")

        try:
            import random
            client = self.option.new_jm_client()

            if tag_name:
                # 全站搜索，关键词可以是标题/标签/作者
                page = await asyncio.to_thread(client.search_site, tag_name, 1)
            else:
                # 从日榜第1页随机挑一个
                page = await asyncio.to_thread(client.day_ranking, 1)

            items = list(page.iter_id_title())
            if not items:
                yield event.plain_result("没抽到，再试一次")
                return

            album_id, title = random.choice(items)

            # 查一下详情，拿章节数和页数
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            chapters = list(album) if hasattr(album, '__iter__') else []
            page_count = getattr(album, 'page_count', '?')

            yield event.plain_result(
                f"🎲 抽到了！\n\n"
                f"📖 {title}\n"
                f"🆔 JM{album_id}\n"
                f"📄 {len(chapters)}章 / {page_count}页\n\n"
                f"发 /jm {album_id} 下载"
            )

        except Exception as e:
            logger.error(f"随机失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 随机失败：{e}")

    # ========== /jms 全站搜索 ==========
    @filter.command("jms")
    async def jm_search(self, event: AstrMessageEvent):
        """全站搜索，用法：/jms <关键词> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：\n"
                "  /jms <关键词>\n"
                "  /jms <关键词> 2\n"
                "  /jms <关键词> mv   (mv=最多浏览, tf=最多收藏)"
            )
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self.option.new_jm_client()

        async for result in self._render_search_page(
            event, client, client.search_site, keyword, qq_page, order_by, "全站搜索"
        ):
            yield result

    # ========== /jma 按作者搜索 ==========
    @filter.command("jma")
    async def jm_search_author(self, event: AstrMessageEvent):
        """按作者搜索，用法：/jma <作者名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jma <作者名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self.option.new_jm_client()

        async for result in self._render_search_page(
            event, client, client.search_author, keyword, qq_page, order_by, "作者搜索"
        ):
            yield result

    # ========== /jmt 按标签搜索 ==========
    @filter.command("jmt")
    async def jm_search_tag(self, event: AstrMessageEvent):
        """按标签搜索，用法：/jmt <标签名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmt <标签名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self.option.new_jm_client()

        async for result in self._render_search_page(
            event, client, client.search_tag, keyword, qq_page, order_by, "标签搜索"
        ):
            yield result

    # ========== /jmr 按角色搜索 ==========
    @filter.command("jmr")
    async def jm_search_actor(self, event: AstrMessageEvent):
        """按角色/演员搜索，用法：/jmr <角色名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmr <角色名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self.option.new_jm_client()

        async for result in self._render_search_page(
            event, client, client.search_actor, keyword, qq_page, order_by, "角色搜索"
        ):
            yield result

    # ========== /jmrank 排行榜 ==========
    @filter.command("jmrank")
    async def jm_ranking(self, event: AstrMessageEvent):
        """排行榜，用法：/jmrank day|week|month [页码]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "用法：\n"
                "  /jmrank day    日榜\n"
                "  /jmrank week   周榜\n"
                "  /jmrank month  月榜"
            )
            return

        rank_type = args[1].lower()
        qq_page = int(args[2]) if len(args) >= 3 else 1

        rank_map = {
            'day': ('日榜', 'day_ranking'),
            'week': ('周榜', 'week_ranking'),
            'month': ('月榜', 'month_ranking'),
        }

        if rank_type not in rank_map:
            yield event.plain_result("类型只能是 day / week / month")
            return

        rank_name, method_name = rank_map[rank_type]
        yield event.plain_result(f"🏆 获取{rank_name} 第{qq_page}页...")

        try:
            client = self.option.new_jm_client()
            method = getattr(client, method_name)

            PER_QQ_PAGE = 10
            PER_WEB_PAGE = 80
            QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

            web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
            offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

            page = await asyncio.to_thread(method, web_page)
            all_items = list(page.iter_id_title())

            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            if not page_items:
                yield event.plain_result("没有更多了")
                return

            results = []
            for i, (aid, title) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                results.append(f"{global_index}. [JM{aid}] {title}")

            reply = f"🏆 {rank_name}（第{qq_page}页）\n\n" + "\n".join(results)
            reply += f"\n\n下一页：/jmrank {rank_type} {qq_page + 1}"
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"排行榜获取失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 失败：{e}")

    # ========== /jmhistory 下载历史 ==========
    @filter.command("jmhistory")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhistory"""
        history = self._load_history()
        if not history:
            yield event.plain_result("还没有下载记录")
            return

        results = []
        for i, item in enumerate(history[:20]):
            results.append(f"{i+1}. [JM{item['id']}] {item['title'][:30]}  ({item['size_mb']}MB)  {item['time']}")

        reply = "📜 下载历史（最近20条）：\n\n" + "\n".join(results)
        reply += "\n\n/jm <id> 重新下载"
        yield event.plain_result(reply)

    # ========== /jmfav 收藏夹 ==========
    @filter.command("jmfav")
    async def jm_favorite(self, event: AstrMessageEvent):
        """收藏夹功能：
        /jmfav → 查看收藏夹文件夹列表
        /jmfav <文件夹id> → 查看文件夹里的内容
        /jmfav add <漫画id> → 把漫画加入收藏夹
        """
        args = event.message_str.split()

        try:
            client = self.option.new_jm_client()

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
                yield event.plain_result("⚠️ 未配置登录账号，无法使用收藏夹功能")
                return

            # /jmfav add <id> → 加入收藏
            if len(args) >= 3 and args[1].lower() == "add":
                album_id = args[2]
                yield event.plain_result(f"⭐ 正在收藏 JM{album_id} ...")
                await asyncio.to_thread(client.add_favorite_album, album_id)
                yield event.plain_result(f"✅ 已收藏 JM{album_id}")
                return

            # /jmfav <文件夹id> → 查看文件夹内容
            if len(args) >= 2 and args[1].isdigit():
                folder_id = args[1]
                yield event.plain_result(f"📂 获取收藏夹内容...")
                page = await asyncio.to_thread(client.favorite_folder, 1, username=username, folder_id=folder_id)
                items = list(page.iter_id_title())

                if not items:
                    yield event.plain_result("这个文件夹是空的")
                    return

                results = []
                for i, (aid, title) in enumerate(items[:10]):
                    results.append(f"{i+1}. [JM{aid}] {title}")

                reply = f"📂 收藏夹内容（前10条）：\n\n" + "\n".join(results)
                reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"
                yield event.plain_result(reply)
                return

            # /jmfav → 文件夹列表
            yield event.plain_result("📂 获取收藏夹列表...")
            folders = await asyncio.to_thread(client.favorite_folder, 1, username=username)

            results = []
            for i, folder in enumerate(folders):
                name = getattr(folder, 'name', str(folder))
                count = getattr(folder, 'num', '?')
                folder_id = getattr(folder, 'id', i)
                results.append(f"{i+1}. {name}（{count}本）  /jmfav {folder_id}")

            reply = "📂 我的收藏夹：\n\n" + "\n".join(results)
            reply += "\n\n/jmfav add <id> 收藏本子"
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"收藏夹操作失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 失败：{e}")

    async def terminate(self):
        pass
