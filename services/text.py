"""文本处理：繁简转换、敏感词替换、安全文件名。"""

import re


class TextService:
    def __init__(self, settings):
        self.settings = settings

    def to_simplified(self, text):
        """繁体转简体"""
        if not self.settings.convert_enabled or not text:
            return text
        try:
            import zhconv
            return zhconv.convert(text, 'zh-cn')
        except Exception:
            return text

    def to_traditional(self, text):
        """简体转繁体，用于搜索"""
        if not text:
            return text
        try:
            import zhconv
            return zhconv.convert(text, 'zh-tw')
        except Exception:
            return text

    def sanitize_tags(self, tags):
        """敏感词替换成拼音，避免QQ审核"""
        if not self.settings.sensitive_filter:
            return tags
        from pypinyin import lazy_pinyin

        sensitive_map = {}
        # 合并用户自定义敏感词
        sensitive_map.update(self.settings.custom_sensitive_map)
        result = []
        for tag in tags:
            # 先替换已知敏感词；长词优先，避免“性虐待”被“性虐”截断成 xingnve待
            for cn, pinyin in sorted(
                sensitive_map.items(), key=lambda item: len(item[0]), reverse=True
            ):
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

    def safe_text(self, text, simplify=None):
        """统一处理所有对外展示的动态文本：繁简转换 + 敏感词转拼音"""
        if not text:
            return text
        if not isinstance(text, str):
            text = str(text)
        if simplify is None:
            simplify = self.settings.convert_title
        if simplify:
            text = self.to_simplified(text)
        if self.settings.sensitive_filter:
            text = self.sanitize_tags([text])[0]
        return text

    def safe_filename(self, name, max_len=60):
        """生成安全的文件名：敏感词转拼音并去掉非法字符"""
        name = self.safe_text(name) or ''
        # 章节名本身可能带 .zip/.pdf 等后缀，避免生成 .zip.zip / .zip.pdf
        while True:
            stripped = re.sub(
                r"(?i)\.(?:zip|pdf|rar|7z|jpg|jpeg|png|webp|gif|mp4|mkv|epub|txt)$",
                "",
                name.strip(),
            )
            if stripped == name:
                break
            name = stripped
        for ch in '\\/:*?"<>|\n\r\t':
            name = name.replace(ch, '_')
        return name.strip()[:max_len] or 'untitled'
