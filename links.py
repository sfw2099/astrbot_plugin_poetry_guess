# -*- coding: utf-8 -*-
"""猜诗句插件 → 诗词底座 / 秋烨枢纽 连接层。

底座（astrbot_plugin_poetry_base）：库检索/校验/随机出题/诗句积累记录。
秋烨（astrbot_plugin_qiuye）：成就解锁/道具增删/插件使用记录。

两者均为懒加载（每次调用时从 star 注册表取实例），任一不可用时优雅降级：
- 底座不可用 → 出题/校验失败并给出安装提示
- 秋烨不可用 → 成就/道具静默跳过（不影响游戏本体）
"""

HUB_NAME = "astrbot_plugin_qiuye"
BASE_NAME = "astrbot_plugin_poetry_base"


def get_hub(context):
    try:
        meta = context.get_registered_star(HUB_NAME)
        inst = getattr(meta, "star_cls", None)
        if inst is not None and getattr(inst, "hub_ready", False):
            return inst
    except Exception:
        pass
    return None


def get_base(context):
    try:
        meta = context.get_registered_star(BASE_NAME)
        inst = getattr(meta, "star_cls", None)
        if inst is not None:
            return inst
    except Exception:
        pass
    return None


# ================= 底座：库 =================

def base_db_ready(context) -> bool:
    b = get_base(context)
    return b is not None and b.ensure_db()


def base_db_path(context):
    b = get_base(context)
    if b is not None:
        try:
            return b.db_path
        except Exception:
            pass
    return None


def base_classic_poems(context):
    b = get_base(context)
    if b is not None:
        return getattr(b, "classic_poems", []) or []
    return []


def base_is_in_library(context, text: str) -> bool:
    b = get_base(context)
    if b is None:
        return False
    try:
        return bool(b.is_in_library(text))
    except Exception:
        return False


def base_check_exact_poetry(context, text: str):
    b = get_base(context)
    if b is None:
        return None
    try:
        return b.check_exact_poetry(text)
    except Exception:
        return None


def base_is_adjacent_pair(context, a: str, b_text: str) -> bool:
    b = get_base(context)
    if b is None:
        return False
    try:
        return bool(b.is_adjacent_pair(a, b_text))
    except Exception:
        return False


def base_get_random_verse(context, *args, **kwargs):
    b = get_base(context)
    if b is None:
        return []
    try:
        return b.get_random_verse(*args, **kwargs)
    except Exception:
        return []


def base_get_random_verse_by_combo(context, *args, **kwargs):
    b = get_base(context)
    if b is None:
        return []
    try:
        return b.get_random_verse_by_combo(*args, **kwargs)
    except Exception:
        return []


def base_search_by_chars_and_len(context, *args, **kwargs):
    b = get_base(context)
    if b is None:
        return []
    try:
        return b.search_by_chars_and_len(*args, **kwargs)
    except Exception:
        return []


def base_search_combo_by_char(context, *args, **kwargs):
    b = get_base(context)
    if b is None:
        return []
    try:
        return b.search_combo_by_char(*args, **kwargs)
    except Exception:
        return []


# ================= 底座：个人诗词数据 =================

def base_record_verse(context, uid: str, text: str, name: str = "") -> int:
    b = get_base(context)
    if b is None:
        return 0
    try:
        return int(b.record_verse(uid, text, name))
    except Exception:
        return 0


def base_get_verses(context, uid: str) -> dict:
    b = get_base(context)
    if b is None:
        return {}
    try:
        return dict(b.get_verses(uid))
    except Exception:
        return {}


def base_has_clause(context, uid: str, clause: str) -> bool:
    b = get_base(context)
    if b is None:
        return False
    try:
        return bool(b.has_clause(uid, clause))
    except Exception:
        return False


def base_inc_stat(context, uid: str, key: str, amount: int = 1, name: str = ""):
    b = get_base(context)
    if b is None:
        return
    try:
        b.inc_stat(uid, key, amount, name)
    except Exception:
        pass


def base_get_stats(context, uid: str) -> dict:
    b = get_base(context)
    if b is None:
        return {}
    try:
        return dict(b.get_stats(uid))
    except Exception:
        return {}


def base_check_verse_conditions(context, uid: str, name: str = "") -> dict:
    b = get_base(context)
    if b is None:
        return {}
    try:
        return dict(b.check_verse_conditions(uid, name))
    except Exception:
        return {}


# ================= 秋烨：成就/道具/使用记录 =================

def hub_unlock_achievement(context, uid: str, ach_id: str, name: str = "") -> bool:
    hub = get_hub(context)
    if hub is None:
        return False
    try:
        return bool(hub.unlock_achievement(uid, ach_id, plugin="astrbot_plugin_poetry_guess", name=name))
    except Exception:
        return False


def hub_set_progress(context, uid: str, ach_id: str, progress: int, name: str = "") -> bool:
    hub = get_hub(context)
    if hub is None:
        return False
    try:
        return bool(hub.set_achievement_progress(uid, ach_id, progress, plugin="astrbot_plugin_poetry_guess", name=name))
    except Exception:
        return False


def hub_get_achievements(context, uid: str) -> dict:
    hub = get_hub(context)
    if hub is None:
        return {}
    try:
        return dict(hub.get_achievements(uid))
    except Exception:
        return {}


def hub_add_item(context, uid: str, item: str, n: int = 1, name: str = "") -> int:
    hub = get_hub(context)
    if hub is None:
        return 0
    try:
        return int(hub.add_item(uid, item, n, name))
    except Exception:
        return 0


def hub_consume_item(context, uid: str, item: str, n: int = 1, name: str = "") -> bool:
    hub = get_hub(context)
    if hub is None:
        return False
    try:
        return bool(hub.consume_item(uid, item, n, name))
    except Exception:
        return False


def hub_item_count(context, uid: str, item: str, name: str = "") -> int:
    hub = get_hub(context)
    if hub is None:
        return 0
    try:
        return int(hub.item_count(uid, item, name))
    except Exception:
        return 0


def hub_get_draw_bonus(context, uid: str, name: str = "") -> int:
    hub = get_hub(context)
    if hub is None:
        return 0
    try:
        return int(hub.get_draw_bonus(uid, name))
    except Exception:
        return 0


def hub_add_draw_bonus(context, uid: str, step: int, name: str = ""):
    hub = get_hub(context)
    if hub is None:
        return
    try:
        hub.add_draw_bonus(uid, step, name)
    except Exception:
        pass


def hub_reset_draw_bonus(context, uid: str, name: str = ""):
    hub = get_hub(context)
    if hub is None:
        return
    try:
        hub.reset_draw_bonus(uid, name)
    except Exception:
        pass


def hub_record_plugin_use(context, uid: str, name: str = ""):
    hub = get_hub(context)
    if hub is None:
        return
    try:
        hub.record_plugin_use(uid, "astrbot_plugin_poetry_guess", name)
    except Exception:
        pass


def hub_register_manifest(context):
    """启动时向秋烨注册本插件成就/道具清单。"""
    from .manifest import ACHIEVEMENTS, ITEMS
    hub = get_hub(context)
    if hub is None:
        return False
    try:
        hub.register_achievements("astrbot_plugin_poetry_guess", ACHIEVEMENTS)
        hub.register_items("astrbot_plugin_poetry_guess", ITEMS)
        return True
    except Exception:
        return False
