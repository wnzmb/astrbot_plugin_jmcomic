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
