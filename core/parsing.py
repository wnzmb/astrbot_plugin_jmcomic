"""纯解析工具：消息 ID、正整数页码。"""

import re


def parse_positive_page(token, label="页码"):
    """严格解析正整数页码，0/负数/字母/符号都会抛出中文错误。"""
    if not re.fullmatch(r"\d+", str(token).strip()):
        raise ValueError(f"{label}必须是大于 0 的整数")
    value = int(token)
    if value < 1:
        raise ValueError(f"{label}必须是大于 0 的整数")
    return value


def extract_id(message_str):
    """从消息字符串里提取第一个连续数字，作为漫画ID"""
    match = re.search(r'\d+', message_str)
    return match.group() if match else None


def format_index_ranges(indexes):
    """把 [1,2,3,5,7,8] 压缩成 1-3,5,7-8"""
    values = sorted(set(int(x) for x in indexes))
    if not values:
        return ""
    parts = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
        else:
            parts.append(str(start) if start == previous else f"{start}-{previous}")
            start = previous = value
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def format_index_token(indexes):
    """把话序号格式化为展示用 31-35 / 31,35,40（不补零）。"""
    return format_index_ranges(indexes) or "0"
