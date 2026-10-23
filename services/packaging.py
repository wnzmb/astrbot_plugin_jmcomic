"""产物打包：PDF 合成与封面、ZIP 打包、文件名/压缩包内目录命名。"""

import io
import os
import zipfile
from pathlib import Path

from astrbot.api import logger

from ..core.parsing import format_index_ranges


class PackagingService:
    def __init__(self, settings, catalog):
        self.settings = settings
        self.catalog = catalog

    # ========== 复用判断 ==========
    @staticmethod
    def output_fresh(output: Path, source_dirs):
        """输出文件存在且不比源图片/元数据旧时直接复用"""
        if not output.exists():
            return False
        try:
            out_mtime = output.stat().st_mtime
            for source in source_dirs:
                path = Path(source)
                files = [path] if path.is_file() else path.rglob("*")
                for item in files:
                    if item.is_file() and item.stat().st_mtime > out_mtime:
                        return False
            return True
        except Exception:
            return False

    # ========== PDF ==========
    @staticmethod
    def find_cover_font(size):
        from PIL import ImageFont
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        candidates = [
            Path(windir) / "Fonts" / "msyh.ttc",
            Path(windir) / "Fonts" / "msyhbd.ttc",
            Path(windir) / "Fonts" / "simhei.ttf",
            Path(windir) / "Fonts" / "simsun.ttc",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        ]
        for font_path in candidates:
            try:
                if font_path.is_file():
                    return ImageFont.truetype(str(font_path), size)
            except Exception:
                continue
        return None

    def make_cover_image(self, text, cover_path):
        """用系统 CJK 字体生成 PDF 首页封面；找不到字体时放弃封面"""
        try:
            from PIL import Image, ImageDraw
            title_font = self.find_cover_font(54)
            body_font = self.find_cover_font(30)
            if title_font is None or body_font is None:
                logger.warning("未找到中文字体，跳过 PDF 标题封面")
                return None

            width, height = 1240, 1754
            margin = 80
            image = Image.new("RGB", (width, height), "white")
            draw = ImageDraw.Draw(image)
            max_width = width - margin * 2

            def wrap_line(line, font):
                if not line:
                    return [""]
                result = []
                current = ""
                for char in line:
                    if draw.textlength(current + char, font=font) <= max_width:
                        current += char
                    else:
                        result.append(current)
                        current = char
                result.append(current)
                return result

            rendered = []
            for line_index, line in enumerate(str(text).splitlines()):
                font = title_font if line_index == 0 else body_font
                for wrapped in wrap_line(line, font):
                    rendered.append((wrapped, font))

            y = margin
            for line, font in rendered:
                if y + font.size > height - margin:
                    break
                draw.text((margin, y), line, font=font, fill=(25, 25, 25))
                y += font.size + (18 if font is title_font else 12)
                if font is title_font and y < height:
                    draw.line((margin, y - 8, width - margin, y - 8), fill=(180, 180, 180), width=2)
                    y += 12

            image.save(cover_path, "JPEG", quality=95)
            return cover_path
        except Exception as e:
            logger.warning(f"生成 PDF 封面失败: {e}")
            return None

    def build_pdf(self, image_paths, pdf_path: Path, source_dirs=None,
                  metadata=None, cover_text=None):
        """把图片按顺序合成一个 PDF。

        优先使用 img2pdf（不把所有图片解码进内存，能显著降低大文件崩溃概率），
        不可用或遇到不支持的图片格式时回退到 reportlab/Pillow。
        cover_text 不为空时，在首页插入一张标题封面（不计入图片页上限）。
        """
        if source_dirs and self.output_fresh(pdf_path, source_dirs):
            return pdf_path
        if not image_paths:
            return None

        cover_path = None
        if cover_text:
            cover_path = pdf_path.with_name(pdf_path.stem + "_cover.jpg")
            if not self.make_cover_image(cover_text, cover_path):
                cover_path = None
        if cover_path is not None:
            image_paths = [cover_path] + list(image_paths)
        metadata = metadata or {}

        try:
            try:
                import img2pdf
            except ImportError:
                img2pdf = None
            if img2pdf is not None:
                try:
                    kwargs = {
                        key: str(metadata[key])
                        for key in ("title", "author", "subject", "keywords")
                        if metadata.get(key)
                    }
                    # 元数据写入失败时不再“无元数据重试”，直接交给
                    # reportlab 分支，优先保证 PDF 标题属性存在。
                    pdf_bytes = img2pdf.convert(
                        [str(path) for path in image_paths], **kwargs
                    )
                    pdf_path.write_bytes(pdf_bytes)
                    return pdf_path
                except Exception as e:
                    logger.warning(f"img2pdf 合成失败，尝试 reportlab: {e}")

            # reportlab 逐页写入，不把所有图片同时解码到内存里
            try:
                from PIL import Image
                from reportlab.pdfgen import canvas
                from reportlab.lib.utils import ImageReader

                pdf_canvas = canvas.Canvas(str(pdf_path))
                for setter, key in (
                    ("setTitle", "title"),
                    ("setAuthor", "author"),
                    ("setSubject", "subject"),
                    ("setKeywords", "keywords"),
                ):
                    try:
                        if metadata.get(key):
                            getattr(pdf_canvas, setter)(str(metadata[key]))
                    except Exception:
                        pass
                wrote_page = False
                for image_path in image_paths:
                    try:
                        with Image.open(image_path) as source:
                            width, height = source.size
                            if source.mode not in ("RGB", "L"):
                                source = source.convert("RGB")
                            buffer = io.BytesIO()
                            source.save(buffer, format="JPEG", quality=95)
                            buffer.seek(0)
                        # 按 100dpi 换算成 PDF 点，避免页面尺寸过大
                        page_width = width * 0.72
                        page_height = height * 0.72
                        pdf_canvas.setPageSize((page_width, page_height))
                        pdf_canvas.drawImage(
                            ImageReader(buffer), 0, 0,
                            width=page_width, height=page_height,
                        )
                        pdf_canvas.showPage()
                        wrote_page = True
                        buffer.close()
                    except Exception:
                        continue
                if wrote_page:
                    pdf_canvas.save()
                    return pdf_path
                pdf_canvas.save()
            except Exception as e:
                logger.warning(f"reportlab 合成失败，回退 Pillow: {e}")

            from PIL import Image

            images = []
            try:
                for image_path in image_paths:
                    try:
                        images.append(Image.open(image_path).convert("RGB"))
                    except Exception:
                        continue
                if not images:
                    return None
                images[0].save(
                    pdf_path,
                    save_all=True,
                    append_images=images[1:],
                    resolution=100.0,
                )
                return pdf_path
            finally:
                for image in images:
                    try:
                        image.close()
                    except Exception:
                        pass
        finally:
            if cover_path is not None:
                try:
                    cover_path.unlink(missing_ok=True)
                except Exception:
                    pass

    # ========== ZIP ==========
    def build_zip(self, entries, zip_path: Path, source_dirs=None, info_text=None):
        """按页段打包 ZIP。

        entries 为 [(图片列表, 压缩包内目录名或 None)]；目录名为 None 时图片放在根目录。
        info_text 会作为根目录下的“漫画信息.txt”写入。
        """
        if source_dirs and self.output_fresh(zip_path, source_dirs):
            return zip_path
        if not any(images for images, _ in entries):
            return None
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for images, arc_dir in entries:
                for image in images:
                    arc_name = f"{arc_dir}/{image.name}" if arc_dir else image.name
                    zipf.write(image, arc_name)
            if info_text:
                zipf.writestr("漫画信息.txt", str(info_text).encode("utf-8"))
        return zip_path

    # ========== 命名 ==========
    def zip_chapter_folder(self, photo, index, folder=None, album_dir=None):
        """ZIP 内话目录名：P{章节id}_第{网站话数}话。"""
        photo_id = str(getattr(photo, "photo_id", index) or index)
        position = self.catalog.photo_position(photo, index, folder, album_dir)
        return f"P{photo_id}_第{position}话"

    def chapter_file_stem(self, photo, index, folder=None, album_id="", album_dir=None):
        """单话文件：JM{漫画id}_P{网站话数:03d}。"""
        album_id = str(
            album_id
            or getattr(photo, "album_id", "")
            or "unknown"
        )
        position = self.catalog.photo_position(photo, index, folder, album_dir)
        return f"JM{album_id}_P{position:03d}"

    def group_file_stem(self, album_id, album_title, indexes):
        """多话合并文件：JM漫画id_P起始-P结束 / P起始,P下一话。

        连续话用 P031-P035；非连续话用 P031,P035,P040，避免把实际
        只含三话的文件标成 P031-P040。
        """
        values = sorted({int(x) for x in indexes})
        return f"JM{album_id}_{self.format_position_token(values)}"

    @staticmethod
    def format_position_token(indexes):
        """把话序号格式化为文件名用 P031-P035 / P031,P035,P040。"""
        parts = []
        for token in format_index_ranges(indexes).split(","):
            if not token:
                continue
            if "-" in token:
                start, end = token.split("-", 1)
                parts.append(f"P{int(start):03d}-P{int(end):03d}")
            else:
                parts.append(f"P{int(token):03d}")
        return ",".join(parts) or "P000"

    def file_stem_for_entries(self, entries, album_id, album_title, album_dir=None):
        """按页段生成文件名；页段后缀用 _p起始-结束，话序号用 P。"""
        if len(entries) == 1:
            unit, folder, _ = entries[0]
            stem = self.chapter_file_stem(
                unit.photo, unit.index, folder, album_id, album_dir
            )
            if unit.partial:
                stem += f"_p{unit.start:03d}-{unit.end:03d}"
            return stem

        indexes = {unit.index for unit, _folder, _images in entries}
        if len(indexes) == 1:
            # 同一话的多个不连续页段仍属于单话，每个页段单独追加 _p起始-结束。
            unit, folder, _ = entries[0]
            stem = self.chapter_file_stem(
                unit.photo, unit.index, folder, album_id, album_dir
            )
            for unit, _folder, _images in entries:
                if unit.partial:
                    stem += f"_p{unit.start:03d}-{unit.end:03d}"
            return stem

        stem = self.group_file_stem(
            album_id, album_title, [unit.index for unit, _folder, _images in entries]
        )
        partials = [unit for unit, _folder, _images in entries if unit.partial]
        if len(partials) == 1:
            unit = partials[0]
            stem += f"_p{unit.start:03d}-{unit.end:03d}"
        else:
            # 手动组合多个页段时逐个标注话序号，避免不同页码组合生成同名文件
            for unit, folder, _images in entries:
                if not unit.partial:
                    continue
                position = self.catalog.photo_position(
                    unit.photo, unit.index, folder, album_dir
                )
                stem += f"_P{position:03d}p{unit.start:03d}-{unit.end:03d}"
        return stem

    def zip_arc_dir(self, unit, folder, multi, album_dir=None):
        """ZIP 内章节目录名；单话也包一层，便于和缓存目录对照。"""
        arc_dir = self.zip_chapter_folder(unit.photo, unit.index, folder, album_dir)
        if unit.partial:
            arc_dir += f"_p{unit.start:03d}-{unit.end:03d}"
        return arc_dir
