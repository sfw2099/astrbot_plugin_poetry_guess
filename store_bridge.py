# -*- coding: utf-8 -*-
"""原 PlayerManager 的行为桥接层。

数据去向：
- verses / 诗词stats（guess_games/duel_games/total_guesses 等）→ 诗词底座
- 成就（含 closer/duel_streak/hazard_tier/pig/beloved_verse 等升级制）→ 秋烨
- 道具背包 / 抽道具保底 → 秋烨
- last_duel_lost_to（复仇者）→ 秋烨用户档案的扩展字段（经 hub.get_user 读写）

对外接口与原 PlayerManager 保持同名同语义，main.py 迁移成本最低。
"""

import time

from . import links
from .manifest import closer_level_name, duel_streak_name, hazard_tier_name


class StoreBridge:
    """兼容原 self.pm 调用面。构造时需传入 context。"""

    def __init__(self, context):
        self.context = context

    # ---------- 名字 ----------

    def load(self, uid, name=""):
        """兼容旧代码 self.pm.load(uid).get("name"/"stats")。
        返回一个轻量视图 dict：{"name", "stats"}。"""
        stats = links.base_get_stats(self.context, uid)
        hub = links.get_hub(self.context)
        extra = {}
        if hub is not None:
            try:
                u = hub.get_user(str(uid), name)
                extra = {
                    "last_duel_lost_to": u.get("last_duel_lost_to"),
                }
            except Exception:
                pass
        view = {
            "name": self._name_of(uid, name),
            "stats": dict(stats),
        }
        view["stats"].update({k: v for k, v in extra.items() if v is not None})
        return view

    def _name_of(self, uid, name=""):
        if name:
            return name
        hub = links.get_hub(self.context)
        if hub is not None:
            try:
                return hub.get_user(str(uid)).get("name") or f"用户{uid}"
            except Exception:
                pass
        return f"用户{uid}"

    def save(self, uid):
        """旧代码里 save 前会直接改 view dict；新架构写入走专门方法，这里兼容为空。"""
        pass

    def _uid_name(self, uid):
        return self._name_of(uid)

    # ---------- 诗句积累（底座） ----------

    def record_verse(self, uid, text, name=""):
        return links.base_record_verse(self.context, uid, text, name)

    def get_verses(self, uid):
        return links.base_get_verses(self.context, uid)

    def inc_stat(self, uid, key, amount=1, name=""):
        links.base_inc_stat(self.context, uid, key, amount, name)

    # ---------- 成就（秋烨） ----------

    def unlock_achievement(self, uid, ach_id, name=""):
        return links.hub_unlock_achievement(self.context, uid, ach_id, name)

    def get_achievements(self, uid):
        return links.hub_get_achievements(self.context, uid)

    def check_verse_achievements(self, uid, name=""):
        """个人积累类成就：条件由底座判定，解锁由秋烨执行。返回新解锁 id 列表。"""
        conds = links.base_check_verse_conditions(self.context, uid, name)
        new = []
        for ach_id, ok in conds.items():
            if ok and links.hub_unlock_achievement(self.context, uid, ach_id, name):
                new.append(ach_id)
        return new

    def add_pig(self, uid, name=""):
        """🐖 重复诗句计数 +1（秋烨 progress 累加）。返回最新累计数。"""
        hub = links.get_hub(self.context)
        if hub is None:
            return 0
        try:
            u = hub.get_user(str(uid), name)
            achs = u.setdefault("achievements", {})
            cur = achs.get("pig", {})
            count = int(cur.get("progress", 0)) + 1
            achs["pig"] = {
                "unlocked": True,
                "time": cur.get("time", time.time()),
                "progress": count,
                "plugin": "astrbot_plugin_poetry_guess",
            }
            hub.store.save_user(str(uid), u)
            return count
        except Exception:
            return 0

    def collect_season(self, uid, season_bit, name=""):
        """斯蒂芬·金：季节位掩码存底座 stats.seasons，集齐四季解锁秋烨成就。"""
        stats = links.base_get_stats(self.context, uid)
        cur = int(stats.get("seasons", 0))
        new = cur | int(season_bit)
        links.base_inc_stat(self.context, uid, "seasons", new - cur)
        if new == 15:
            return links.hub_unlock_achievement(self.context, uid, "king_stephen", name)
        return False

    def check_closer(self, uid, closer_count, name=""):
        """收尾人升级制：progress 存秋烨 achievements.closer。"""
        hub = links.get_hub(self.context)
        if hub is None:
            return None
        try:
            u = hub.get_user(str(uid), name)
            achs = u.setdefault("achievements", {})
            cur = achs.get("closer", {})
            old_lv = closer_level_name(cur.get("progress", 0))
            achs["closer"] = {
                "unlocked": True,
                "time": cur.get("time", time.time()),
                "progress": int(closer_count),
                "plugin": "astrbot_plugin_poetry_guess",
            }
            hub.store.save_user(str(uid), u)
            new_lv = closer_level_name(int(closer_count))
            return new_lv if new_lv != old_lv else None
        except Exception:
            return None

    def check_duel_streak(self, uid, streak, name=""):
        hub = links.get_hub(self.context)
        if hub is None:
            return None
        try:
            u = hub.get_user(str(uid), name)
            achs = u.setdefault("achievements", {})
            cur = achs.get("duel_streak", {})
            old_max = int(cur.get("progress", 0))
            new_max = max(old_max, int(streak))
            if new_max <= 0:
                return None
            achs["duel_streak"] = {
                "unlocked": True,
                "time": cur.get("time", time.time()),
                "progress": new_max,
                "plugin": "astrbot_plugin_poetry_guess",
            }
            hub.store.save_user(str(uid), u)
            new_lv = duel_streak_name(new_max)
            return new_lv if new_lv != duel_streak_name(old_max) else None
        except Exception:
            return None

    def check_hazard_tier(self, uid, tier, name=""):
        hub = links.get_hub(self.context)
        if hub is None:
            return None
        try:
            u = hub.get_user(str(uid), name)
            achs = u.setdefault("achievements", {})
            cur = achs.get("hazard_tier", {})
            old_max = int(cur.get("progress", 0))
            new_max = max(old_max, int(tier))
            if new_max <= 0:
                return None
            achs["hazard_tier"] = {
                "unlocked": True,
                "time": cur.get("time", time.time()),
                "progress": new_max,
                "plugin": "astrbot_plugin_poetry_guess",
            }
            hub.store.save_user(str(uid), u)
            new_lv = hazard_tier_name(new_max)
            return new_lv if new_lv != hazard_tier_name(old_max) else None
        except Exception:
            return None

    def check_beloved_verse(self, uid, name=""):
        """挚爱诗句：最高频诗句由底座 verses 判定，progress/verse 存秋烨。"""
        hub = links.get_hub(self.context)
        verses = links.base_get_verses(self.context, uid)
        if hub is None or not verses:
            return None
        try:
            top_verse = max(verses, key=lambda v: verses[v].get("count", 0))
            top_count = verses[top_verse].get("count", 0)
            if top_count < 50:
                return None
            u = hub.get_user(str(uid), name)
            achs = u.setdefault("achievements", {})
            cur = achs.get("beloved_verse", {})
            if cur.get("unlocked") and cur.get("verse") == top_verse and cur.get("progress") == top_count:
                return None
            achs["beloved_verse"] = {
                "unlocked": True,
                "time": cur.get("time", time.time()),
                "progress": top_count,
                "verse": top_verse,
                "plugin": "astrbot_plugin_poetry_guess",
            }
            hub.store.save_user(str(uid), u)
            return (top_verse, top_count)
        except Exception:
            return None

    # ---------- 复仇者（last_duel_lost_to 存秋烨档案） ----------

    def _get_last_lost_to(self, uid):
        hub = links.get_hub(self.context)
        if hub is None:
            return None
        try:
            return hub.get_user(str(uid)).get("last_duel_lost_to")
        except Exception:
            return None

    def _set_last_lost_to(self, uid, winner_uid, name=""):
        hub = links.get_hub(self.context)
        if hub is None:
            return
        try:
            u = hub.get_user(str(uid), name)
            u["last_duel_lost_to"] = str(winner_uid)
            hub.store.save_user(str(uid), u)
        except Exception:
            pass

    # ---------- 道具（秋烨） ----------

    def add_item(self, uid, item, n=1, name=""):
        return links.hub_add_item(self.context, uid, item, n, name)

    def get_items(self, uid, name=""):
        hub = links.get_hub(self.context)
        if hub is None:
            return {}
        try:
            return dict(hub.get_items(uid, name))
        except Exception:
            return {}

    def item_count(self, uid, item, name=""):
        return links.hub_item_count(self.context, uid, item, name)

    def consume_item(self, uid, item, n=1, name=""):
        return links.hub_consume_item(self.context, uid, item, n, name)

    def take_random_item(self, from_uid, to_uid, name_from="", name_to=""):
        """探囊取物。"""
        import random as _r
        inv = self.get_items(from_uid, name_from)
        owned = [k for k, v in inv.items() if v and int(v) > 0]
        if not owned:
            return None
        item = _r.choice(owned)
        self.consume_item(from_uid, item, 1, name_from)
        self.add_item(to_uid, item, 1, name_to)
        return item

    # ---------- 抽道具保底（秋烨） ----------

    def get_draw_bonus(self, uid, name=""):
        return links.hub_get_draw_bonus(self.context, uid, name)

    def reset_draw_bonus(self, uid, name=""):
        links.hub_reset_draw_bonus(self.context, uid, name)

    def add_draw_bonus(self, uid, step, name=""):
        links.hub_add_draw_bonus(self.context, uid, step, name)
