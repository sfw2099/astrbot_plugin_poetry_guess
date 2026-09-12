# -*- coding: utf-8 -*-
"""小工具函数。"""


def split_single_clauses(text):
    """将含标点的诗句拆成单个分句（去标点），两句则拆两个单句。"""
    import re
    clauses = re.split(r'[，。！？、；：\s]+', text or "")
    return [re.sub(r'[^\u4e00-\u9fff]', '', c) for c in clauses if re.sub(r'[^\u4e00-\u9fff]', '', c)]
