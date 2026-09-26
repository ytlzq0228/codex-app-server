"""Stable usernames omit email domains; identities are never merged by prefix."""
import re


def username_prefix(value):
    value = value.strip()
    if '@' in value:
        value = value.split('@', 1)[0].lower()
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,120}', value):
        raise ValueError('用户名仅支持字母、数字、点、下划线、加号和连字符，最长 120 位')
    return value
