#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parse and validate a Feishu/Lark Base URL."""

import re
from typing import Dict
from urllib.parse import parse_qs, unquote, urlparse

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{10,}$")
ALLOWED_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com", ".larkoffice.com")


def _empty() -> Dict[str, str]:
    return {"app_token": "", "table_id": "", "view_id": ""}


def parse_feishu_base_url(raw: str) -> Dict[str, str]:
    """Return app_token/table_id/view_id or raise ValueError for an unsafe URL."""
    value = (raw or "").strip()
    if not value:
        return _empty()
    if TOKEN_PATTERN.fullmatch(value):
        return {"app_token": value, "table_id": "", "view_id": ""}

    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("飞书链接必须是完整的 HTTPS 地址")
    hostname = parsed.hostname.lower()
    if not any(hostname.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES):
        raise ValueError("链接域名不是受支持的飞书/Lark 域名")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if "wiki" in parts:
        raise ValueError("暂不支持知识库 wiki 链接，请打开多维表格后复制 /base/ 链接")
    try:
        base_index = parts.index("base")
        app_token = parts[base_index + 1]
    except (ValueError, IndexError):
        raise ValueError("链接中未找到 /base/{app_token}")
    if not TOKEN_PATTERN.fullmatch(app_token):
        raise ValueError("链接中的多维表格 App Token 格式不正确")

    query = parse_qs(parsed.query, keep_blank_values=True)
    table_id = (query.get("table") or query.get("tbl") or query.get("table_id") or [""])[0].strip()
    view_id = (query.get("view") or query.get("view_id") or [""])[0].strip()
    if table_id and not re.fullmatch(r"[A-Za-z0-9_-]+", table_id):
        raise ValueError("链接中的 Table ID 格式不正确")
    if view_id and not re.fullmatch(r"[A-Za-z0-9_-]+", view_id):
        raise ValueError("链接中的 View ID 格式不正确")
    return {"app_token": app_token, "table_id": table_id, "view_id": view_id}


if __name__ == "__main__":
    examples = [
        "https://example.feishu.cn/base/BakExampleAppToken001?table=tblExampleTable01&view=vewExampleView01",
        "BakExampleAppToken001",
    ]
    for example in examples:
        print(example, "->", parse_feishu_base_url(example))
