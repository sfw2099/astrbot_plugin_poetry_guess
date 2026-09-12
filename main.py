# -*- coding: utf-8 -*-
"""猜诗句插件：猜诗句(Wordle式) + 劫难系统 + 道具系统 + 诗词对垒。

数据架构：
- 诗词库/个人诗句 → 诗词底座（astrbot_plugin_poetry_base）
- 成就/道具/使用记录 → 秋烨枢纽（astrbot_plugin_qiuye）
- 邀战猜诗词与 AI bot 暂未迁移（留在旧插件）
"""

import asyncio
import os
import re
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register, StarTools

from .game.guess_verse import (
    GuessVerseEngine, DuelVerseEngine,
    render_grid, render_blank, render_answer, render_hint, render_hazards,
    render_duel,
    extract_hanzi, extract_punct,
    INITIALS_LIST, FINALS_LIST,
    _init_plugin_dir,
)
from .game.items import ITEMS, roll_win_item, roll_loser_item
from .manifest import HAZARD_IDS, closer_level_name
from . import links
from .store_bridge import StoreBridge

BOT_ID = "999"


@register("astrbot_plugin_poetry_guess", "ALin", "猜诗句+劫难+道具+对垒", "1.0.0")
class PoetryGuessPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_poetry_guess")
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = str(self.plugin_data_dir)
        self.pm = StoreBridge(context)
        self.verse_max_attempts = self.config.get("verse_max_attempts", 10)
        self.verse_min_len = self.config.get("verse_min_len", 7)
        self.verse_max_len = self.config.get("verse_max_len", 18)
        self.guess_verse_sessions = {}  # session_id -> engine
        self.duel_sessions = {}         # sid -> duel dict
        self._shell_wait = {}           # 金蝉脱壳私聊换题
        # 启动时向秋烨注册成就/道具清单
        ok = links.hub_register_manifest(context)
        try:
            _init_plugin_dir(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass
        logger.info(f"[poetry_guess] 猜诗句插件已加载。数据目录: {self.data_dir}，秋烨注册{'成功' if ok else '失败(秋烨未就绪)'}")

    # ==================== 基础设施 ====================

    def _uid_name(self, uid):
        return self.pm._uid_name(uid)

    def _achieve_msg(self, uid, ach_id):
        from .manifest import ACHIEVEMENTS
        name = ACHIEVEMENTS.get(ach_id, (ach_id, ""))[0]
        uname = self._uid_name(uid)
        return f"🏆 {uname} 达成成就「{name}」！"

    def _base_ready_msg(self):
        return "⏳ 诗词底座插件未安装或数据库未就绪，请先安装 astrbot_plugin_poetry_base 并发送 /安装数据库"

    def _base_ready(self) -> bool:
        return links.base_db_ready(self.context)

    def _pick_random_index(self, target_text, used):
        """从未用过的下标随机挑一个。target_text 纯汉字。"""
        import random as _r
        avail = [i for i in range(len(target_text)) if i not in used]
        if not avail:
            return None
        return _r.choice(avail)

    def _pick_random_shengmu(self, target_parts, used):
        """随机挑一个未揭示过的字，返回 (idx, 声母, 韵母)。"""
        import random as _r
        avail = [i for i, p in enumerate(target_parts) if i not in used and p.get("initial")]
        if not avail:
            return None
        idx = _r.choice(avail)
        return idx, target_parts[idx].get("initial", ""), target_parts[idx].get("final", "")

    def _current_engine_fmt(self, engine):
        """返回当前猜诗句的格式 ('single', n)/(combo, (a,b))。"""
        punct = getattr(engine, "target_punct", []) or []
        hanzi = getattr(engine, "target_hanzi", "") or ""
        if not punct:
            return ("single", len(hanzi))
        lens = []
        cur = 0
        for pos, _p in punct:
            if pos >= len(hanzi):
                continue
            lens.append(pos - cur)
            cur = pos
        lens.append(len(hanzi) - cur)
        return ("combo", tuple(lens))

    def _pick_verse_with_char(self, engine, ch):
        """猜诗句同格式下随机找一句含 ch 的诗句。返回 (sentence,title,author,dynasty) 或 None。"""
        import random as _r
        fmt = self._current_engine_fmt(engine)
        # 优先：总库 SQL 直接按含字检索
        if self._base_ready():
            try:
                if fmt[0] == "single":
                    rows = links.base_search_by_chars_and_len(self.context, [ch], fmt[1], limit=30)
                else:
                    rows = links.base_search_combo_by_char(self.context, fmt[1][0], fmt[1][1], ch, limit=30)
                for verse, title, author, dynasty in _r.sample(rows, min(len(rows), 5)):
                    if verse != engine.target_text:
                        return (verse, title, author, dynasty)
            except Exception:
                pass
        # 兜底：经典曲库候选
        cands = []
        for p in links.base_classic_poems(self.context):
            sent = (p.get("sentence") or "")
            h = re.sub(r"[^\u4e00-\u9fff]", "", sent)
            if ch not in h:
                continue
            punct = extract_punct(sent)
            if fmt[0] == "single":
                if not punct and len(h) == fmt[1]:
                    cands.append((h, p))
            else:
                if punct:
                    segs = re.split(r"[，。！？、；：]", sent)
                    segs = [re.sub(r"[^\u4e00-\u9fff]", "", s) for s in segs if re.sub(r"[^\u4e00-\u9fff]", "", s)]
                    if len(segs) == len(fmt[1]) and all(len(s) == n for s, n in zip(segs, fmt[1])):
                        cands.append((sent, p))
        if cands:
            for sent, p in _r.sample(cands, min(len(cands), 5)):
                if sent != engine.target_text:
                    return (sent, p.get("title", ""), p.get("author", ""), p.get("dynasty", ""))
        # 最后兜底：总库随机抽样
        if self._base_ready():
            try:
                if fmt[0] == "single":
                    rows = links.base_get_random_verse(self.context, fmt[1], fmt[1], target_count=40, max_scan=200)
                else:
                    rows = links.base_get_random_verse_by_combo(self.context, fmt[1][0], fmt[1][1], target_count=40, max_scan=400)
                for verse, title, author, dynasty in rows:
                    if ch in verse and verse != engine.target_text:
                        return (verse, title, author, dynasty)
            except Exception:
                pass
        return None

    def _format_desc(self, fmt):
        if fmt[0] == "single":
            return f"{fmt[1]} 字单句"
        return " + ".join(f"{x} 字" for x in fmt[1]) + "（两句）"

    # ==================== 猜诗句 ====================

    @filter.command("猜诗句")
    async def start_guess_verse(self, event: AstrMessageEvent):
        if not self._base_ready():
            yield event.plain_result(self._base_ready_msg())
            return
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.guess_verse_sessions:
            yield event.plain_result("本群已有进行中的猜诗句，发送 /结束猜诗句 可终止。")
            return
        # ===== 解析参数：格式(4/5/6/7 或 44/34/43/55/77) / 提示方式(声|形) =====
        raw = str(event.get_message_str() or "").strip()
        tokens = re.sub(r"^[/／]?\s*猜诗句\s*", "", raw, flags=re.IGNORECASE).split()
        fmt = None
        hint_mode = None
        for token in tokens:
            if token in ("4", "5", "6", "7"):
                if fmt is not None:
                    yield event.plain_result("❌ 只能指定一种格式。\n用法：/猜诗句 [格式] [声|形]")
                    return
                fmt = ("single", int(token))
            elif token in ("44", "34", "43", "55", "77"):
                if fmt is not None:
                    yield event.plain_result("❌ 只能指定一种格式。\n用法：/猜诗句 [格式] [声|形]")
                    return
                fmt = ("combo", (int(token[0]), int(token[1])))
            elif token in ("声", "形", "拼音", "部首"):
                if hint_mode is not None:
                    yield event.plain_result("❌ 只能指定一种提示方式（声=拼音 / 形=部首）。\n用法：/猜诗句 [格式] [声|形]")
                    return
                hint_mode = "pinyin" if token in ("声", "拼音") else "radical"
            else:
                yield event.plain_result(
                    f"❌ 无效参数「{token}」。\n用法：/猜诗句 [格式] [声|形]\n"
                    f"格式：4/5/6/7 单句，或 44/34/43/55/77 两句\n"
                    f"例如：/猜诗句 4 声 ｜ /猜诗句 55 ｜ /猜诗句 形 7"
                )
                return

        # 未指定格式 -> 随机单句：4字10% / 6字10% / 5、7字各40%
        if fmt is None:
            import random as _r
            roll = _r.random()
            if roll < 0.10:
                fmt = ("single", 4)
            elif roll < 0.20:
                fmt = ("single", 6)
            elif roll < 0.60:
                fmt = ("single", 5)
            else:
                fmt = ("single", 7)

        # 未指定提示方式 -> 随机：声70% / 形30%
        if hint_mode is None:
            import random as _r2
            hint_mode = "pinyin" if _r2.random() < 0.70 else "radical"

        engine = GuessVerseEngine(None, None, 4, 7,
                                  classic_poems=links.base_classic_poems(self.context), hint_mode=hint_mode)
        engine.db_source = _BaseDBSource(self.context)
        if fmt[0] == "single":
            ok, msg = engine.new_game(word_len=fmt[1])
        else:
            ok, msg = engine.new_game(combo=fmt[1])
        if not ok:
            yield event.plain_result(f"❌ 初始化失败：{msg}")
            return

        self.guess_verse_sessions[session_id] = engine
        # 劫难机制：概率触发 1~N 个随机劫难（不重复）
        import random as _rh
        hazard_rate = float(self.config.get("hazard_rate", 20)) / 100.0
        hazard_max = max(1, min(9, int(self.config.get("hazard_max", 5))))
        if _rh.random() < hazard_rate:
            engine.hazards = _rh.sample(HAZARD_IDS, _rh.randint(1, hazard_max))
        hint_label = "拼音" if hint_mode == "pinyin" else "部首"
        from .manifest import ACHIEVEMENTS
        hazard_txt = ""
        if engine.hazards:
            hazard_txt = "☠️ 本局劫难：" + "、".join(ACHIEVEMENTS.get(h, (h,))[0] for h in engine.hazards) + "\n"
        yield event.plain_result(
            "🎯 【猜诗句】开始！\n" + hazard_txt +
            f"答案格式：{self._format_desc(fmt)}，提示方式：{hint_label}。\n"
            "发送「cc 诗句」进行猜测（两句需带标点），如：cc 离离原上草，一岁一枯荣\n"
            "每次猜测后，每个字的【汉字/声母/韵母/声调】独立着色（拼音模式）：\n"
            "🟢 绿色 = 正确且位置正确\n"
            "🟠 橙色 = 答案中存在但位置错误\n"
            "⚪ 灰色 = 答案中不存在\n"
            "部首模式下用字色+边框颜色提示。"
        )
        blank_path = os.path.join(self.data_dir, f"verse_blank_{session_id}.png")
        render_blank(engine, blank_path)
        yield event.image_result(blank_path)

    @filter.command("结束猜诗句")
    async def end_guess_verse(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        if session_id in self.guess_verse_sessions:
            engine = self.guess_verse_sessions.pop(session_id)
            ans_path = os.path.join(self.data_dir, f"verse_ans_{session_id}.png")
            render_answer(engine, ans_path)
            yield event.image_result(ans_path)
            yield event.plain_result(f"游戏结束，正确诗句：{engine.target_text}")
        else:
            yield event.plain_result("当前没有进行中的猜诗句游戏。")

    @filter.command("当前劫难")
    async def current_hazards(self, event: AstrMessageEvent):
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.guess_verse_sessions.get(session_id)
        if engine is None:
            yield event.plain_result("当前没有进行中的猜诗句游戏。")
            return
        img_path = os.path.join(self.data_dir, f"hazards_{session_id}.png")
        render_hazards(engine, img_path)
        yield event.image_result(img_path)

    @filter.command("猜诗句帮助")
    async def guess_verse_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "===== 猜诗句 帮助 =====\n"
            "1. /猜诗句 [格式] [声|形]：开始游戏（格式：4/5/6/7 单句 或 44/34/43/55/77 两句）\n"
            "2. 发送「cc 诗句」进行猜测（两句需带标点）\n"
            "3. 每次猜测后，每个字的【汉字/声母/韵母/声调】独立着色（拼音模式）\n"
            "4. 部首模式：字色 + 边框颜色提示\n"
            "5. 「提示」：查看声母韵母状态图（仅拼音模式）\n"
            "6. /当前劫难：查看本局劫难\n"
            "7. /结束猜诗句：结束当前游戏\n"
            "8. /诗词道具 <名称>：使用道具；/抽道具 <诗句>：抽道具\n"
        )

    # ==================== 道具 ====================

    @filter.command("诗词道具", alias={"使用道具"})
    async def use_item(self, event: AstrMessageEvent, item: str = "", n: str = ""):
        """使用诗词道具：/诗词道具 道具名 [数量] [额外参数，如定仙游的字或@玩家]"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        item = (item or "").strip()
        if not item or item not in ITEMS:
            yield event.plain_result(
                "未知道具。可用道具：\n"
                + "\n".join(f"· {k}：{v['desc']}" for k, v in ITEMS.items())
            )
            return
        count = 1
        try:
            count = max(1, min(int(n or "1"), 10))
        except ValueError:
            count = 1
        raw = str(event.get_message_str() or "").strip()
        tail = raw
        tail = re.sub(r"^[/／]?\s*(?:诗词道具|使用道具)\s*", "", tail, flags=re.IGNORECASE)
        tail = re.sub(r"^" + re.escape(item) + r"\s*", "", tail).strip()
        mnum = re.match(r"^(\d+)\s*", tail)
        if mnum:
            tail = tail[mnum.end():].strip()
        at_id = self._extract_at_id(event)
        if self.pm.item_count(uid, item, uname) < count:
            yield event.plain_result(f"道具【{item}】数量不足（持有 {self.pm.item_count(uid, item, uname)} 个）。")
            return
        result = await self._do_use_item(event, uid, uname, item, count, tail, at_id)
        if isinstance(result, str):
            yield event.plain_result(result)
            return
        async for m in result:
            yield m

    def _extract_at_id(self, event):
        for c in event.message_obj.message:
            if isinstance(c, Comp.At):
                return str(c.qq)
        raw_text = str(getattr(event, "message_str", "") or "")
        m = re.search(r"\[CQ:at,qq=(\d+)\]", raw_text)
        if m:
            return m.group(1)
        return None

    def _roll_draw(self, uid, uname):
        """统一抽道具掷骰：基础 10% + draw_bonus。返回 (命中?, 提示文本, 成就提示)。"""
        import random as _r
        bonus = self.pm.get_draw_bonus(uid, uname)
        rate = 10 + bonus
        if _r.randint(1, 100) <= rate:
            self.pm.reset_draw_bonus(uid, uname)
            item = _r.choice(list(ITEMS.keys()))
            self.pm.add_item(uid, item, 1, uname)
            return True, f"🎁 抽中了道具【{item}】！本次概率 {rate}%（保底已重置）。", ""
        self.pm.add_draw_bonus(uid, 10, uname)
        new_rate = min(10 + self.pm.get_draw_bonus(uid, uname), 100)
        ach = ""
        if new_rate >= 100 and self.pm.unlock_achievement(uid, "unlucky", uname):
            ach = f"🏆 {uname} 达成成就「真有这么倒霉的人啊？」！"
        return False, f"未抽中（本次 {rate}%），下次概率提升至 {new_rate}%。", ach

    @filter.command("抽道具")
    async def draw_item(self, event: AstrMessageEvent, text: str = ""):
        """抽道具：接一句你没积累过的诗句，随机概率获得道具（连抽递增），诗句并入库。"""
        uid = str(event.get_sender_id())
        uname = event.get_sender_name() or f"用户{uid}"
        raw = str(event.get_message_str() or "").strip()
        verse = re.sub(r"^[/／]?\s*抽道具\s*", "", raw, flags=re.IGNORECASE).strip()
        verse = verse.strip("「」\"\"''")
        clauses = [re.sub(r"[^\u4e00-\u9fff]", "", c) for c in re.split(r"[，。！？、；：\s]+", verse)]
        clauses = [c for c in clauses if c]
        if not clauses:
            yield event.plain_result("用法：/抽道具 一句你尚未积累过的诗句")
            return
        if not self._base_ready():
            yield event.plain_result(self._base_ready_msg())
            return
        # 校验每个分句均为库中真实诗句
        for c in clauses:
            if not links.base_is_in_library(self.context, c):
                yield event.plain_result(f"「{c}」不在诗词库中，请输入库中真实诗句。")
                return
        mine = self.pm.get_verses(uid)
        all_used = all(c in mine for c in clauses)
        if all_used:
            yield event.plain_result(f"{uname} 这句诗你已经积累过了，换一句新的试试吧！")
            return
        self.pm.record_verse(uid, verse, uname)
        for a in self.pm.check_verse_achievements(uid, uname):
            from .manifest import ACHIEVEMENTS
            yield event.plain_result(f"🏆 {uname} 达成成就「{ACHIEVEMENTS.get(a, (a, ''))[0]}」！")
        _hit, _tip, _ach = self._roll_draw(uid, uname)
        yield event.plain_result(_tip)
        if _ach:
            yield event.plain_result(_ach)

    async def _do_use_item(self, event, uid, uname, item, count, tail, at_id):
        """按道具分发。返回 str 或 async generator。"""
        session_id = str(event.get_group_id() or event.get_session_id())
        engine = self.guess_verse_sessions.get(session_id)
        duel_sid = None
        duel = None
        for k, d in self.duel_sessions.items():
            if session_id == k or uid in (d.get("challenger_id"), d.get("opponent_id")):
                duel, duel_sid = d, k
                break
        # 提示类（火眼金睛/三仙归洞/仙人指路）
        if item in ("火眼金睛", "三仙归洞", "仙人指路"):
            if engine is not None and "sanjianqikou" in getattr(engine, "hazards", []):
                return "本局劫难【三缄其口】：无法使用提示类道具。"
            target_parts = None
            target_text = None
            target_author = None
            if engine is not None and getattr(engine, "target_parts", None):
                target_parts = engine.target_parts
                target_text = getattr(engine, "target_hanzi", "")
                target_author = getattr(engine, "author", "")
            elif duel is not None and duel.get("engine"):
                de = duel["engine"]
                my_side = "a" if uid == de.a_id else ("b" if uid == de.b_id else None)
                if my_side is None:
                    return "未找到你在对垒中的位置。"
                target_parts = de.target_parts_of(my_side)
                target_text = de.a_target_hanzi if my_side == "a" else de.b_target_hanzi
                try:
                    puzzle = duel.get("puzzles", {}).get(de.b_id if my_side == "a" else de.a_id, "")
                    meta = links.base_check_exact_poetry(self.context, puzzle) if self._base_ready() else None
                    target_author = meta[1] if meta else ""
                except Exception:
                    target_author = ""
            if not target_parts:
                return "当前没有进行中的猜诗句/对垒游戏，无法使用该道具。"
            self.pm.consume_item(uid, item, count, uname)
            if item == "火眼金睛":
                out = []
                used = set()
                for _ in range(count):
                    idx = self._pick_random_index(target_text, used)
                    if idx is None:
                        break
                    used.add(idx)
                    ch = target_text[idx]
                    out.append(f"第{idx+1}个字是「{ch}」")
                return "🔍 火眼金睛：答案中 " + "，".join(out) if out else "答案较短，无法再揭示。"
            if item == "三仙归洞":
                out = []
                used = set()
                for _ in range(count):
                    res = self._pick_random_shengmu(target_parts, used)
                    if not res:
                        break
                    idx, sm, ym = res
                    used.add(idx)
                    out.append(f"第{idx+1}字 声母「{sm}」韵母「{ym}」")
                return "🎯 三仙归洞：" + "；".join(out) if out else "无法继续揭示。"
            if target_author:
                return f"📜 仙人指路：当前答案作者是【{target_author}】"
            return "无法解析答案作者，请稍后再试。"
        # 招灾
        if item == "招灾":
            if engine is None:
                return "招灾需在猜诗句进行中使用。"
            if getattr(engine, "history", []):
                return "本局已有人猜测，招灾只能在开局未猜测时使用。"
            remaining = [h for h in HAZARD_IDS if h not in getattr(engine, "hazards", [])]
            if not remaining:
                return "本局已集齐所有劫难。"
            import random as _r
            h = _r.choice(remaining)
            engine.hazards = list(getattr(engine, "hazards", [])) + [h]
            self.pm.consume_item(uid, item, count, uname)
            from .manifest import ACHIEVEMENTS
            return f"☠️ 招灾成功！本局新增劫难【{ACHIEVEMENTS.get(h, (h,))[0]}】。"
        # 解难
        if item == "解难":
            if engine is None:
                return "解难需在猜诗句进行中使用。"
            hazards = list(getattr(engine, "hazards", []) or [])
            if not hazards:
                return "本局没有劫难可解。"
            import random as _r
            removed = []
            for _ in range(count):
                if not hazards:
                    break
                h = _r.choice(hazards)
                hazards.remove(h)
                removed.append(h)
            if removed:
                engine.hazards = hazards
                self.pm.consume_item(uid, item, len(removed), uname)
                from .manifest import ACHIEVEMENTS
                names = "、".join(ACHIEVEMENTS.get(h, (h,))[0] for h in removed)
                return f"🧧 解难成功！已解除劫难【{names}】。"
            return "本局没有劫难可解。"
        # 定仙游
        if item == "定仙游":
            if engine is None:
                return "定仙游需在猜诗句进行中使用。"
            ch = (tail or "").strip()
            if not ch:
                return "用法：/诗词道具 定仙游 汉字"
            new_verse = self._pick_verse_with_char(engine, ch[0])
            if not new_verse:
                return f"未能找到同格式且含「{ch[0]}」的诗句，换一个试试。"
            poem = {"title": new_verse[1], "author": new_verse[2], "dynasty": new_verse[3]}
            engine._set_target(new_verse[0], poem)
            engine.participants = set()
            engine.user_guesses = {}
            engine.user_verses = set()
            engine.user_initials = {}
            engine.user_finals = {}
            self.pm.consume_item(uid, item, count, uname)
            import time as _t
            blank_path = os.path.join(self.data_dir, f"verse_blank_{session_id}_{int(_t.time())}.png")
            render_blank(engine, blank_path)

            async def _gen():
                yield event.plain_result(f"🔮 定仙游：已重新出题（含「{ch[0]}」），旧题目作废，请重新开始猜测。")
                yield event.image_result(blank_path)
            return _gen()
        # 金蝉脱壳（对垒）
        if item == "金蝉脱壳":
            if duel is None or not duel.get("engine"):
                return "金蝉脱壳需在诗词对垒进行中、且轮到你的回合使用。"
            de = duel["engine"]
            side = "a" if uid == de.a_id else "b"
            if not de.is_turn(uid):
                return "现在不是你的回合，无法使用金蝉脱壳。"
            if self.pm.item_count(uid, item, uname) <= 0:
                return f"道具【{item}】数量不足。"
            if uid in self._shell_wait:
                if time.time() - self._shell_wait[uid].get("ts", 0) <= 120:
                    return "你已有进行中的金蝉脱壳换题，请到私聊发送「cc 新诗句」。"
                self._shell_wait.pop(uid, None)
            self._shell_wait[uid] = {"sid": duel_sid, "side": side, "ts": time.time()}
            ok = await self._send_private(
                event.bot, uid,
                "🪙 金蝉脱壳：请私聊发送你的新题目（格式需与当前一致，前缀 cc），"
                "例：cc 床前明月光",
            )
            return f"🪙 已私信你，请到私聊发送新题目（前缀 cc）。" + ("" if ok else "（私聊发送失败，请确认已添加机器人为好友）")
        # 探囊取物
        if item == "探囊取物":
            if not at_id or at_id == uid:
                return "请 @ 一个目标玩家来偷取道具。"
            stolen = self.pm.take_random_item(at_id, uid, self._uid_name(at_id), uname)
            if not stolen:
                return f"对方没有可偷的道具。"
            self.pm.consume_item(uid, item, 1, uname)
            return f"🪝 探囊取物成功！从对方身上顺走了【{stolen}】。"
        # 对垒回合类
        if item in ("乐不思蜀", "百战不殆", "孤注一掷", "请君入梦"):
            if duel is None or not duel.get("engine"):
                return f"{item}需在诗词对垒进行中使用。"
            de = duel["engine"]
            if not de.is_turn(uid):
                return f"{item}需在你的回合使用。"
            side = "a" if uid == de.a_id else "b"
            opp = "b" if side == "a" else "a"
            eff = duel.setdefault("item_effects", {"skip": {}, "immune": {}, "gamble": {}, "dream": {}})
            self.pm.consume_item(uid, item, count, uname)
            if item == "乐不思蜀":
                eff["skip"][opp] = eff["skip"].get(opp, 0) + count
                return "😴 乐不思蜀：对方下一回合将跳过，你获得一次额外回合。"
            if item == "百战不殆":
                eff["immune"][side] = eff["immune"].get(side, 0) + count
                return "🛡 百战不殆：接下来你方将有若干次免疫机会（对方猜中也不结束）。"
            if item == "孤注一掷":
                eff["gamble"][side] = {"active": True, "left": 3 * count}
                return "🎲 孤注一掷：接下来你将连续追加若干次自己的回合；若仍猜不中则由对方获胜。"
            if item == "请君入梦":
                eff["dream"][opp] = eff["dream"].get(opp, 0) + 2 * count
                return "💤 请君入梦：对方下两个回合将由系统随机代猜。"
            return "未知道具。"
        return f"道具【{item}】暂未实现使用逻辑。"

    async def _send_private(self, bot, user_id, text):
        try:
            await bot.api.call_action(
                "send_private_msg",
                user_id=int(user_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
            return True
        except Exception as e:
            logger.error(f"[poetry_guess] 私聊发送失败 user={user_id}: {e}")
            return False

    def _puzzle_matches_format(self, de, side, old_puzzle, new_puzzle):
        """校验新题（金蝉脱壳用）与旧题同格式，且为库中真实诗句。"""
        old_h = re.sub(r"[^\u4e00-\u9fff]", "", old_puzzle)
        old_punct = extract_punct(old_puzzle)
        nh = re.sub(r"[^\u4e00-\u9fff]", "", new_puzzle)
        npunct = extract_punct(new_puzzle)

        def _lens(h, ps):
            ps = [p for p in ps if p[0] < len(h)]
            lens = []
            cur = 0
            for pos, _p in ps:
                lens.append(pos - cur)
                cur = pos
            lens.append(len(h) - cur)
            return lens

        if not old_punct:
            if npunct:
                return False
            if len(nh) != len(old_h):
                return False
        else:
            if not npunct:
                return False
            if _lens(old_h, old_punct) != _lens(nh, npunct) or len(nh) != len(old_h):
                return False
            segs = re.split(r"[，。！？、；：]", new_puzzle)
            segs = [re.sub(r"[^\u4e00-\u9fff]", "", s) for s in segs if re.sub(r"[^\u4e00-\u9fff]", "", s)]
            if len(segs) != 2 or not self._base_ready() or not links.base_is_adjacent_pair(self.context, segs[0], segs[1]):
                return False
        return True

    # ==================== 猜测处理（含劫难/结算） ====================

    async def _handle_shell_private(self, event, msg_raw, is_private, handled):
        """金蝉脱壳：玩家在私聊发送新题（cc 前缀），校验后换题并通知对垒群。"""
        if not is_private:
            return
        uid = str(event.get_sender_id())
        st = self._shell_wait.get(uid)
        if not st:
            return
        if not msg_raw.startswith("cc"):
            return
        handled[0] = True
        clean = re.sub(r"^cc\s*", "", msg_raw).strip()
        sid = st.get("sid")
        duel = self.duel_sessions.get(sid)
        if not duel or not duel.get("engine"):
            self._shell_wait.pop(uid, None)
            yield event.plain_result("对垒会话已结束，金蝉脱壳取消。")
            return
        de = duel["engine"]
        side = "a" if uid == de.a_id else "b"
        old_puzzle = duel.get("puzzles", {}).get(uid, "")
        if not self._puzzle_matches_format(de, side, old_puzzle, clean):
            yield event.plain_result("新题格式需与当前一致且为库中真实诗句，请重发（前缀 cc）。")
            return
        de.replace_side_puzzle(side, clean)
        duel["puzzles"][uid] = clean
        duel.setdefault("user_verses", {}).setdefault(
            de.b_id if side == "a" else de.a_id, set()).clear()
        self.pm.consume_item(uid, "金蝉脱壳", 1, event.get_sender_name() or f"用户{uid}")
        self._shell_wait.pop(uid, None)
        de.switch_turn()
        import time as _t
        duel_img = os.path.join(self.data_dir, f"duel_{sid}_{int(_t.time())}.png")
        render_duel(de, duel_img, hint_mode=duel.get("hint_mode", "pinyin"))
        origin = duel.get("group_origin")
        turn_text = f"🪙 金蝉脱壳成功，对方要重新猜了！现在轮到 {de.current_name()}。"
        if origin:
            try:
                from astrbot.api.all import Plain as _Plain, Image as _Image, MessageChain as _MC
                await self.context.send_message(origin, _MC([_Plain(turn_text), _Image.fromFileSystem(duel_img)]))
            except Exception as e:
                logger.error(f"[poetry_guess] 金蝉脱壳群通知失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_recv_msg(self, event: AstrMessageEvent):
        msg_raw = event.message_str.strip()
        is_private = bool(event.is_private_chat()) if hasattr(event, "is_private_chat") else False
        # 金蝉脱壳：私聊换题优先
        handled_shell = [False]
        async for result in self._handle_shell_private(event, msg_raw, is_private, handled_shell):
            yield result
        if handled_shell[0]:
            return
        if not (msg_raw.startswith("cc") or self.duel_sessions or self.guess_verse_sessions):
            return
        if msg_raw.startswith(("(", "（")) and msg_raw.endswith((")", "）")):
            return
        if not msg_raw or msg_raw.startswith(("/", "查询", "安装", "结束", "纵横", "衔字", "蛇形", "删除", "恢复", "bot")):
            return
        if msg_raw in ("猜诗句", "猜诗句帮助", "结束猜诗句", "当前劫难", "猜诗句规则") or msg_raw.startswith("猜诗句 "):
            return
        if msg_raw in ("提示", "声韵提示", "拼音提示"):
            session_id0 = str(event.get_group_id() or event.get_session_id())
            engine0 = self.guess_verse_sessions.get(session_id0)
            if engine0 is not None:
                if "sanjianqikou" in getattr(engine0, "hazards", []):
                    yield event.plain_result("本局劫难【三缄其口】：无法使用提示。")
                    return
                if engine0.hint_mode != "pinyin":
                    yield event.plain_result("当前为部首提示模式，无拼音提示。")
                    return
                hint_path = os.path.join(self.data_dir, f"verse_hint_{session_id0}.png")
                render_hint(engine0, hint_path)
                yield event.image_result(hint_path)
                return
            return

        session_id = str(event.get_group_id() or event.get_session_id())

        # 对垒处理
        if msg_raw.startswith("cc") or self.duel_sessions:
            handled = [False]
            async for result in self._handle_duel_message(event, msg_raw, is_private, handled):
                yield result
            if handled[0]:
                return

        # 猜诗句处理
        if session_id in self.guess_verse_sessions:
            engine = self.guess_verse_sessions[session_id]
            if not msg_raw.startswith("cc"):
                return
            clean = re.sub(r'^cc\s*', '', msg_raw).strip()
            uid = str(event.get_sender_id())
            uname = event.get_sender_name() or f"用户{uid}"
            links.hub_record_plugin_use(self.context, uid, uname)
            result = self._apply_verse_guess(engine, session_id, uid, uname, clean)
            if not result["ok"]:
                yield event.plain_result(result["err"])
                return
            for kind, payload in result["msgs"]:
                if kind == "text":
                    yield event.plain_result(payload)
                else:
                    yield event.image_result(payload)
            return

    # ==================== 猜诗句核心 ====================

    def _apply_verse_guess(self, engine, session_id, uid, uname, clean):
        """执行猜诗句猜测的核心逻辑。返回结果 dict，不发送消息。"""
        msgs = []
        hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)

        def _fail(err):
            return {"ok": False, "err": err, "comp": None, "all_correct": False,
                    "finished": False, "msgs": msgs}

        if not hanzi or len(hanzi) != len(engine.target_hanzi):
            return _fail(f"答案 {len(engine.target_hanzi)} 个字，当前 {len(hanzi)} 字。请输入「cc 诗句」。")
        ok_fmt, fmt_msg = engine.check_format(clean)
        if not ok_fmt:
            return _fail(fmt_msg or "格式不正确。")
        # 判在不在总库中
        if extract_punct(clean):
            segs = re.split(r'[，。！？、；：]', clean)
            segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
            if len(segs) != 2 or not self._base_ready() or not links.base_is_adjacent_pair(self.context, segs[0], segs[1]):
                return _fail(f"「{clean}」未在诗词库中（需为库中相邻两句）。")
        else:
            if not links.base_is_in_library(self.context, clean):
                return _fail(f"「{clean}」不在诗词库中，请输入曲库诗句。")
        hazards = getattr(engine, "hazards", []) or []
        # 推陈出新：只能使用所有参与者都未积累过的新句（答案本身除外，避免死局）
        if "tuichenchuxin" in hazards and hanzi != engine.target_hanzi:
            from .poetry_utils import split_single_clauses
            for clause in split_single_clauses(clean):
                for p_uid in (set(getattr(engine, "participants", set())) | {uid}):
                    if links.base_has_clause(self.context, str(p_uid), clause):
                        return _fail("本局劫难【推陈出新】：须使用所有参与者都未积累过的新句。")
        # 一脉相承：除首句外，每次猜测须与上一句共享至少一个汉字
        if "yimaixiangcheng" in hazards and engine.history:
            prev_h = extract_hanzi(engine.history[-1][0])
            if not (set(hanzi) & set(prev_h)):
                return _fail("本局劫难【一脉相承】：须与上一句共享至少一个汉字。")
        # 记录参与者与单局个人猜测次数
        if not hasattr(engine, "participants"):
            engine.participants = set()
            engine.user_guesses = {}
            engine.user_verses = set()
            engine.user_initials = {}
            engine.user_finals = {}
        engine.participants.add(uid)
        engine.user_guesses[uid] = engine.user_guesses.get(uid, 0) + 1
        # 🐖 重复诗句检测
        if hanzi in engine.user_verses:
            pig_count = self.pm.add_pig(uid, uname)
            msgs.append(("text", f"🐖 {uname} 重复诗句！猪+1（{'🐖' * pig_count}）"))
        else:
            engine.user_verses.add(hanzi)
        # 记录诗句到个人数据
        added = self.pm.record_verse(uid, clean, uname)
        self.pm.inc_stat(uid, "total_guesses", 1, uname)
        # 游戏内抽道具
        if added > 0 and uid != BOT_ID:
            _hit, _tip, _ach = self._roll_draw(uid, uname)
            if _hit:
                msgs.append(("text", f"✨ {uname} 使用新诗句，触发抽道具！{_tip}"))
            if _ach:
                msgs.append(("text", _ach))
        # 检查个人/特殊成就
        for a in self.pm.check_verse_achievements(uid, uname):
            msgs.append(("text", self._achieve_msg(uid, a)))
        ok, err, comp, all_correct = engine.guess(clean)
        if not ok:
            return _fail(err)
        # 追踪本局声母/韵母使用
        engine.user_initials.setdefault(uid, set())
        engine.user_finals.setdefault(uid, set())
        guess_parts = engine.history[-1][1] if engine.history else []
        for gp in guess_parts:
            if gp.get("initial"):
                engine.user_initials[uid].add(gp["initial"])
            if gp.get("final"):
                engine.user_finals[uid].add(gp["final"])
        # 一事无成
        if comp and all(
            c is not None and c.get("char") == "absent"
            and c.get("initial") == "absent" and c.get("final") == "absent"
            for c in comp
        ):
            if self.pm.unlock_achievement(uid, "all_gray", uname):
                msgs.append(("text", self._achieve_msg(uid, "all_gray")))
        # 旗开得胜
        if len(engine.history) == 1:
            has_char = any(c is not None and c.get("char") in ("correct", "present") for c in comp)
            has_pinyin = any(
                c is not None and c.get("initial") in ("correct", "present")
                and c.get("final") in ("correct", "present")
                for c in comp
            )
            if has_char or has_pinyin:
                if self.pm.unlock_achievement(uid, "first_hit_char", uname):
                    msgs.append(("text", self._achieve_msg(uid, "first_hit_char")))
        # 七步成诗：每猜满 7 次未结束，换含某字的新题
        if not all_correct and "qibuchengshi" in hazards and len(engine.history) % 7 == 0:
            import random as _rr
            ch = _rr.choice(engine.target_hanzi)
            nv = self._pick_verse_with_char(engine, ch)
            if nv:
                engine._set_target(nv[0], {"title": nv[1], "author": nv[2], "dynasty": nv[3]})
                blank_path = os.path.join(self.data_dir, f"verse_blank_{session_id}.png")
                render_blank(engine, blank_path)
                msgs.append(("text", f"⚡ 七步成诗：题目已更换（新题含「{ch}」），请重新开始猜测。"))
                msgs.append(("image", blank_path))
                return {"ok": True, "err": None, "comp": comp, "all_correct": False,
                        "finished": False, "msgs": msgs}
        img_path = os.path.join(self.data_dir, f"verse_{session_id}.png")
        render_grid(engine, img_path, max_attempts=15 if "baijuguoxi" in hazards else None, hint_mode=engine.hint_mode)
        msgs.append(("image", img_path))
        finished = False
        if all_correct:
            ans_path = os.path.join(self.data_dir, f"verse_ans_{session_id}.png")
            render_answer(engine, ans_path)
            msgs.append(("image", ans_path))
            n_participants = len(getattr(engine, "participants", set()))
            msgs.append(("text", f"🎉 猜中了！{engine.target_text}\n（本局参与 {n_participants} 人）"))
            for m in self._settle_guess_verse_achievements(engine, uid, uname):
                msgs.append(("text", m))
            # 道具掉落
            if getattr(engine, "hazards", []):
                import random as _rr
                for puid in list(getattr(engine, "participants", set())):
                    reward = _rr.choice(["招灾", "解难"])
                    pname = self._uid_name(puid)
                    self.pm.add_item(str(puid), reward, 1, pname)
                    msgs.append(("text", f"🎁 {pname} 通关劫难，获得道具【{reward}】！"))
            else:
                wg = engine.user_guesses.get(uid, 0) if hasattr(engine, "user_guesses") else len(engine.history)
                win_item = roll_win_item(wg, "verse")
                if win_item:
                    self.pm.add_item(uid, win_item, 1, uname)
                    msgs.append(("text", f"🎁 {uname} 获得道具【{win_item}】！"))
                for puid in list(getattr(engine, "participants", set())):
                    if str(puid) == str(uid):
                        continue
                    loser_item = roll_loser_item("verse")
                    if loser_item:
                        pname = self._uid_name(puid)
                        self.pm.add_item(puid, loser_item, 1, pname)
                        msgs.append(("text", f"🎁 {pname} 获得道具【{loser_item}】！"))
            self.guess_verse_sessions.pop(session_id, None)
            finished = True
        elif engine.is_finished() or ("baijuguoxi" in hazards and len(engine.history) >= 15):
            ans_path = os.path.join(self.data_dir, f"verse_ans_{session_id}.png")
            render_answer(engine, ans_path)
            msgs.append(("image", ans_path))
            msgs.append(("text", f"机会耗尽！正确诗句：{engine.target_text}"))
            self.guess_verse_sessions.pop(session_id, None)
            finished = True
        else:
            msgs.append(("text", f"继续猜！已猜 {len(engine.history)} 次（不限次数）"))
        return {"ok": True, "err": None, "comp": comp, "all_correct": all_correct,
                "finished": finished, "msgs": msgs}

    def _settle_guess_verse_achievements(self, engine, winner_uid, winner_name):
        """猜诗句猜中后结算所有参与者成就。返回提示消息列表。"""
        msgs = []
        participants = getattr(engine, "participants", set())
        user_guesses = getattr(engine, "user_guesses", {})
        n = len(participants)
        total = len(engine.history)
        target_hanzi = getattr(engine, "target_hanzi", "") or ""
        all_guess_text = ""
        for _t, _p, _c in getattr(engine, "history", []):
            all_guess_text += _t or ""
        logger.info(f"[poetry_guess] 猜诗句结算：本局参与 {n} 人 → {sorted(participants)}")

        # 道法自然
        dao_trigger = False
        non_win_guesses = [t for t, _p, _c in getattr(engine, "history", [])]
        if len(non_win_guesses) >= 3 and len(engine.history) >= 2:
            non_win = engine.history[:-1]
            if len(non_win) >= 2:
                common = None
                for t, _p, _c in non_win:
                    titles = set(self._poem_titles_of(t or ""))
                    if not titles:
                        common = None
                        break
                    common = titles if common is None else (common & titles)
                    if not common:
                        break
                dao_trigger = bool(common)
        crychic_trigger = n >= 3 and all(ch in all_guess_text for ch in "苦来兮")

        for p_uid in participants:
            p_name = self._uid_name(p_uid)
            pm = self.pm
            counts = {1: "solo_pass", 2: "double_pass", 3: "triple_pass",
                      4: "four_scholar", 5: "five_poem", 6: "six_scholar",
                      7: "seven_sage", 8: "eight_scholar"}
            if n in counts:
                if pm.unlock_achievement(p_uid, counts[n], p_name):
                    msgs.append(self._achieve_msg(p_uid, counts[n]))
            if total <= 10:
                if pm.unlock_achievement(p_uid, "minimalist", p_name):
                    msgs.append(self._achieve_msg(p_uid, "minimalist"))
            h = time.localtime().tm_hour
            if h >= 23 or h < 5:
                if pm.unlock_achievement(p_uid, "night_owl", p_name):
                    msgs.append(self._achieve_msg(p_uid, "night_owl"))
            elif 5 <= h < 8:
                if pm.unlock_achievement(p_uid, "early_bird", p_name):
                    msgs.append(self._achieve_msg(p_uid, "early_bird"))
            if user_guesses.get(p_uid, 0) >= 20:
                if pm.unlock_achievement(p_uid, "persistent", p_name):
                    msgs.append(self._achieve_msg(p_uid, "persistent"))
            user_initials = getattr(engine, "user_initials", {}).get(p_uid, set())
            user_finals = getattr(engine, "user_finals", {}).get(p_uid, set())
            if user_initials >= set(INITIALS_LIST):
                if pm.unlock_achievement(p_uid, "all_initials", p_name):
                    msgs.append(self._achieve_msg(p_uid, "all_initials"))
            if user_finals >= set(FINALS_LIST):
                if pm.unlock_achievement(p_uid, "all_finals", p_name):
                    msgs.append(self._achieve_msg(p_uid, "all_finals"))
            if n >= 2 and (h >= 23 or h < 5):
                if pm.unlock_achievement(p_uid, "night_group", p_name):
                    msgs.append(self._achieve_msg(p_uid, "night_group"))
            pm.inc_stat(p_uid, "guess_games", 1, p_name)
            for a in pm.check_verse_achievements(p_uid, p_name):
                msgs.append(self._achieve_msg(p_uid, a))
            beloved = pm.check_beloved_verse(p_uid, p_name)
            if beloved:
                bv, bc = beloved
                msgs.append(f"🏆 {p_name} 达成成就「挚爱诗句-{bv}」！（使用 {bc} 次）")
            season_map = {"春": 1, "夏": 2, "秋": 4, "冬": 8}
            season_ach = {"春": "spring_redemption", "夏": "summer_apprentice",
                          "秋": "autumn_corpse", "冬": "winter_breath"}
            for s, bit in season_map.items():
                if s in target_hanzi:
                    if pm.unlock_achievement(p_uid, season_ach[s], p_name):
                        msgs.append(self._achieve_msg(p_uid, season_ach[s]))
                    if pm.collect_season(p_uid, bit, p_name):
                        msgs.append(self._achieve_msg(p_uid, "king_stephen"))
            if n >= 3 and all(ch in all_guess_text for ch in "春日影"):
                if pm.unlock_achievement(p_uid, "spring_film", p_name):
                    msgs.append(self._achieve_msg(p_uid, "spring_film"))
            if crychic_trigger:
                if pm.unlock_achievement(p_uid, "crychic", p_name):
                    msgs.append(self._achieve_msg(p_uid, "crychic"))
            if dao_trigger:
                if pm.unlock_achievement(p_uid, "dao_fa_zi_ran", p_name):
                    msgs.append(self._achieve_msg(p_uid, "dao_fa_zi_ran"))
            if p_uid == winner_uid:
                pm.inc_stat(p_uid, "guess_wins", 1, p_name)
                if total == 1:
                    if pm.unlock_achievement(p_uid, "first_hit", p_name):
                        msgs.append(self._achieve_msg(p_uid, "first_hit"))
                if n >= 2 and user_guesses.get(p_uid, 0) == 1:
                    if pm.unlock_achievement(p_uid, "peach_picker", p_name):
                        msgs.append(self._achieve_msg(p_uid, "peach_picker"))
                ach_closer = pm.get_achievements(p_uid).get("closer", {})
                cc = ach_closer.get("progress", 0) + 1
                new_lv = pm.check_closer(p_uid, cc, p_name)
                if new_lv:
                    msgs.append(f"🏆 {p_name} 达成成就「{new_lv}」！")
                st = pm.load(p_uid).get("stats", {})
                if st.get("guess_wins", 0) >= 10:
                    if pm.unlock_achievement(p_uid, "guess_win_10", p_name):
                        msgs.append(self._achieve_msg(p_uid, "guess_win_10"))
            # 劫难成就 + x重天
            for hz in getattr(engine, "hazards", []) or []:
                if pm.unlock_achievement(p_uid, hz, p_name):
                    msgs.append(self._achieve_msg(p_uid, hz))
            if getattr(engine, "hazards", []):
                tier_name = pm.check_hazard_tier(p_uid, len(engine.hazards), p_name)
                if tier_name:
                    msgs.append(f"🏆 {p_name} 达成成就「{tier_name}」！")
        return msgs

    def _poem_titles_of(self, text):
        """查询猜测句出自哪些诗词（标题集合）。"""
        titles = set()
        clean = re.sub(r'[^\u4e00-\u9fff]', '', text or "")
        if not clean or not self._base_ready():
            return titles
        try:
            r = links.base_check_exact_poetry(self.context, clean)
            if r:
                titles.add(r[0])
        except Exception:
            pass
        return titles

    # ==================== 诗词对垒（完整版） ====================

    @staticmethod
    def _parse_poem_format(token):
        """解析格式 token：4/5/6/7 单句 或 44/34/43/55/77 两句。"""
        token = (token or "").strip()
        if token in ("4", "5", "6", "7"):
            return ("single", int(token))
        if token in ("44", "34", "43", "55", "77"):
            return ("combo", (int(token[0]), int(token[1])))
        return None

    @filter.command("诗词对垒")
    async def start_duel(self, event: AstrMessageEvent):
        if not self._base_ready():
            yield event.plain_result(self._base_ready_msg())
            return
        group_id = str(event.get_group_id() or "")
        if not group_id or group_id == "None":
            yield event.plain_result("诗词对垒仅支持群聊哦~")
            return
        session_id = group_id
        if session_id in self.duel_sessions:
            yield event.plain_result("当前群已有进行中的诗词对垒。")
            return

        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or f"用户{sender_id}"

        # ===== 解析参数：@某人 / 格式 / 提示方式，可组合任意顺序 =====
        target_id = self._extract_at_id(event)
        fmt = None
        hint_mode = None
        msg_text = str(event.get_message_str() or "")
        remainder = re.sub(r"^[/／]?\s*诗词对垒", "", msg_text, flags=re.IGNORECASE)
        remainder = re.sub(r"\[CQ:at,qq=\d+\]", " ", remainder)
        remainder = re.sub(r"@\d{5,12}", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip()

        for token in remainder.split(" "):
            if not token:
                continue
            parsed = self._parse_poem_format(token)
            if parsed:
                if fmt is not None:
                    yield event.plain_result("❌ 只能指定一种格式（4/5/6/7 单句 或 44/34/43/55/77 两句）。\n用法：/诗词对垒 [@某人] [格式] [声|形]")
                    return
                fmt = parsed
            elif token in ("声", "形", "拼音", "部首"):
                if hint_mode is not None:
                    yield event.plain_result("❌ 只能指定一种提示方式（声=拼音 / 形=部首）。\n用法：/诗词对垒 [@某人] [格式] [声|形]")
                    return
                hint_mode = "pinyin" if token in ("声", "拼音") else "radical"
            elif token.lower() in ("bot", "机器人", "ai"):
                yield event.plain_result("🤖 bot 对垒暂未迁移（AI bot 后续版本支持），请 @ 一位群友。")
                return
            else:
                yield event.plain_result(
                    f"❌ 无效参数「{token}」。\n用法：/诗词对垒 [@某人] [格式] [声|形]\n"
                    f"格式：4/5/6/7 单句，或 44/34/43/55/77 两句\n"
                    f"例如：/诗词对垒 4 声 ｜ /诗词对垒 55 形 @某人"
                )
                return

        if target_id == sender_id:
            yield event.plain_result("不能挑战自己哦~")
            return

        # 未指定格式 -> 随机单句：4字10% / 6字10% / 5、7字各40%
        if fmt is None:
            import random as _r
            roll = _r.random()
            if roll < 0.10:
                fmt = ("single", 4)
            elif roll < 0.20:
                fmt = ("single", 6)
            elif roll < 0.60:
                fmt = ("single", 5)
            else:
                fmt = ("single", 7)

        # 未指定提示方式 -> 随机：声70% / 形30%
        if hint_mode is None:
            import random as _r2
            hint_mode = "pinyin" if _r2.random() < 0.70 else "radical"

        target_name = None
        if target_id:
            target_name = f"用户{target_id}"
            try:
                info = await event.bot.api.call_action("get_group_member_info", group_id=int(group_id), user_id=int(target_id))
                if isinstance(info, dict) and "data" in info:
                    info = info["data"]
                target_name = info.get("nickname") or info.get("card") or target_name
            except Exception:
                pass

        self.duel_sessions[session_id] = {
            "state": "wait_confirm",
            "challenger_id": sender_id,
            "challenger_name": sender_name,
            "opponent_id": target_id,   # 无@则为 None，由第一个回复【接受】者担任
            "opponent_name": target_name,
            "fmt": fmt,
            "hint_mode": hint_mode,
            "puzzles": {},        # {user_id: sentence}
            "puzzle_done": set(), # 已出题的人
            "engine": None,
            "item_effects": {"skip": {}, "immune": {}, "gamble": {}, "dream": {}},
            "group_origin": getattr(event, "unified_msg_origin", None),
            "created_at": time.time(),
        }

        hint_label = "拼音" if hint_mode == "pinyin" else "部首"
        fmt_desc = self._format_desc(fmt)
        if target_id:
            yield event.plain_result(
                f"🍵 【诗词对垒】\n"
                f"{sender_name} 向 {target_name} 发起对垒！\n"
                f"双方各出「{fmt_desc}」诗句（曲库中，前缀「cc」）作为题目，随后互猜对方诗句，先猜中者获胜。\n"
                f"提示方式：{hint_label}\n"
                f"请 {target_name} 回复【接受】开始，或回复【拒绝】。（2 分钟内有效）"
            )
        else:
            yield event.plain_result(
                f"🍵 【诗词对垒】\n"
                f"{sender_name} 发起自由对垒！\n"
                f"双方各出「{fmt_desc}」诗句（曲库中，前缀「cc」）作为题目，随后互猜对方诗句，先猜中者获胜。\n"
                f"提示方式：{hint_label}\n"
                f"第一个回复【接受】的群成员将作为对手。（2 分钟内有效）"
            )
        try:
            origin = getattr(event, "unified_msg_origin", None)
            asyncio.create_task(self._duel_confirm_timeout(session_id, origin))
        except Exception:
            pass

    @filter.command("结束对垒")
    async def end_duel(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        session_id = str(event.get_group_id() or event.get_session_id())
        for k, d in list(self.duel_sessions.items()):
            if k == session_id or uid in (d.get("challenger_id"), d.get("opponent_id")):
                self.duel_sessions.pop(k, None)
                if d.get("engine"):
                    ans_path = os.path.join(self.data_dir, f"duel_ans_{k}.png")
                    try:
                        render_answer(d["engine"], ans_path)
                        yield event.image_result(ans_path)
                    except Exception:
                        pass
                yield event.plain_result("对垒已结束。")
                return
        yield event.plain_result("当前没有进行中的对垒。")

    async def _duel_confirm_timeout(self, session_id, msg_origin):
        """对垒确认 2 分钟超时自动取消。"""
        try:
            await asyncio.sleep(120)
            if session_id in self.duel_sessions:
                d = self.duel_sessions[session_id]
                if d.get("state") == "wait_confirm":
                    self.duel_sessions.pop(session_id)
                    if msg_origin:
                        from astrbot.api.all import Plain as _Plain, MessageChain as _MC
                        await self.context.send_message(msg_origin, _MC([
                            _Plain("⏰ 对垒挑战超时（2 分钟未响应），已自动取消。")
                        ]))
        except Exception as e:
            logger.error(f"[poetry_guess] 对垒超时任务异常: {e}")

    async def _handle_duel_message(self, event, msg_raw, is_private, handled):
        """处理诗词对垒相关消息。
        私聊：出题；群聊：确认/猜测。
        async generator：用 yield 发送消息；handled[0]=True 表示消息已被对垒处理。
        不调用 event.stop_event()（会中断生成器），用 event.should_call_llm(True) 阻断默认 LLM。
        """
        uid = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        is_private = is_private or not group_id or group_id == "None"

        # 命令类消息不进入对垒处理
        if msg_raw.startswith("/"):
            return

        # 找到当前用户/群相关的对垒会话
        duel = None
        sid = None
        for k, d in self.duel_sessions.items():
            if group_id and group_id != "None" and group_id == k:
                duel, sid = d, k
                break
            if uid in (d.get("challenger_id"), d.get("opponent_id")):
                duel, sid = d, k
                break
        if not duel:
            return

        is_free_confirm = (
            duel.get("state") == "wait_confirm"
            and not duel.get("opponent_id")
            and not is_private
        )
        is_participant = uid in (duel.get("challenger_id"), duel.get("opponent_id"))
        if not is_participant and not is_free_confirm:
            return

        def _block_llm():
            handled[0] = True
            try:
                event.should_call_llm(True)
            except Exception:
                pass

        # ===== 等待确认阶段（群聊）=====
        if duel["state"] == "wait_confirm":
            if time.time() - duel.get("created_at", 0) > 120:
                self.duel_sessions.pop(sid)
                _block_llm()
                yield event.plain_result("⏰ 对垒挑战超时，已自动取消。")
                return
            opp_id = duel.get("opponent_id")
            if opp_id:
                if uid != opp_id:
                    return
                if msg_raw in ("接受", "同意", "应战"):
                    duel["state"] = "wait_puzzle"
                    fmt_desc = self._format_desc(duel.get("fmt"))
                    hl = "拼音" if duel.get("hint_mode") == "pinyin" else "部首"
                    hint = (f"🍵 【诗词对垒】提示方式：{hl}，格式：{fmt_desc}\n"
                            f"请发送「{fmt_desc}」诗句作为你的题目（总库中，前缀「cc」）。\n"
                            f"单句例：cc 床前明月光 ｜ 两句例：cc 离离原上草，一岁一枯荣")
                    ok_a = await self._send_private(event.bot, duel["challenger_id"], hint)
                    ok_b = await self._send_private(event.bot, duel["opponent_id"], hint)
                    _block_llm()
                    if ok_a and ok_b:
                        yield event.plain_result(f"🍵 对垒开始！已私聊双方提示出题（{fmt_desc}），出题完成后在群聊公开互猜。")
                    else:
                        yield event.plain_result(
                            f"⚠️ 私聊出题失败（机器人需与双方互为好友才能私聊）。\n"
                            f"请先让双方添加机器人为好友，再重新发起对垒。"
                        )
                        self.duel_sessions.pop(sid)
                    return
                elif msg_raw in ("拒绝", "拒绝挑战", "不接受"):
                    self.duel_sessions.pop(sid)
                    _block_llm()
                    yield event.plain_result(f"{duel['opponent_name']} 拒绝了挑战。")
                    return
                return
            else:
                if uid == duel["challenger_id"]:
                    if msg_raw in ("接受", "同意", "应战"):
                        _block_llm()
                        yield event.plain_result("不能挑战自己哦~ 请等待其他成员回复【接受】。")
                    return
                if msg_raw in ("接受", "同意", "应战"):
                    duel["opponent_id"] = uid
                    duel["opponent_name"] = event.get_sender_name() or f"用户{uid}"
                    duel["state"] = "wait_puzzle"
                    fmt_desc = self._format_desc(duel.get("fmt"))
                    hl = "拼音" if duel.get("hint_mode") == "pinyin" else "部首"
                    hint = (f"🍵 【诗词对垒】提示方式：{hl}，格式：{fmt_desc}\n"
                            f"请发送「{fmt_desc}」诗句作为你的题目（总库中，前缀「cc」）。\n"
                            f"单句例：cc 床前明月光 ｜ 两句例：cc 离离原上草，一岁一枯荣")
                    ok_a = await self._send_private(event.bot, duel["challenger_id"], hint)
                    ok_b = await self._send_private(event.bot, uid, hint)
                    _block_llm()
                    if ok_a and ok_b:
                        yield event.plain_result(f"🍵 {event.get_sender_name()} 接受对垒！已私聊双方提示出题（{fmt_desc}），出题完成后在群聊公开互猜。")
                    else:
                        yield event.plain_result(
                            f"⚠️ 私聊出题失败（机器人需与双方互为好友才能私聊）。\n"
                            f"请先让双方添加机器人为好友，再重新发起对垒。"
                        )
                        self.duel_sessions.pop(sid)
                    return
                return

        # ===== 出题阶段（私聊）=====
        if duel["state"] == "wait_puzzle":
            if not is_private:
                return
            if not msg_raw.startswith("cc"):
                _block_llm()
                return
            if uid in duel["puzzle_done"]:
                _block_llm()
                yield event.plain_result("你已经出过题了，等待对方出题中...")
                return
            clean = re.sub(r'^cc\s*', '', msg_raw).strip()
            hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)
            fmt = duel.get("fmt")
            fmt_kind = fmt[0] if fmt else "single"
            if fmt_kind == "single":
                need = fmt[1]
                if len(hanzi) != need:
                    _block_llm()
                    yield event.plain_result(f"题目需为 {need} 字单句，当前 {len(hanzi)} 字。")
                    return
                if not links.base_is_in_library(self.context, clean):
                    _block_llm()
                    yield event.plain_result(f"「{clean}」不在诗词库中，请输入曲库诗句作为题目。")
                    return
            else:
                a_len, b_len = fmt[1]
                segs = re.split(r'[，。！？、；：]', clean)
                segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
                if len(segs) != 2 or len(segs[0]) != a_len or len(segs[1]) != b_len:
                    _block_llm()
                    yield event.plain_result(f"题目需为「{a_len} 字+{b_len} 字」两句（带标点），当前格式不符。")
                    return
                if not self._base_ready() or not links.base_is_adjacent_pair(self.context, segs[0], segs[1]):
                    _block_llm()
                    yield event.plain_result(f"「{clean}」未在诗词库中（两句需为库中某首的相邻两句）。")
                    return
            duel["puzzles"][uid] = clean
            duel["puzzle_done"].add(uid)
            self.pm.record_verse(uid, clean, event.get_sender_name() or f"用户{uid}")
            _block_llm()
            if len(duel["puzzle_done"]) >= 2:
                self._start_duel_playing(duel, sid)
            yield event.plain_result(f"✅ 出题成功！题目：{clean}。等待对方出题...")
            return

        # ===== 猜测阶段（群聊）=====
        if duel["state"] == "playing":
            engine = duel["engine"]
            if engine is None:
                return
            if is_private:
                _block_llm()
                return
            if not engine.is_turn(uid):
                _block_llm()
                return
            if not msg_raw.startswith("cc"):
                _block_llm()
                return
            clean = re.sub(r'^cc\s*', '', msg_raw).strip()
            _block_llm()
            links.hub_record_plugin_use(self.context, uid, event.get_sender_name() or f"用户{uid}")
            result = self._apply_duel_guess(duel, sid, uid, event.get_sender_name() or f"用户{uid}", clean)
            if not result["ok"]:
                yield event.plain_result(result["err"])
                return
            for kind, payload in result["msgs"]:
                if kind == "text":
                    yield event.plain_result(payload)
                else:
                    yield event.image_result(payload)
            # 乐不思蜀（跳过）/ 请君入梦（代猜）自动推进
            auto_step = 0
            while (not result.get("finished")) and sid in self.duel_sessions:
                auto_step += 1
                if auto_step > 6:
                    break
                engine = duel.get("engine")
                if not engine:
                    break
                eff = duel.get("item_effects", {})
                cur = engine.current_side()
                cur_uid = engine.a_id if cur == "a" else engine.b_id
                if eff.get("skip", {}).get(cur, 0) > 0:
                    eff["skip"][cur] -= 1
                    engine.switch_turn()
                    yield event.plain_result(f"😴 对方被【乐不思蜀】跳过一回合，轮到 {engine.current_name()}。")
                    continue
                if eff.get("dream", {}).get(cur, 0) > 0:
                    eff["dream"][cur] -= 1
                    guess = self._pick_auto_guess(engine, cur)
                    if not guess:
                        break
                    r2 = self._apply_duel_guess(duel, sid, cur_uid, self._uid_name(cur_uid), guess)
                    for kind, payload in r2.get("msgs", []):
                        if kind == "text":
                            yield event.plain_result(payload)
                        else:
                            yield event.image_result(payload)
                    result = r2
                    continue
                break
            return

    def _pick_auto_guess(self, engine, side):
        """请君入梦：随机给 side 方挑一句他要猜的目标句（同格式）。"""
        import random as _r
        target_hanzi = engine.a_target_hanzi if side == "a" else engine.b_target_hanzi
        target_punct = engine.a_target_punct if side == "a" else engine.b_target_punct
        if not target_punct:
            n = len(target_hanzi)
            clause_set = set()
            for p in links.base_classic_poems(self.context):
                sent = p.get("sentence", "")
                for clause in re.split(r'[，、]', sent):
                    pure = re.sub(r'[^\u4e00-\u9fff]', '', clause)
                    if 4 <= len(pure) <= 7:
                        clause_set.add(pure)
            cands = [h for h in clause_set if len(h) == n]
            if cands:
                return _r.choice(cands)
            if self._base_ready():
                try:
                    rows = links.base_get_random_verse(self.context, n, n, target_count=20, max_scan=300)
                    if rows:
                        return rows[0][0]
                except Exception:
                    pass
            return None
        segs = []
        cur = 0
        for pos, _p in target_punct:
            if pos >= len(target_hanzi):
                continue
            segs.append(pos - cur)
            cur = pos
        segs.append(len(target_hanzi) - cur)
        if len(segs) == 2 and self._base_ready():
            try:
                rows = links.base_get_random_verse_by_combo(self.context, segs[0], segs[1], target_count=10, max_scan=300)
                if rows:
                    return rows[0][0]
            except Exception:
                pass
        return None

    def _start_duel_playing(self, duel, sid):
        """双方出题完成，进入互猜阶段。群通知 + 出题关系成就主动发送。返回 engine。"""
        a_id = duel["challenger_id"]
        b_id = duel["opponent_id"]
        engine = DuelVerseEngine(
            duel["puzzles"][a_id], duel["puzzles"][b_id],
            a_id, duel["challenger_name"], b_id, duel["opponent_name"],
        )
        duel["engine"] = engine
        duel["state"] = "playing"
        duel["item_effects"] = {"skip": {}, "immune": {}, "gamble": {}, "dream": {}}
        soulmate_msgs = self._check_soulmate(duel, a_id, b_id)
        puzzle_msgs = self._check_duel_puzzle_achievements(duel, a_id, b_id)
        origin = duel.get("group_origin")
        if origin:
            lines = [
                "🍵 双方已出题！开始互猜！",
                f"{duel['challenger_name']} 猜 {duel['opponent_name']} 的题，{duel['opponent_name']} 猜 {duel['challenger_name']} 的题。",
                f"先轮到：{engine.current_name()}（发送「cc 诗句」猜测）",
            ]
            lines += soulmate_msgs + puzzle_msgs
            try:
                from astrbot.api.all import Plain as _Plain, MessageChain as _MC
                asyncio.create_task(self.context.send_message(origin, _MC([_Plain("\n".join(lines))])))
            except Exception as e:
                logger.error(f"[poetry_guess] 对垒开局群通知失败: {e}")
        return engine

    def _check_soulmate(self, duel, a_id, b_id):
        """心有灵犀：双方所出诗句是否出自同一首诗词。返回提示消息列表。"""
        msgs = []
        a_puzzle = duel.get("puzzles", {}).get(a_id, "")
        b_puzzle = duel.get("puzzles", {}).get(b_id, "")
        if not self._base_ready() or not a_puzzle or not b_puzzle:
            return msgs
        try:
            a_titles = set(self._poem_titles_of(a_puzzle))
            b_titles = set(self._poem_titles_of(b_puzzle))
            if a_titles & b_titles:
                for p in (a_id, b_id):
                    if self.pm.unlock_achievement(p, "soulmate", self._uid_name(p)):
                        msgs.append(self._achieve_msg(p, "soulmate"))
        except Exception:
            pass
        return msgs

    def _check_duel_puzzle_achievements(self, duel, a_id, b_id):
        """对垒出题关系成就：双方题目的作者/朝代/相同字/月花酒山江。返回提示消息列表。"""
        msgs = []
        a_puzzle = duel.get("puzzles", {}).get(a_id, "")
        b_puzzle = duel.get("puzzles", {}).get(b_id, "")
        if not a_puzzle or not b_puzzle:
            return msgs
        a_hanzi = set(extract_hanzi(a_puzzle))
        b_hanzi = set(extract_hanzi(b_puzzle))
        if a_hanzi & b_hanzi:
            for p in (a_id, b_id):
                if self.pm.unlock_achievement(p, "duel_common_char", self._uid_name(p)):
                    msgs.append(self._achieve_msg(p, "duel_common_char"))
        both_char_map = {
            "月": "duel_both_moon", "花": "duel_both_flower", "酒": "duel_both_wine",
            "山": "duel_both_mountain", "江": "duel_both_river",
        }
        for ch, ach in both_char_map.items():
            if ch in a_hanzi and ch in b_hanzi:
                for p in (a_id, b_id):
                    if self.pm.unlock_achievement(p, ach, self._uid_name(p)):
                        msgs.append(self._achieve_msg(p, ach))
        if self._base_ready():
            try:
                meta_a = links.base_check_exact_poetry(self.context, a_puzzle)
                meta_b = links.base_check_exact_poetry(self.context, b_puzzle)
                if meta_a and meta_b:
                    _, author_a, dynasty_a = meta_a
                    _, author_b, dynasty_b = meta_b
                    if author_a and author_b and author_a != "佚名" and author_b != "佚名" and author_a == author_b:
                        for p in (a_id, b_id):
                            if self.pm.unlock_achievement(p, "duel_same_author", self._uid_name(p)):
                                msgs.append(self._achieve_msg(p, "duel_same_author"))
                    if dynasty_a and dynasty_b and dynasty_a == dynasty_b:
                        for p in (a_id, b_id):
                            if self.pm.unlock_achievement(p, "duel_same_dynasty", self._uid_name(p)):
                                msgs.append(self._achieve_msg(p, "duel_same_dynasty"))
            except Exception:
                pass
        return msgs

    def _apply_duel_guess(self, duel, sid, uid, uname, clean):
        """执行对垒猜测的核心逻辑（库校验 + 状态推进 + 结算 + 渲染）。返回结果 dict，不发送消息。"""
        msgs = []
        engine = duel["engine"]
        eff = duel.setdefault("item_effects", {"skip": {}, "immune": {}, "gamble": {}, "dream": {}})
        hanzi = re.sub(r'[^\u4e00-\u9fff]', '', clean)
        side = "a" if uid == engine.a_id else "b"
        target_punct = engine.a_target_punct if side == "a" else engine.b_target_punct
        target_len = len(engine.a_target_hanzi if side == "a" else engine.b_target_hanzi)

        def _fail(err):
            return {"ok": False, "err": err, "comp": None, "all_correct": False,
                    "finished": False, "msgs": msgs}

        if not hanzi or len(hanzi) != target_len:
            return _fail(f"字数不符（需 {target_len} 字）")
        if target_punct:
            segs = re.split(r'[，。！？、；：]', clean)
            segs = [re.sub(r'[^\u4e00-\u9fff]', '', s) for s in segs if re.sub(r'[^\u4e00-\u9fff]', '', s)]
            if len(segs) != 2 or not self._base_ready() or not links.base_is_adjacent_pair(self.context, segs[0], segs[1]):
                return _fail(f"「{clean}」未在诗词库中（需为库中相邻两句）")
        else:
            if not links.base_is_in_library(self.context, clean):
                return _fail(f"「{clean}」不在诗词库中，请输入曲库诗句")
        ok, err, side, comp, all_correct = engine.guess(uid, clean)
        if not ok:
            return _fail(err)
        added = self.pm.record_verse(uid, clean, uname)
        self.pm.inc_stat(uid, "total_guesses", 1, uname)
        if added > 0 and uid != BOT_ID:
            _hit, _tip, _ach = self._roll_draw(uid, uname)
            if _hit:
                msgs.append(("text", f"✨ {uname} 使用新诗句，触发抽道具！{_tip}"))
            if _ach:
                msgs.append(("text", _ach))
        # 🐖 重复诗句检测（本局内自己发过的纯汉字）
        if "user_verses" not in duel:
            duel["user_verses"] = {}
        duel["user_verses"].setdefault(uid, set())
        if hanzi in duel["user_verses"][uid]:
            pig_count = self.pm.add_pig(uid, uname)
            msgs.append(("text", f"🐖 {uname} 重复诗句！猪+1（{'🐖' * pig_count}）"))
        else:
            duel["user_verses"][uid].add(hanzi)
        duel["guess_counts"][uid] = duel["guess_counts"].get(uid, 0) + 1
        # 追踪本局声母/韵母使用
        if "user_initials" not in duel:
            duel["user_initials"] = {}
            duel["user_finals"] = {}
        duel["user_initials"].setdefault(uid, set())
        duel["user_finals"].setdefault(uid, set())
        side_parts = []
        if side == "a" and engine.a_history:
            side_parts = engine.a_history[-1][1]
        elif side == "b" and engine.b_history:
            side_parts = engine.b_history[-1][1]
        for gp in side_parts:
            if gp.get("initial"):
                duel["user_initials"][uid].add(gp["initial"])
            if gp.get("final"):
                duel["user_finals"][uid].add(gp["final"])
        # 一事无成
        if comp and all(
            c is not None and c.get("char") == "absent"
            and c.get("initial") == "absent" and c.get("final") == "absent"
            for c in comp
        ):
            if self.pm.unlock_achievement(uid, "all_gray", uname):
                msgs.append(("text", self._achieve_msg(uid, "all_gray")))
        # 旗开得胜
        if len(engine.a_history) + len(engine.b_history) == 1:
            has_char = any(c is not None and c.get("char") in ("correct", "present") for c in comp)
            has_pinyin = any(
                c is not None and c.get("initial") in ("correct", "present")
                and c.get("final") in ("correct", "present")
                for c in comp
            )
            if has_char or has_pinyin:
                if self.pm.unlock_achievement(uid, "first_hit_char", uname):
                    msgs.append(("text", self._achieve_msg(uid, "first_hit_char")))
        img_path = os.path.join(self.data_dir, f"duel_{sid}.png")
        render_duel(engine, img_path, hint_mode=duel.get("hint_mode", "pinyin"))
        msgs.append(("image", img_path))
        finished = False
        immune_opp = "b" if side == "a" else "a"
        if all_correct and eff["immune"].get(immune_opp, 0) > 0:
            # 百战不殆：被猜中方免疫一次，不结束，给其反杀回合
            eff["immune"][immune_opp] -= 1
            engine.winner = None
            left = eff["immune"].get(immune_opp, 0)
            msgs.append(("text", f"🛡 对方使用【百战不殆】免疫了这次命中！（剩余 {left} 次）"))
            engine.switch_turn()
            msgs.append(("text", f"轮到 {engine.current_name()}。"))
        elif all_correct:
            wname = engine.side_name(side)
            win_text = (
                f"🏆 {wname} 猜中了对方的诗句！\n"
                f"{engine.a_name} 的题：{engine.a_puzzle}\n"
                f"{engine.b_name} 的题：{engine.b_puzzle}"
            )
            for m in self._settle_duel_achievements(duel, engine, side, uid):
                win_text += "\n" + m
            msgs.append(("text", win_text))
            self.duel_sessions.pop(sid, None)
            finished = True
        else:
            # 孤注一掷：出手方连续多次未命中
            gamble = eff["gamble"].get(side)
            if gamble and gamble.get("active"):
                left = int(gamble.get("left", 0)) - 1
                if left <= 0:
                    eff["gamble"][side]["active"] = False
                    eff["gamble"][side]["left"] = 0
                    loser_name = self._uid_name(uid)
                    win_name = self._uid_name(engine.a_id if side == "b" else engine.b_id)
                    msgs.append(("text", f"🎲 【孤注一掷】耗尽仍未猜中，{loser_name} 判定失败，{win_name} 获胜！"))
                    self.duel_sessions.pop(sid, None)
                    finished = True
                else:
                    eff["gamble"][side]["left"] = left
                    msgs.append(("text", f"🎲 【孤注一掷】继续你的回合（还剩 {left} 次）。"))
                if finished:
                    return {"ok": True, "err": None, "comp": comp, "all_correct": False,
                            "finished": True, "msgs": msgs}
            else:
                engine.switch_turn()
                gc = duel.get("guess_counts", {})
                if gc.get(engine.a_id, 0) >= 20 and gc.get(engine.b_id, 0) >= 20:
                    for p in (engine.a_id, engine.b_id):
                        if self.pm.unlock_achievement(p, "too_dark", self._uid_name(p)):
                            msgs.append(("text", self._achieve_msg(p, "too_dark")))
                msgs.append(("text", f"轮到 {engine.current_name()}。"))
        return {"ok": True, "err": None, "comp": comp, "all_correct": all_correct,
                "finished": finished, "msgs": msgs}

    def _settle_duel_achievements(self, duel, engine, winner_side, winner_uid):
        """对垒分出胜负后结算成就。返回提示消息列表。"""
        msgs = []
        a_id, b_id = engine.a_id, engine.b_id
        loser_id = b_id if winner_side == "a" else a_id
        gc = duel.get("guess_counts", {})
        win_guesses = gc.get(str(winner_uid), 0)
        pm = self.pm

        pm.inc_stat(winner_uid, "duel_wins", 1, self._uid_name(winner_uid))
        pm.inc_stat(a_id, "duel_games", 1, self._uid_name(a_id))
        pm.inc_stat(b_id, "duel_games", 1, self._uid_name(b_id))
        h = time.localtime().tm_hour
        for p in (a_id, b_id):
            ach = None
            if h >= 23 or h < 5:
                ach = "night_owl"
            elif 5 <= h < 8:
                ach = "early_bird"
            if ach and pm.unlock_achievement(p, ach, self._uid_name(p)):
                msgs.append(self._achieve_msg(p, ach))
        if h >= 23 or h < 5:
            for p in (a_id, b_id):
                if pm.unlock_achievement(p, "night_group", self._uid_name(p)):
                    msgs.append(self._achieve_msg(p, "night_group"))
        win_name = self._uid_name(winner_uid)
        win_initials = duel.get("user_initials", {}).get(winner_uid, set())
        win_finals = duel.get("user_finals", {}).get(winner_uid, set())
        if win_initials >= set(INITIALS_LIST):
            if pm.unlock_achievement(winner_uid, "all_initials", win_name):
                msgs.append(self._achieve_msg(winner_uid, "all_initials"))
        if win_finals >= set(FINALS_LIST):
            if pm.unlock_achievement(winner_uid, "all_finals", win_name):
                msgs.append(self._achieve_msg(winner_uid, "all_finals"))
        if win_guesses <= 10:
            if pm.unlock_achievement(winner_uid, "duel_speed", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_speed"))
        if win_guesses <= 5:
            if pm.unlock_achievement(winner_uid, "duel_open", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_open"))
        if winner_side == "a" and pm.unlock_achievement(a_id, "first_mover", self._uid_name(a_id)):
            msgs.append(self._achieve_msg(a_id, "first_mover"))
        elif winner_side == "b" and pm.unlock_achievement(b_id, "second_mover", self._uid_name(b_id)):
            msgs.append(self._achieve_msg(b_id, "second_mover"))
        win_stats = pm.load(winner_uid).get("stats", {})
        dw = win_stats.get("duel_wins", 0)
        if dw >= 5:
            if pm.unlock_achievement(winner_uid, "duel_win_5", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_win_5"))
        if dw >= 10:
            if pm.unlock_achievement(winner_uid, "duel_win_10", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "duel_win_10"))
        # 连胜追踪：胜者连胜 +1，败者清零（记录历史最高连胜）
        cur_streak = int(win_stats.get("duel_streak", 0)) + 1
        pm.set_stat(winner_uid, "max_duel_streak", max(int(win_stats.get("max_duel_streak", 0)), cur_streak))
        new_streak = pm.check_duel_streak(winner_uid, cur_streak, self._uid_name(winner_uid))
        pm.set_stat(winner_uid, "duel_streak", cur_streak)
        if new_streak:
            msgs.append(f"🏆 {self._uid_name(winner_uid)} 达成成就「{new_streak}」！")
        pm.set_stat(loser_id, "duel_streak", 0)
        # 复仇者：上局输给 loser_id，本局作为 winner_uid 赢回
        prev = pm.load(winner_uid).get("stats", {}).get("last_duel_lost_to")
        if prev and str(prev) == str(loser_id):
            if pm.unlock_achievement(winner_uid, "avenger", self._uid_name(winner_uid)):
                msgs.append(self._achieve_msg(winner_uid, "avenger"))
        pm._set_last_lost_to(loser_id, winner_uid)
        for p in (a_id, b_id):
            beloved = pm.check_beloved_verse(p, self._uid_name(p))
            if beloved:
                bv, bc = beloved
                msgs.append(f"🏆 {self._uid_name(p)} 达成成就「挚爱诗句-{bv}」！（使用 {bc} 次）")
        # 道具掉落：胜者按猜测次数概率，败者固定 5%
        win_item = roll_win_item(win_guesses, "duel")
        if win_item:
            pm.add_item(winner_uid, win_item, 1, self._uid_name(winner_uid))
            msgs.append(f"🎁 {self._uid_name(winner_uid)} 获得道具【{win_item}】！")
        loser_item = roll_loser_item("duel")
        if loser_item:
            pm.add_item(loser_id, loser_item, 1, self._uid_name(loser_id))
            msgs.append(f"🎁 {self._uid_name(loser_id)} 获得道具【{loser_item}】！")
        return msgs


class _BaseDBSource:
    """GuessVerseEngine.db_source 适配：转发到诗词底座。"""

    def __init__(self, context):
        self._context = context

    @property
    def db_path(self):
        return links.base_db_path(self._context)

    def get_random_verse(self, *a, **kw):
        return links.base_get_random_verse(self._context, *a, **kw)

    def get_random_verse_by_combo(self, *a, **kw):
        return links.base_get_random_verse_by_combo(self._context, *a, **kw)
