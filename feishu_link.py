#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从"飞书多维表格完整链接"解析出 app_token / table_id / view_id。
用户只需复制浏览器地址栏里的多维表格链接，无需手工分辨 bascn/cli_/tbl/vew。

支持链接形态：
  https://<租户>.feishu.cn/base/<app_token>?table=<table_id>&view=<view_id>
  https://<租户>.feishu.cn/base/<app_token>?tbl=<table_id>            (部分旧分享链接用 tbl)
  https://<租户>.feishu.cn/wiki/<wiki_token>                           (知识库内文档，无法直接用，需先转为多维表格独立链接)
纯 app_token 裸串也能识别（作为兜底）。
"""
import re
from typing import Dict


def parse_feishu_base_url(raw: str) -> Dict[str, str]:
    """解析飞书多维表格链接，返回 {app_token, table_id, view_id}（缺失项为空串）。"""
    s = (raw or "").strip()
    out = {"app_token": "", "table_id": "", "view_id": ""}
    if not s:
        return out

    # 剥离 query 前的 path 部分，定位 /base/{app_token}
    path = s.split("?", 1)[0]
    m = re.search(r"/base/([A-Za-z0-9]+)", path)
    if not m:
        # 用户可能直接贴了裸 app_token
        bare = re.fullmatch(r"[A-Za-z0-9]{10,}", s)
        if bare:
            out["app_token"] = bare.group(0)
        return out
    out["app_token"] = m.group(1)

    # 解析 query 参数（table / tbl / view，顺序无关，兼容带 #hash）
    query = s.split("?", 1)[1] if "?" in s else ""
    query = query.split("#", 1)[0]
    params = dict(re.findall(r"([^&=\s]+)=([^&=\s]*)", query))
    table_id = params.get("table") or params.get("tbl") or params.get("table_id") or ""
    view_id = params.get("view") or params.get("view_id") or ""
    out["table_id"] = table_id
    out["view_id"] = view_id
    return out


if __name__ == "__main__":
    import sys
    test_cases = [
        "https://xxx.feishu.cn/base/BakJbdruzakB5YsmaKAcFK6MnJ2?table=tblK1bt0NLW6M2DO&view=vewABC123",
        "https://xxx.feishu.cn/base/bascn1234567890?tbl=tblXYZ&view=vewQ",
        "https://xxx.feishu.cn/base/BakAbcDef123",
        "BakJbdruzakB5YsmaKAcFK6MnJ2",
        "https://xxx.feishu.cn/wiki/wikcn123",
    ]
    for t in test_cases:
        print(f"{t}\n  -> {parse_feishu_base_url(t)}\n")
