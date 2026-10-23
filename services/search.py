"""搜索：参数解析与分页渲染。"""

import asyncio
import math
import re

from jmcomic import JmMagicConstants


class SearchService:
    def __init__(self, settings, client, text, messaging, pagination):
        self.settings = settings
        self.client = client
        self.text = text
        self.messaging = messaging
        self.pagination = pagination

    def parse_search_args(self, args):
        """
        解析搜索参数：关键词 + 可选页码 + 可选排序。
        关键词允许包含空格：只把末尾的页码/排序当作参数，其余片段合并为关键词，
        避免 `jms 纯  爱` 被拆成关键词“纯” + 非法参数“爱”。
        支持格式：
          /jms 关键词
          /jms 多词 关键词
          /jms 关键词 2
          /jms 关键词 mv
          /jms 关键词 2 mv
        返回：(keyword, qq_page, order_by)
        """
        tokens = [str(x) for x in args[1:] if str(x).strip()]
        if not tokens:
            raise ValueError("关键词不能为空")

        qq_page = 1
        order_by = JmMagicConstants.ORDER_BY_LATEST  # 默认最新
        page_set = False
        sort_set = False

        # 从末尾消费页码/排序；至少保留一个片段作为关键词，
        # 这样 `/jms 2` 仍会被当作关键词“2”，而不是页码。
        while len(tokens) > 1:
            last = tokens[-1]
            if not sort_set and last in self.settings.order_map:
                order_by = last
                sort_set = True
                tokens.pop()
                continue
            if not page_set and re.fullmatch(r"[+-]?\d+", last):
                value = int(last)
                if value < 1:
                    raise ValueError("页码必须是大于 0 的整数")
                qq_page = value
                page_set = True
                tokens.pop()
                continue
            break

        keyword = re.sub(r"\s+", " ", " ".join(tokens)).strip()
        if not keyword:
            raise ValueError("关键词不能为空")
        return keyword, qq_page, order_by

    async def render_search_page(self, event, client, search_method, keyword, qq_page, order_by, title_prefix="", command_name="jms"):
        """通用搜索结果渲染：分页 + 切片 + 输出"""
        self.pagination.clear(event)
        PER_QQ_PAGE = self.settings.per_page
        PER_WEB_PAGE = 80
        QQ_PAGES_PER_WEB_PAGE = PER_WEB_PAGE // PER_QQ_PAGE

        web_page = math.ceil(qq_page / QQ_PAGES_PER_WEB_PAGE)
        offset_in_web_page = (qq_page - 1) % QQ_PAGES_PER_WEB_PAGE * PER_QQ_PAGE

        order_name = self.settings.order_map.get(order_by, order_by)
        display_keyword = self.text.safe_text(keyword, self.settings.convert_title)
        no_result_text = f"没有找到与「{display_keyword}」相关的漫画，请换个关键词试试"
        yield self.messaging.plain(event, f"🔍 {title_prefix}{display_keyword}（{order_name}，第{qq_page}页）...")

        try:
            total = None
            totals = []
            if self.settings.dual_search:
                # 搜两次：原词 + 繁体词，合并去重，适配半简半繁标题
                import zhconv
                keyword_traditional = zhconv.convert(keyword, 'zh-tw')
                # 如果原词和繁体词相同，只搜一次
                search_keywords = [keyword] if keyword == keyword_traditional else [keyword, keyword_traditional]
                all_items = []
                for kw in search_keywords:
                    page = await asyncio.to_thread(search_method, kw, web_page, order_by)
                    page_total = getattr(page, 'total', None)
                    if page_total is not None:
                        totals.append(int(page_total))
                    all_items.extend(list(page.iter_id_title_tag()))
                if totals:
                    total = max(totals)
                # 去重
                seen = set()
                unique_items = []
                for item in all_items:
                    if item[0] not in seen:
                        seen.add(item[0])
                        unique_items.append(item)
                all_items = unique_items
            else:
                page = await asyncio.to_thread(search_method, keyword, web_page, order_by)
                total = getattr(page, 'total', None)
                all_items = list(page.iter_id_title_tag())
            start = offset_in_web_page
            end = start + PER_QQ_PAGE
            page_items = all_items[start:end]

            # 上游 API 对超大页码会 clamp 到最后一页，不能只看返回是否为空。
            total_int = None
            if total is not None:
                try:
                    total_int = int(total)
                except (TypeError, ValueError):
                    total_int = None
            if total_int is not None and total_int <= 0:
                self.pagination.clear(event)
                yield self.messaging.plain(event, no_result_text)
                return
            if total_int is not None:
                max_page = math.ceil(total_int / PER_QQ_PAGE)
                if qq_page > max_page:
                    self.pagination.clear(event)
                    yield self.messaging.plain(event,
                        f"第{qq_page}页没有更多结果了（共{total_int}条，最多{max_page}页）"
                    )
                    return

            if not page_items:
                self.pagination.clear(event)
                if total_int is not None:
                    yield self.messaging.plain(event, f"第{qq_page}页没有更多结果了")
                else:
                    yield self.messaging.plain(event, no_result_text)
                return

            results = []
            for i, (aid, item_title, tags) in enumerate(page_items):
                global_index = (qq_page - 1) * PER_QQ_PAGE + i + 1
                item_title = self.text.safe_text(item_title, self.settings.convert_title)
                tags = [self.text.safe_text(t, self.settings.convert_tags) for t in tags]
                safe_tags = tags[:3]
                tag_str = f" [{', '.join(safe_tags)}]" if safe_tags else ""
                results.append(f"{global_index}. [JM{aid}] {item_title}{tag_str}")

            reply = f"{title_prefix}：{display_keyword}（{order_name}）\n"
            global_start = (qq_page - 1) * PER_QQ_PAGE
            range_text = f"第{qq_page}页，第{global_start + 1}-{global_start + len(page_items)}条"
            if total_int is not None:
                range_text += f"，共{total_int}条"
            reply += range_text + "\n\n" + "\n".join(results)
            reply += f"\n\n/jmv <id> 查详情，/jm <id> 下载"

            # 下一页提示：保留当前搜索命令和排序参数。
            # 关键词若会被敏感词转换，就不把原词写进命令，避免命令本身触发违规。
            next_keyword = keyword if self.text.safe_text(keyword, self.settings.convert_title) == keyword else "<关键词>"
            next_cmd = f"/{command_name} {next_keyword} {qq_page + 1}"
            internal_next_cmd = f"/{command_name} {keyword} {qq_page + 1}"
            if order_by != JmMagicConstants.ORDER_BY_LATEST:
                next_cmd += f" {order_by}"
                internal_next_cmd += f" {order_by}"
            reply += f"\n下一页：{next_cmd}"

            max_page = (
                math.ceil(total_int / PER_QQ_PAGE)
                if total_int is not None
                else None
            )
            self.pagination.remember(
                event, internal_next_cmd, qq_page, max_page
            )
            yield self.messaging.plain(event, reply)

        except Exception as e:
            yield self.messaging.plain(event, self.client.handle_exception(e, "搜索"))
