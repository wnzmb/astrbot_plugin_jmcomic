"""漫画/章节元信息：标题、话序号、页数、信息页文案、畸形章节 album 检测。"""

import asyncio
from datetime import datetime

from ..core.parsing import format_index_token


class CatalogService:
    def __init__(self, settings, cache, text):
        self.settings = settings
        self.cache = cache
        self.text = text

    @staticmethod
    def text_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(item) for item in value if str(item).strip()]

    # ========== 标题 ==========
    def album_display_title(self, album, album_dir=None):
        name = getattr(album, "name", "") if album is not None else ""
        if name:
            return self.text.safe_text(str(name), self.settings.convert_title)
        metadata = self.cache.read_metadata(album_dir)
        return metadata.get("title_display") or metadata.get("title") or ""

    def chapter_display_title(self, photo, index=1, folder=None, album_dir=None):
        name = getattr(photo, "name", "") if photo is not None else ""
        if name:
            return self.text.safe_text(str(name), self.settings.convert_title)

        metadata = self.cache.read_metadata(folder)
        if metadata.get("title_display") or metadata.get("title"):
            return metadata.get("title_display") or metadata.get("title")

        photo_id = str(getattr(photo, "photo_id", "") or "")
        album_metadata = self.cache.read_metadata(album_dir)
        for item in album_metadata.get("chapters", []) or []:
            if photo_id and str(item.get("photo_id", "")) == photo_id:
                return item.get("title_display") or item.get("title") or f"第{index}话"
        return f"第{index}话"

    # ========== 页数 ==========
    @staticmethod
    def photo_page_count(photo):
        """读取章节页数；API 通常提供 page_arr，缺失时回退 page_count/total。"""
        try:
            page_arr = getattr(photo, "page_arr", None)
            if page_arr is not None:
                count = len(page_arr)
                if count > 0:
                    return count
        except Exception:
            pass
        for attr in ("page_count", "total"):
            try:
                value = int(getattr(photo, attr, 0) or 0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        return 0

    def photo_range_pages(self, photo, page_range=None):
        total = self.photo_page_count(photo)
        if total <= 0:
            return 0
        if not page_range:
            return total
        start, end = int(page_range[0]), page_range[1]
        start = max(1, min(start, total))
        end = total if end is None else min(int(end), total)
        if end < start:
            start, end = end, start
        return max(0, end - start + 1)

    def format_chapter_line(self, photo, index, limit=50):
        """统一 jmv/jmlist 的章节行：第n话 [JM章节id] 漫画原标题（N页）。"""
        position = self.photo_position(photo, index)
        title = self.text.safe_text(getattr(photo, "name", ""), self.settings.convert_title)
        title = title[:limit] if title else ""
        pages = self.photo_page_count(photo)
        if title:
            suffix = f"{title}（{pages}页）" if pages else f"{title}（页数未知）"
        else:
            suffix = f"（无标题，{pages}页）" if pages else "（无标题，页数未知）"
        return f"第{position}话 [JM{getattr(photo, 'photo_id', '')}] {suffix}"

    # ========== metadata 落盘 ==========
    def save_target_metadata(self, album, album_id, chapters, album_dir,
                             selected_photos=None):
        """把漫画/章节标题写入缓存目录，供打包和缺标题兜底使用"""
        if album_dir is None:
            return
        all_chapters = list(chapters or [])
        if album is not None:
            try:
                expanded = list(album)
                if expanded:
                    all_chapters = expanded
            except Exception:
                pass

        album_id = str(
            album_id or getattr(album, "album_id", "") or ""
        )
        existing = self.cache.read_metadata(album_dir)
        title = str(
            (getattr(album, "name", "") if album is not None else "")
            or existing.get("title", "")
            or ""
        )
        title_display = (
            self.text.safe_text(title, self.settings.convert_title) if title else ""
        )
        authors = self.text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or existing.get("authors", [])
        )
        tags = self.text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or existing.get("tags", [])
        )
        author_display = [
            self.text.safe_text(item, self.settings.convert_author) for item in authors
        ]
        tag_display = [
            self.text.safe_text(item, self.settings.convert_tags) for item in tags
        ]

        existing_chapters = {
            str(item.get("photo_id", "")): item
            for item in (existing.get("chapters", []) or [])
            if isinstance(item, dict)
        }
        chapter_items = []
        for index, photo in enumerate(all_chapters, 1):
            photo_title = str(getattr(photo, "name", "") or "")
            photo_id = str(getattr(photo, "photo_id", "") or "")
            previous = existing_chapters.get(photo_id, {})
            chapter_title_display = (
                self.text.safe_text(photo_title, self.settings.convert_title)
                if photo_title else previous.get("title_display", "")
            )
            item = {
                "photo_id": photo_id,
                "index": index,
                "title": photo_title or previous.get("title", ""),
                "title_display": chapter_title_display,
            }
            if previous.get("page_count") is not None:
                item["page_count"] = previous["page_count"]
            chapter_items.append(item)

        album_data = {
            "schema": 1,
            "album_id": album_id,
            "title": title,
            "title_display": title_display,
            "authors": authors,
            "authors_display": author_display,
            "tags": tags,
            "tags_display": tag_display,
            "chapters": chapter_items,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 先写入专辑信息，章节标题为空时 chapter_display_title 可从
        # 专辑元数据里回退；写入章节元数据后再补全专辑里的标题。
        self.cache.write_metadata(album_dir, album_data)

        if selected_photos is None:
            selected_photos = all_chapters
        seen = set()
        album_items_changed = False
        for photo in selected_photos:
            photo_id = str(getattr(photo, "photo_id", "") or "")
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            folder = self.cache.chapter_dir(photo, album_dir)
            if folder is None:
                continue
            index = next(
                (
                    item["index"] for item in chapter_items
                    if item.get("photo_id") == photo_id
                ),
                1,
            )
            chapter_title = self.chapter_display_title(
                photo, index, folder, album_dir
            )
            page_arr = getattr(photo, "page_arr", None)
            page_count = len(page_arr) if page_arr else len(self.cache.chapter_images(folder))
            raw_chapter_title = str(getattr(photo, "name", "") or "")
            self.cache.write_metadata(folder, {
                "schema": 1,
                "album_id": album_id,
                "album_title": title,
                "album_title_display": title_display,
                "photo_id": photo_id,
                "index": index,
                "title": raw_chapter_title,
                "title_display": chapter_title,
                "page_count": page_count,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            for item in chapter_items:
                if item.get("photo_id") != photo_id:
                    continue
                if raw_chapter_title:
                    item["title"] = raw_chapter_title
                item["title_display"] = chapter_title
                item["page_count"] = page_count
                album_items_changed = True
                break

        if album_items_changed:
            album_data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.cache.write_metadata(album_dir, album_data)

    # ========== 话序号 ==========
    def known_photo_position(self, photo, folder=None, album_dir=None):
        """只返回可确认的网站话序号；取不到时返回 None。"""
        return self.cache.known_photo_position(photo, folder, album_dir)

    def photo_position(self, photo, fallback=1, folder=None, album_dir=None):
        """取章节在漫画中的网站话序号（1 起），失败时回退到调用方序号。"""
        position = self.known_photo_position(photo, folder, album_dir)
        if position is not None:
            return position
        return max(1, int(fallback or 1))

    def unit_metadata_index(self, unit, folder):
        try:
            index = int(self.cache.read_metadata(folder).get("index") or unit.index)
            return index if index > 0 else unit.index
        except Exception:
            return unit.index

    # ========== 信息页文案 ==========
    def album_info_text(self, album, album_id, album_dir, chapter_entries):
        """生成放进 ZIP 的可读漫画信息文件"""
        metadata = self.cache.read_metadata(album_dir)
        title = self.album_display_title(album, album_dir) or f"JM{album_id}"
        authors = self.text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or metadata.get("authors", [])
        )
        tags = self.text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or metadata.get("tags", [])
        )
        lines = [
            "漫画信息",
            f"标题：{title}",
            f"ID：JM{album_id}",
        ]
        if authors:
            lines.append("作者：" + "、".join(
                self.text.safe_text(item, self.settings.convert_author) for item in authors
            ))
        if tags:
            lines.append("标签：" + "、".join(
                self.text.safe_text(item, self.settings.convert_tags) for item in tags
            ))
        lines.append("")
        lines.append("本次包含：")
        if not chapter_entries:
            lines.append("- 无可用话")
        for unit, folder, _images in chapter_entries:
            chapter_index = self.unit_metadata_index(unit, folder)
            chapter_title = self.chapter_display_title(
                unit.photo, chapter_index, folder, album_dir
            )
            page_suffix = (
                f"（第{unit.start}-{unit.end}页）" if unit.partial else ""
            )
            photo_id = getattr(unit.photo, "photo_id", chapter_index)
            lines.append(
                f"- 第{chapter_index}话 {chapter_title}{page_suffix}  [JM{photo_id}]"
            )
        return "\n".join(lines).rstrip() + "\n"

    def album_cover_text(self, album, album_id, album_dir, chapter_entries):
        """PDF 首页封面用的精简信息，避免章节过多时溢出"""
        metadata = self.cache.read_metadata(album_dir)
        title = self.album_display_title(album, album_dir) or f"JM{album_id}"
        authors = self.text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or metadata.get("authors", [])
        )
        tags = self.text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or metadata.get("tags", [])
        )
        indexes = [
            self.unit_metadata_index(unit, folder)
            for unit, folder, _images in chapter_entries
        ]
        lines = [title, f"JM{album_id}"]
        if authors:
            lines.append("作者：" + "、".join(
                self.text.safe_text(item, self.settings.convert_author) for item in authors
            ))
        if tags:
            lines.append("标签：" + "、".join(
                self.text.safe_text(item, self.settings.convert_tags) for item in tags
            ))
        if indexes:
            index_text = format_index_token(indexes)
            lines.append(f"本次包含：第{index_text}话（共{len(set(indexes))}话）")
            partial_entries = [
                entry for entry in chapter_entries if entry[0].partial
            ]
            if partial_entries:
                lines.append("含页段：" + "、".join(
                    f"第{self.unit_metadata_index(unit, folder)}话 "
                    f"{unit.start}-{unit.end}页"
                    for unit, folder, _images in partial_entries[:6]
                ))
        return "\n".join(lines)

    def pdf_metadata(self, album, album_id, album_dir, chapter_entries):
        metadata = self.cache.read_metadata(album_dir)
        title = self.album_display_title(album, album_dir) or f"JM{album_id}"
        authors = self.text_list(
            (getattr(album, "authors", None) if album is not None else None)
            or metadata.get("authors", [])
        )
        tags = self.text_list(
            (getattr(album, "tags", None) if album is not None else None)
            or metadata.get("tags", [])
        )
        indexes = [
            self.unit_metadata_index(unit, folder)
            for unit, folder, _images in chapter_entries
        ]
        result = {
            "title": title,
            "subject": f"JM{album_id}"
            + (f" 第{format_index_token(indexes)}话" if indexes else ""),
        }
        if authors:
            result["author"] = "、".join(
                self.text.safe_text(item, self.settings.convert_author) for item in authors
            )
        if tags:
            result["keywords"] = "、".join(
                self.text.safe_text(item, self.settings.convert_tags) for item in tags
            )
        return result

    # ========== 章节 ID 误当漫画 ID 的畸形数据 ==========
    @staticmethod
    def looks_like_chapter_album(album) -> bool:
        """识别上游把章节 ID 当漫画 ID 返回的畸形 album 对象。

        API 对章节 ID 会返回 name=zip 文件名、authors/tags=['filename']
        且章节列表为父漫画的畸形数据；部分章节 ID（如 1236387）的
        authors/tags 为空，但首项 photo_id 会变成父漫画 ID。两种特征
        都用于提前给出可操作提示，避免 jmv/jmlist 展示错乱字段。
        """
        if album is None:
            return False
        authors = getattr(album, "authors", []) or []
        tags = getattr(album, "tags", []) or []
        if isinstance(authors, str):
            authors = [authors]
        if isinstance(tags, str):
            tags = [tags]
        authors = [str(x).strip().lower() for x in authors]
        tags = [str(x).strip().lower() for x in tags]
        if (authors and all(x == "filename" for x in authors)) or (
            tags and all(x == "filename" for x in tags)
        ):
            return True

        album_id = str(getattr(album, "album_id", "") or "").strip()
        try:
            first = next(iter(album))
        except (TypeError, StopIteration):
            return False
        first_id = str(getattr(first, "photo_id", "") or "").strip()
        return bool(album_id and first_id and first_id != album_id)

    @staticmethod
    def chapter_album_parent_id(album, album_id) -> str:
        """从畸形章节 album 的首项提取父漫画ID。

        API 返回的畸形章节 album 里，第一项通常是父漫画的占位章节，
        其 photo_id 就是父漫画ID（例如 275013 的首项 photo_id=43164）。
        """
        try:
            first = next(iter(album))
        except (TypeError, StopIteration):
            return ""
        candidate = str(getattr(first, "photo_id", "") or "").strip()
        if candidate.isdigit() and candidate != str(album_id):
            return candidate
        return ""

    async def resolve_chapter_album_parent(self, client, album, album_id) -> str:
        """优先从 album 首项取父漫画ID，失败时用 photo 详情兜底"""
        parent_id = self.chapter_album_parent_id(album, album_id)
        if parent_id:
            return parent_id
        try:
            photo = await asyncio.to_thread(client.get_photo_detail, album_id)
        except Exception:
            return ""
        parent_id = str(getattr(photo, "album_id", "") or "").strip()
        if parent_id.isdigit() and parent_id != str(album_id):
            return parent_id
        return ""
