"""下载选择：参数解析、ID 识别、-c 话/页段解析、页段展开与分册规划。"""

import asyncio
import re

from jmcomic.jm_exception import MissingAlbumPhotoException

from ..core.models import PageUnit
from ..core.parsing import extract_id, format_index_ranges


class SelectionService:
    def __init__(self, settings, cache, state):
        self.settings = settings
        self.cache = cache
        self.state = state

    # ========== 参数 / ID ==========
    @staticmethod
    def parse_download_args(args):
        """解析 /jm <目标> [-c 话] [-latest N]；PDF/ZIP 合并参数由后台配置决定"""
        target = args[1] if len(args) > 1 else ""
        chapter_expr = None
        latest_count = None
        index = 2
        while index < len(args):
            token = args[index]
            if token in ("-c", "--chapters"):
                if index + 1 >= len(args):
                    raise ValueError("-c 后面需要填写话或页段，例如 -c 31-40 或 -c 5:201-400")
                chapter_expr = args[index + 1]
                index += 2
            elif token in ("-latest", "--latest"):
                if index + 1 >= len(args):
                    raise ValueError("-latest 后面需要填写话数，例如 -latest 5")
                raw = str(args[index + 1]).strip()
                if not raw.isdigit() or int(raw) <= 0:
                    raise ValueError("-latest 需要大于 0 的整数，例如 -latest 5")
                latest_count = int(raw)
                index += 2
            elif token.startswith("-"):
                raise ValueError(f"无法识别的选项：{token}")
            else:
                raise ValueError(f"无法识别的参数：{token}")
        if latest_count is not None:
            if chapter_expr:
                raise ValueError("-c 和 -latest 不能同时使用")
            chapter_expr = f"latest:{latest_count}"
        return target, chapter_expr

    @staticmethod
    def normalize_target(token):
        """支持 123 / JM123 / a123(漫画) / p123(章节)"""
        token = str(token).strip()
        prefix = ""
        # 负数/正号不是合法ID，不能从 "-1" 里提取出 "1"
        if token.startswith(("-", "+")):
            return prefix, token
        if len(token) > 1 and token[0].lower() in ("a", "p") and token[1:].isdigit():
            prefix = token[0].lower()
            token = token[1:]
        target_id = extract_id(token) or token
        return prefix, str(target_id)

    async def resolve_target(self, client, token):
        """识别输入是漫画ID还是章节ID，并返回章节列表。"""
        prefix, target_id = self.normalize_target(token)
        if not target_id.isdigit():
            raise ValueError(f"无效ID：{token}")

        cache_key = f"{prefix}:{target_id}" if prefix else target_id
        kind = self.state.id_type_cache.get(cache_key)
        album_probe = None
        photo_probe = None

        if kind is None:
            if prefix == "a":
                kind = "album"
            elif prefix == "p":
                kind = "photo"
            else:
                # 先按章节探测：章节所属漫画ID与输入不同，说明输入是章节。
                # 首章ID可能与漫画ID相同，这种情况仍按漫画处理。
                try:
                    photo_probe = await asyncio.to_thread(client.get_photo_detail, target_id)
                    parent_id = str(getattr(photo_probe, "album_id", ""))
                    if parent_id and parent_id != target_id:
                        kind = "photo"
                    else:
                        album_probe = await asyncio.to_thread(client.get_album_detail, target_id)
                        kind = "album"
                except MissingAlbumPhotoException:
                    album_probe = await asyncio.to_thread(client.get_album_detail, target_id)
                    kind = "album"
            self.state.id_type_cache[cache_key] = kind

        if kind == "album":
            album = album_probe or await asyncio.to_thread(client.get_album_detail, target_id)
            chapters = list(album) if hasattr(album, "__iter__") else []
            return {
                "kind": "album",
                "id": str(target_id),
                "album": album,
                "photo": None,
                "chapters": chapters,
            }

        photo = photo_probe or await asyncio.to_thread(client.get_photo_detail, target_id)
        album = getattr(photo, "from_album", None)
        if album is None:
            parent_id = str(getattr(photo, "album_id", "") or "")
            if parent_id and parent_id != target_id:
                try:
                    album = await asyncio.to_thread(client.get_album_detail, parent_id)
                except Exception:
                    album = None
        return {
            "kind": "photo",
            "id": str(target_id),
            "album": album,
            "photo": photo,
            "chapters": [photo],
        }

    # ========== 页段 ==========
    @staticmethod
    def parse_page_range(part, notes):
        """解析页段：201-400 / 201- / 201，返回 (start, end|None)"""
        raw = str(part).strip()
        if not raw:
            notes.append("页码范围为空，已忽略")
            return None
        if "-" in raw:
            left, right = raw.split("-", 1)
            left, right = left.strip(), right.strip()
            if not left.isdigit() or (right and not right.isdigit()):
                notes.append(f"页码 {part} 格式不正确，已忽略")
                return None
            start = int(left)
            end = int(right) if right else None
            if start <= 0 or (end is not None and end <= 0):
                notes.append(f"页码 {part} 必须大于 0，已忽略")
                return None
            if end is not None and start > end:
                notes.append(f"页码 {part} 范围倒置，已按 {end}-{start} 处理")
                start, end = end, start
            return start, end
        if raw.isdigit():
            value = int(raw)
            if value <= 0:
                notes.append(f"页码 {part} 必须大于 0，已忽略")
                return None
            return value, value
        notes.append(f"页码 {part} 格式不正确，已忽略")
        return None

    @staticmethod
    def merge_page_ranges(ranges):
        """只合并重叠或相邻的页段，保留不连续区间。

        入参/返回值为 [(起始页, 结束页或None)]；结束页为 None 表示到本章末。
        """
        values = []
        for item in ranges or []:
            try:
                start = int(item[0])
                end = None if item[1] is None else int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if start <= 0:
                continue
            if end is not None and end < start:
                start, end = end, start
            if start <= 0:
                continue
            values.append((start, end))
        values.sort(key=lambda item: (item[0], item[1] is None))

        merged = []
        for start, end in values:
            if not merged:
                merged.append((start, end))
                continue
            last_start, last_end = merged[-1]
            if last_end is None:
                # 上一段已经覆盖到本章末，后续页段都被包含
                continue
            if start <= last_end + 1:
                if end is None or end > last_end:
                    merged[-1] = (last_start, end)
            else:
                merged.append((start, end))
        return merged

    # ========== 话选择 ==========
    def select_chapters(self, chapters, expr):
        """解析 -c：2 / 2-4 / 2,5,7 / 5:201-400 / 5:1-5,5:10-15

        返回 ([(原序号, photo, 页段或None)], 无效项, 提示)
        页段为 (起始页, 结束页或None)，页码从 1 开始；同一章节的
        不连续页段会保留为多条记录，便于分别打包。
        """
        expr = str(expr) if expr is not None else ""
        if expr.lower().startswith("latest:"):
            raw = expr.split(":", 1)[1].strip()
            if not raw.isdigit() or int(raw) <= 0:
                return [], [expr], ["-latest 需要大于 0 的整数"]
            count = min(int(raw), len(chapters))
            if count <= 0:
                return [], [expr], ["漫画没有可用话"]
            expr = f"{len(chapters) - count + 1}-{len(chapters)}"
        all_items = [(i + 1, ch, None) for i, ch in enumerate(chapters)]
        if not expr:
            return all_items, [], []

        indexes = set()
        whole_indexes = set()
        page_ranges = {}
        invalid = []
        notes = []
        for part in re.split(r"[,，]", str(expr)):
            part = part.strip()
            if not part:
                continue
            if ":" in part or "：" in part:
                chapter_part, page_part = re.split(r"[:：]", part, maxsplit=1)
                chapter_part = chapter_part.strip()
                if not chapter_part.isdigit():
                    invalid.append(part)
                    notes.append(f"{part} 页码范围只能用于单个话，例如 5:201-400")
                    continue
                value = int(chapter_part)
                if value <= 0:
                    invalid.append(part)
                    continue
                page_range = self.parse_page_range(page_part, notes)
                if page_range is None:
                    invalid.append(part)
                    continue
                if value in whole_indexes:
                    notes.append(f"第{value}话已选择整话，忽略其页段 {part}")
                    continue
                page_ranges.setdefault(value, []).append(page_range)
                indexes.add(value)
            elif "-" in part:
                left, right = part.split("-", 1)
                if left.strip().isdigit() and right.strip().isdigit():
                    start, end = int(left), int(right)
                    if start <= 0 or end <= 0:
                        invalid.append(part)
                        continue
                    if start > end:
                        notes.append(f"{part} 范围倒置，已按 {end}-{start} 处理")
                        start, end = end, start
                    # 防止恶意超长范围占用内存；正常漫画章节数远小于此值
                    if end - start > 10000:
                        invalid.append(part)
                        notes.append(f"{part} 范围过大，已忽略")
                        continue
                    indexes.update(range(start, end + 1))
                    for value in range(start, end + 1):
                        whole_indexes.add(value)
                        page_ranges.pop(value, None)
                else:
                    invalid.append(part)
                    notes.append(f"{part} 不是有效话格式，已忽略")
            elif part.isdigit():
                value = int(part)
                if value <= 0:
                    invalid.append(part)
                else:
                    indexes.add(value)
                    whole_indexes.add(value)
                    page_ranges.pop(value, None)
            else:
                invalid.append(part)
                notes.append(f"{part} 不是有效章节格式，已忽略")

        selected = []
        for index in sorted(indexes):
            if not 1 <= index <= len(chapters):
                invalid.append(str(index))
                continue
            if index in whole_indexes:
                selected.append((index, chapters[index - 1], None))
                continue
            for page_range in self.merge_page_ranges(page_ranges.get(index, [])):
                selected.append((index, chapters[index - 1], page_range))
        return selected, invalid, notes

    # ========== 页段展开 / 分册 ==========
    def expand_page_units(self, selected, album_dir, page_limit):
        """把选中章节展开成页段。

        完整章节是一个页段；只有单章超过 page_limit 时才切成多个页段。
        """
        units = []
        for index, photo, page_range in selected:
            total = len(self.cache.chapter_image_paths(photo, album_dir))
            if total <= 0:
                units.append(PageUnit(index, photo, 1, 0, 0))
                continue
            start, end = 1, total
            if page_range:
                start = max(1, min(int(page_range[0]), total))
                end = total if page_range[1] is None else min(int(page_range[1]), total)
                if end < start:
                    start, end = end, start
            if page_limit and page_limit > 0:
                cursor = start
                while cursor <= end:
                    chunk_end = min(end, cursor + page_limit - 1)
                    units.append(PageUnit(index, photo, cursor, chunk_end, total))
                    cursor = chunk_end + 1
            else:
                units.append(PageUnit(index, photo, start, end, total))
        return units

    @staticmethod
    def pack_units(units, max_pages=None, max_units=None):
        """按页段贪心装册：页数或份数先到上限就换下一个"""
        groups = []
        current = []
        current_pages = 0
        for unit in units:
            if current:
                over_pages = max_pages is not None and current_pages + unit.pages > max_pages
                over_units = (
                    max_units is not None
                    and len({item.index for item in current}) >= max_units
                )
                if over_pages or over_units:
                    groups.append(current)
                    current = []
                    current_pages = 0
            current.append(unit)
            current_pages += unit.pages
        if current:
            groups.append(current)
        return groups

    def normalize_selected_ranges(self, selected, album_dir):
        """下载完成后按实际图片数夹取页码，返回 (新选择, 提示)"""
        result = []
        notes = []
        for index, photo, page_range in selected:
            if not page_range:
                result.append((index, photo, None))
                continue
            total = len(self.cache.chapter_image_paths(photo, album_dir))
            start, end = page_range
            if total <= 0:
                result.append((index, photo, page_range))
                continue
            if start > total:
                notes.append(f"第{index}话只有{total}页，页码 {start} 超出范围，已忽略该话")
                continue
            if end is not None and end > total:
                notes.append(f"第{index}话只有{total}页，结束页已调整为{total}")
                end = total
            if end is not None and end < start:
                start, end = end, start
            result.append((index, photo, (start, end)))
        return result, notes

    def plan_pdf_groups(self, selected, album_dir=None):
        """规划 PDF：整章优先，仅单章超过页数上限时按页切分"""
        units = self.expand_page_units(selected, album_dir, self.settings.pdf_page_limit)
        return self.pack_units(units, max_pages=self.settings.pdf_page_limit)

    def plan_zip_groups(self, selected, album_dir=None):
        """规划 ZIP：最多 zip_group_size 章，且不超过 zip_page_limit 页"""
        units = self.expand_page_units(selected, album_dir, self.settings.zip_page_limit)
        return self.pack_units(
            units,
            max_pages=self.settings.zip_page_limit or None,
            max_units=self.settings.zip_group_size,
        )

    def resume_expr(self, groups):
        """把剩余分组压缩成 -c 表达式，页段从哪页开始就写哪页

        同一章节的多个不连续页段必须分别输出，不能把中间未下载的页
        合并进来。只有重叠或相邻的页段才会合并。
        """
        info = {}
        order = []
        for group in groups:
            for unit in group:
                if unit.index not in info:
                    info[unit.index] = {"ranges": [], "total": unit.total}
                    order.append(unit.index)
                info[unit.index]["ranges"].append((unit.start, unit.end))

        parts = []
        whole = []

        def flush_whole():
            if whole:
                parts.append(format_index_ranges(whole))
                whole.clear()

        for index in order:
            total = info[index]["total"]
            ranges = self.merge_page_ranges(info[index]["ranges"])
            if any(start <= 1 and end is not None and end >= total
                   for start, end in ranges):
                whole.append(index)
                continue
            flush_whole()
            for start, end in ranges:
                end_text = "" if end is None or end >= total else f"{end:03d}"
                parts.append(f"{index}:{start:03d}-{end_text}")
        flush_whole()
        return ",".join(part for part in parts if part)
