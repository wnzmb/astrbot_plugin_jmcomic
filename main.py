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


@register("astrbot_plugin_jmcomic", "wnzmb", "漫画下载插件", "0.5.0")
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
        domain_str = self.config.get("domain", "comic18j-guta.site")
        # 支持多个域名，逗号分隔
        custom_domains = [d.strip() for d in domain_str.split(",") if d.strip()]
        photo_threads = int(self.config.get("photo_threads", 8))
        image_threads = int(self.config.get("image_threads", 20))
        self.per_page = int(self.config.get("per_page", 10))
        self.allow_group = self.config.get("allow_group", True)
        self.convert_enabled = self.config.get("convert_enabled", True)
        self.convert_title = self.config.get("convert_title", True)
        self.convert_tags = self.config.get("convert_tags", True)
        self.convert_author = self.config.get("convert_author", True)
        self.convert_comment = self.config.get("convert_comment", True)
        self.sensitive_filter = self.config.get("sensitive_filter", True)
        self.tease_enabled = self.config.get("tease_enabled", True)
        self.cache_days = int(self.config.get("cache_days", 7))
        self.comment_count = int(self.config.get("comment_count", 10))
        self.history_max = int(self.config.get("history_max", 50))

        # 解析用户自定义敏感词
        self.custom_sensitive_map = {}
        custom_sensitive = self.config.get("custom_sensitive", "")
        for line in custom_sensitive.strip().split("\n"):
            line = line.strip()
            if "=" in line:
                word, pinyin = line.split("=", 1)
                word = word.strip()
                pinyin = pinyin.strip()
                if word and pinyin:
                    self.custom_sensitive_map[word] = pinyin

        # 解析用户自定义调侃语录
        self.custom_teases = []
        custom_teases = self.config.get("custom_teases", "")
        for line in custom_teases.strip().split("\n"):
            line = line.strip()
            if line:
                self.custom_teases.append(line)

        # 启动时自动清理过期缓存
        if self.cache_days > 0:
            import time as _time
            now = _time.time()
            for item in self.download_base_dir.iterdir():
                if item.is_dir() and not item.name.startswith('.'):
                    mtime = item.stat().st_mtime
                    if now - mtime > self.cache_days * 86400:
                        shutil.rmtree(item, ignore_errors=True)
                        logger.info(f"自动清理过期缓存: {item.name}")

        # 动态生成 jmcomic 配置
        option_dict = {
            "log": False,
            "client": {
                "impl": "html",
                "retry_times": 5,
                "cache": True,
                "domain": {
                    "html": custom_domains + ["18comic.vip"]
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
            "dir_rule": {
                "normalize_zh": "zh-cn"
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

        logger.info(f"JMComic 插件初始化完成 v0.5.0，镜像站: {custom_domains}")

        # 历史记录文件
        self.history_file = self.data_dir / "history.json"
        if not self.history_file.exists():
            self.history_file.write_text("{}", encoding="utf-8")

    def _check_permission(self, event: AstrMessageEvent):
        """检查群聊权限"""
        if not self.allow_group:
            # 群聊会有group_id，私聊没有
            group_id = event.get_group_id()
            if group_id:
                return False
        return True

    def _to_simplified(self, text):
        """繁体转简体"""
        if not self.convert_enabled or not text:
            return text
        try:
            import zhconv
            return zhconv.convert(text, 'zh-cn')
        except:
            return text

    def _to_traditional(self, text):
        """简体转繁体，用于搜索"""
        if not text:
            return text
        try:
            import zhconv
            return zhconv.convert(text, 'zh-tw')
        except:
            return text

    def _load_history(self):
        """加载下载历史，结构是 {user_id: [历史列表]}"""
        try:
            data = json.loads(self.history_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            else:
                return {}
        except:
            return {}

    def _save_history(self, data):
        """保存下载历史"""
        self.history_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_user_history(self, user_id):
        data = self._load_history()
        return data.get(user_id, [])

    def _add_user_history(self, user_id, item):
        data = self._load_history()
        if user_id not in data:
            data[user_id] = []
        data[user_id].insert(0, item)
        data[user_id] = data[user_id][:self.history_max]
        self._save_history(data)
        return len(data[user_id])

    def _sanitize_tags(self, tags):
        """敏感词替换成拼音，避免QQ审核"""
        if not self.sensitive_filter:
            return tags
        import re
        from pypinyin import lazy_pinyin
        
        sensitive_map = {}
        # 合并用户自定义敏感词
        sensitive_map.update(self.custom_sensitive_map)
        result = []
        for tag in tags:
            # 先替换已知敏感词
            for cn, pinyin in sensitive_map.items():
                if cn in tag:
                    tag = tag.replace(cn, pinyin)
            
            # 匹配 "X交" 和 "X乳"，只把这两个字转拼音
            def replace_jiao(match):
                word = match.group(0)
                return ''.join(lazy_pinyin(word))
            def replace_ru(match):
                word = match.group(0)
                return ''.join(lazy_pinyin(word))
            
            tag = re.sub(r'.交', replace_jiao, tag)
            tag = re.sub(r'.乳', replace_ru, tag)
            
            result.append(tag)
        return result

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
        PER_QQ_PAGE = self.per_page
        PER_WEB_PAGE = 80
        QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

        web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
        offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

        order_name = self.order_map.get(order_by, order_by)
        yield event.plain_result(f"🔍 {title_prefix}{keyword}（{order_name}，第{qq_page}页）...")

        try:
            # 搜两次：原词 + 繁体词，合并去重，适配半简半繁标题
            import zhconv
            keyword_traditional = zhconv.convert(keyword, 'zh-tw')
            all_items = []
            for kw in [keyword, keyword_traditional]:
                if kw == keyword:
                    page = await asyncio.to_thread(search_method, kw, web_page, order_by)
                else:
                    page = await asyncio.to_thread(search_method, kw, web_page, order_by)
                all_items.extend(list(page.iter_id_title_tag()))
            # 去重
            seen = set()
            unique_items = []
            for item in all_items:
                if item[0] not in seen:
                    seen.add(item[0])
                    unique_items.append(item)
            all_items = unique_items
            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            if not page_items:
                yield event.plain_result(f"第{qq_page}页没有更多结果了")
                return

            results = []
            for i, (aid, item_title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                if self.convert_title:
                    item_title = self._to_simplified(item_title)
                if self.convert_tags:
                    tags = [self._to_simplified(t) for t in tags]
                safe_tags = self._sanitize_tags(tags[:3])
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {item_title}{tag_str}")

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
          -tag     去掉标签
        示例：/jmv 123456 -role -work
        """
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
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
            tags = getattr(album, 'tags', [])
            page_count = getattr(album, 'page_count', '?')
            comment_count = getattr(album, 'comment_count', '?')

            lines = [
                f"📖 {self._to_simplified(album.name) if self.convert_title else album.name}",
                f"🆔 JM{album_id}",
            ]

            if '-author' not in options:
                author_str = ', '.join(authors) if authors else '未知'
                if self.convert_author:
                    author_str = self._to_simplified(author_str)
                lines.append(f"✍️ 作者：{author_str}")
            if '-work' not in options:
                work_str = ', '.join(works) if works else '无'
                if self.convert_title:
                    work_str = self._to_simplified(work_str)
                lines.append(f"🎬 原作：{work_str}")
            if '-role' not in options:
                actor_str = ', '.join(actors[:3]) if actors else '无'
                if self.convert_tags:
                    actor_str = self._to_simplified(actor_str)
                lines.append(f"👥 角色：{actor_str}")

            if '-tag' not in options:
                if self.convert_tags:
                    tags = [self._to_simplified(t) for t in tags]
                safe_tags = self._sanitize_tags(tags)
                if safe_tags:
                    lines.append(f"🏷️ 标签：{', '.join(safe_tags)}")

            lines.append(f"📄 章节：{len(chapters)}章 | 🖼️ 页数：{page_count}页 | 💬 评论：{comment_count}条")
            lines.append(f"\n发 /jm {album_id} 下载")

            reply = "\n".join(lines)
            yield event.plain_result(reply)

            # 下载封面并发送
            cover_path = self.data_dir / f"cover_{album_id}.jpg"
            try:
                await asyncio.to_thread(client.download_album_cover, album_id, str(cover_path))
                if cover_path.exists():
                    yield event.chain_result([
                        Comp.Image(file=str(cover_path))
                    ])
                    # 发完就删，不存着
                    cover_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"封面下载失败: {e}")

        except Exception as e:
            logger.error(f"查询失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败：{e}")

    # ========== /jm 下载并发ZIP ==========
    @filter.command("jm")
    async def jm_download(self, event: AstrMessageEvent):
        """下载漫画并打包ZIP发到QQ，用法：/jm <id> 或 /jm help"""
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
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
                "  /jmw 原作名 [页码] [排序]   按原作搜索\n"
                "━━━━━━━━━━━━━━━\n"
                "📖 详情：\n"
                "  /jmv <漫画id> [选项]       查详情\n"
                "    选项：-author -work -role -tag 去掉对应字段\n"
                "  /jmc <漫画id>              看评论\n"
                "━━━━━━━━━━━━━━━\n"
                "⬇️ 下载：\n"
                "  /jm <漫画id>                下载并发ZIP\n"
                "  /jmpdf <漫画id>             下载并发PDF\n"
                "━━━━━━━━━━━━━━━\n"
                "📊 其他：\n"
                "  /jmrank day/week/month     排行榜\n"
                "  /jmfav                      我的收藏夹\n"
                "  /jmhis                      下载历史\n"
                "    用法：/jmhis clear 清空历史\n"
                "  /jmdomain                   查看可用域名\n"
                "  /jmupdate                   查看更新日志\n"
                "  /jmrand [关键词]            随机抽一个\n"
                "━━━━━━━━━━━━━━━\n"
                "📝 排序参数：mr(最新) mv(最多浏览) mp(最多页数) tf(最多收藏) tr(评分最高) md(最多评论)"
            )
            yield event.plain_result(help_text)
            return

        album_id = args[1]

        # 先找downloads里有没有已经下载好的漫画
        existing_album_dirs = list(self.download_base_dir.glob(f"JM{album_id}_*"))
        if existing_album_dirs and existing_album_dirs[0].is_dir():
            album_dir = existing_album_dirs[0]
            yield event.plain_result(f"📦 找到已下载的文件，直接打包发送...")
            zip_name = f"{album_dir.name}.zip"
            zip_path = self.zip_base_dir / zip_name
            await asyncio.to_thread(self._zip_folder, album_dir, zip_path)
            size_mb = zip_path.stat().st_size / 1024 / 1024
            yield event.chain_result([
                Comp.Plain(f"📎 {zip_name}\n"),
                Comp.File(file=str(zip_path), name=zip_name)
            ])
            zip_path.unlink(missing_ok=True)
            return

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

            # 发送完保留downloads里的原图，下次直接打包，不删
            # 删除zips里的临时zip
            zip_path.unlink(missing_ok=True)

            # 写入下载历史
            user_id = event.get_sender_id()
            total_count = self._add_user_history(user_id, {
                "id": album_id,
                "title": album.name,
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "size_mb": round(size_mb, 1)
            })

            # 调侃吐槽
            import random
            teases = []
            if total_count == 3:
                teases = ["兄弟，今天已经下3本了，悠着点"]
            elif total_count == 5:
                teases = ["🚨 5本了！再看下去要樯橹灰飞烟灭了", "🚨 提醒：5本了，身体要紧啊兄弟"]
            elif total_count == 7:
                teases = ["差不多得了，出去看看太阳吧，别一直盯着屏幕"]
            elif total_count == 10:
                teases = ["🚨 警告：10本！你这频率比我上班打卡都勤", "🚨 10本了！再看下去腰都要坐坏了"]
            elif total_count == 15:
                teases = ["你这下载量，网站管理员都要注意到你了"]
            elif total_count == 20:
                teases = ["🚨 严重警告：20本！JM官方都要给你发终身VIP了", "🚨 20本了！你这是准备把整个网站搬回家？"]
            elif total_count == 30:
                teases = ["你这用量，键盘都要被你磨包浆了"]
            elif total_count == 50:
                teases = ["🚨 50本了兄弟！建议今天到此为止，明天再看"]
            # 加上用户自定义语录
            teases.extend(self.custom_teases)
            if self.tease_enabled and teases:
                yield event.plain_result(random.choice(teases))

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

            items = list(page.iter_id_title_tag())
            if not items:
                yield event.plain_result("没抽到，再试一次")
                return

            album_id, title, tags = random.choice(items)
            safe_tags = self._sanitize_tags(tags[:5])

            # 查一下详情，拿章节数和页数
            album = await asyncio.to_thread(client.get_album_detail, album_id)
            chapters = list(album) if hasattr(album, '__iter__') else []
            page_count = getattr(album, 'page_count', '?')

            tag_str = f"\n🏷️ {', '.join(safe_tags)}" if safe_tags else ""
            yield event.plain_result(
                f"🎲 抽到了！\n\n"
                f"📖 {title}\n"
                f"🆔 JM{album_id}"
                f"{tag_str}\n"
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
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
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

    # ========== /jmw 按原作搜索 ==========
    @filter.command("jmw")
    async def jm_search_work(self, event: AstrMessageEvent):
        """按原作搜索，用法：/jmw <原作名> [页码] [排序]"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmw <原作名>")
            return

        keyword, qq_page, order_by = self._parse_search_args(args)
        client = self.option.new_jm_client()

        async for result in self._render_search_page(
            event, client, client.search_work, keyword, qq_page, order_by, "原作搜索"
        ):
            yield result

    # ========== /jmpdf 下载并发PDF ==========
    @filter.command("jmpdf")
    async def jm_download_pdf(self, event: AstrMessageEvent):
        """下载漫画并发PDF，用法：/jmpdf <id>"""
        if not self._check_permission(event):
            yield event.plain_result("本插件已关闭群聊使用，请私聊使用")
            return
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmpdf <漫画id>")
            return

        album_id = args[1]

        # 先找downloads里有没有已经下载好的漫画
        existing_album_dirs = list(self.download_base_dir.glob(f"JM{album_id}_*"))
        if existing_album_dirs and existing_album_dirs[0].is_dir():
            album_dir = existing_album_dirs[0]
            image_files = sorted([f for f in album_dir.rglob("*.jpg")] + [f for f in album_dir.rglob("*.png")])
            if image_files:
                yield event.plain_result(f"📄 找到已下载的文件，直接生成PDF...")
                from PIL import Image
                images = [Image.open(img_path).convert('RGB') for img_path in image_files]
                pdf_name = f"{album_dir.name}.pdf"
                pdf_path = self.zip_base_dir / pdf_name
                images[0].save(pdf_path, save_all=True, append_images=images[1:], resolution=100.0)
                size_mb = pdf_path.stat().st_size / 1024 / 1024
                yield event.chain_result([
                    Comp.Plain(f"📎 {pdf_name}\n"),
                    Comp.File(file=str(pdf_path), name=pdf_name)
                ])
                pdf_path.unlink(missing_ok=True)
                return

        yield event.plain_result(f"⬇️ 开始下载 JM{album_id} 并生成PDF ...")

        try:
            # 先下载
            result = await jmcomic.download_album_async(album_id, self.option)
            album = result.detail
            album_dir = Path(album.save_path)

            # 找所有图片，按顺序排序
            image_files = sorted([f for f in album_dir.rglob("*.jpg")] + [f for f in album_dir.rglob("*.png")])
            if not image_files:
                yield event.plain_result("⚠️ 下载完成但找不到图片")
                return

            yield event.plain_result(f"📄 正在生成PDF（共{len(image_files)}页）...")

            # 生成PDF
            from PIL import Image
            images = []
            for img_path in image_files:
                img = Image.open(img_path).convert('RGB')
                images.append(img)

            pdf_name = f"JM{album_id}_{album.name[:30]}.pdf"
            for ch in r'\/:*?"<>|':
                pdf_name = pdf_name.replace(ch, '_')
            pdf_path = self.zip_base_dir / pdf_name

            # 保存PDF
            if images:
                images[0].save(
                    pdf_path,
                    save_all=True,
                    append_images=images[1:],
                    resolution=100.0
                )

            size_mb = pdf_path.stat().st_size / 1024 / 1024

            yield event.plain_result(
                f"✅ PDF生成完成！大小 {size_mb:.1f}MB\n"
                f"正在发送..."
            )
            yield event.chain_result([
                Comp.Plain(f"📎 {pdf_name}\n"),
                Comp.File(file=str(pdf_path), name=pdf_name)
            ])

            # 保留downloads里的原图，下次直接用，删除临时PDF
            pdf_path.unlink(missing_ok=True)

            # 写入下载历史
            user_id = event.get_sender_id()
            total_count = self._add_user_history(user_id, {
                "id": album_id,
                "title": album.name,
                "time": time.strftime("%Y-%m-%d %H:%M"),
                "size_mb": round(size_mb, 1)
            })

            # 调侃吐槽
            import random
            teases = []
            if total_count == 3:
                teases = ["兄弟，今天已经下3本了，悠着点"]
            elif total_count == 5:
                teases = ["🚨 5本了！再看下去要樯橹灰飞烟灭了", "🚨 提醒：5本了，身体要紧啊兄弟"]
            elif total_count == 7:
                teases = ["差不多得了，出去看看太阳吧，别一直盯着屏幕"]
            elif total_count == 10:
                teases = ["🚨 警告：10本！你这频率比我上班打卡都勤", "🚨 10本了！再看下去腰都要坐坏了"]
            elif total_count == 15:
                teases = ["你这下载量，网站管理员都要注意到你了"]
            elif total_count == 20:
                teases = ["🚨 严重警告：20本！JM官方都要给你发终身VIP了", "🚨 20本了！你这是准备把整个网站搬回家？"]
            elif total_count == 30:
                teases = ["你这用量，键盘都要被你磨包浆了"]
            elif total_count == 50:
                teases = ["🚨 50本了兄弟！建议今天到此为止，明天再看"]
            # 加上用户自定义语录
            teases.extend(self.custom_teases)
            if self.tease_enabled and teases:
                yield event.plain_result(random.choice(teases))

        except Exception as e:
            logger.error(f"PDF下载失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 失败：{e}")

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

            PER_QQ_PAGE = self.per_page
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
                yield event.plain_result("没有更多了")
                return

            results = []
            for i, (aid, title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                if self.convert_title:
                    title = self._to_simplified(title)
                if self.convert_tags:
                    tags = [self._to_simplified(t) for t in tags]
                safe_tags = self._sanitize_tags(tags[:3])
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {title}{tag_str}")

            reply = f"🏆 {rank_name}（第{qq_page}页）\n\n" + "\n".join(results)
            reply += f"\n\n下一页：/jmrank {rank_type} {qq_page + 1}"
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"排行榜获取失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 失败：{e}")

    # ========== /jmhis 下载历史 ==========
    @filter.command("jmhis")
    async def jm_history(self, event: AstrMessageEvent):
        """查看下载历史，用法：/jmhis 或 /jmhis clear 清空历史"""
        args = event.message_str.split()
        user_id = event.get_sender_id()

        if len(args) >= 2 and args[1].lower() == "clear":
            data = self._load_history()
            if user_id in data:
                del data[user_id]
            self._save_history(data)
            yield event.plain_result("🧹 你的下载历史已清空")
            return

        history = self._get_user_history(user_id)
        if not history:
            yield event.plain_result("还没有下载记录")
            return

        results = []
        for i, item in enumerate(history[:20]):
            results.append(f"{i+1}. [JM{item['id']}] {item['title'][:30]}  ({item['size_mb']}MB)  {item['time']}")

        reply = "📜 下载历史（最近20条）：\n\n" + "\n".join(results)
        reply += "\n\n/jmhis clear 清空历史"
        yield event.plain_result(reply)

    # ========== /jmc 评论查看 ==========
    @filter.command("jmc")
    async def jm_comments(self, event: AstrMessageEvent):
        """查看评论，用法：/jmc <id>"""
        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result("用法：/jmc <漫画id>")
            return

        album_id = args[1]
        yield event.plain_result(f"💬 获取 JM{album_id} 的评论...")

        try:
            client = self.option.new_jm_client()
            page = await asyncio.to_thread(client.album_pagination, album_id, page=1)

            comments = list(page)
            if not comments:
                yield event.plain_result("还没有评论")
                return

            results = []
            for i, comment in enumerate(comments[:self.comment_count]):
                nickname = getattr(comment, 'nickname', getattr(comment, 'username', '匿名'))
                content = getattr(comment, 'content', '')
                # 去掉换行，太长截断，敏感词转拼音
                content = content.replace('\n', ' ').strip()[:100]
                if self.convert_comment:
                    content = self._to_simplified(content)
                content = self._sanitize_tags([content])[0] if content else ''
                results.append(f"{i+1}. {nickname}：{content}")

            reply = f"💬 JM{album_id} 评论（前10条）：\n\n" + "\n".join(results)
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"获取评论失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 获取评论失败：{e}")

    # ========== /jmdomain 查看可用域名 ==========
    @filter.command("jmdomain")
    async def jm_domain(self, event: AstrMessageEvent):
        """查看最新可用镜像站域名"""
        yield event.plain_result("🌐 正在获取最新可用域名...")
        try:
            client = self.option.new_jm_client()
            domains = await asyncio.to_thread(client.get_html_domain_all_via_github)
            reply = "🌐 JM 最新可用域名列表：\n\n" + "\n".join(domains[:10])
            reply += "\n\n如果当前域名连不上，可以在插件配置里换成上面的域名"
            yield event.plain_result(reply)
        except Exception as e:
            logger.error(f"获取域名失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 获取失败：{e}")

    # ========== /jmupdate 查看更新日志 ==========
    @filter.command("jmupdate")
    async def jm_changelog(self, event: AstrMessageEvent):
        """查看插件更新日志"""
        changelog_path = Path(__file__).parent / "CHANGELOG.md"
        if not changelog_path.exists():
            yield event.plain_result("❌ 更新日志文件不存在")
            return
        content = changelog_path.read_text(encoding="utf-8")
        # 只发最近两个版本
        parts = content.split("---\n\n## ")
        reply = parts[0]
        yield event.plain_result(f"📝 插件更新日志：\n\n{reply[:1500]}")

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

            # /jmfav → 显示收藏的漫画列表
            yield event.plain_result("⭐ 获取我的收藏夹...")
            page = await asyncio.to_thread(client.favorite_folder, 1, username=username)
            items = list(page.iter_id_title_tag())

            if not items:
                yield event.plain_result("你还没有收藏任何漫画")
                return

            results = []
            for i, (aid, title, tags) in enumerate(items[:20]):
                if self.convert_title:
                    title = self._to_simplified(title)
                if self.convert_tags:
                    tags = [self._to_simplified(t) for t in tags]
                safe_tags = self._sanitize_tags(tags[:3])
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{i+1}. [JM{aid}] {title}{tag_str}")

            reply = f"⭐ 我的收藏（共{len(items)}本，显示前20本）：\n\n" + "\n".join(results)
            reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"收藏夹操作失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 失败：{e}")

    async def terminate(self):
        pass
