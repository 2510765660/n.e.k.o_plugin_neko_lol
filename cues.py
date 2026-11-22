"""把对局事件翻译成"给猫娘的一句话提示"。

设计要点（照抄 ``neko_survivors`` / ``neko_warthunder`` 的成熟做法）：

* 提示语是**给模型的上下文**，不是直接对用户说的话。所以统一用括号
  包起来，并明确要求"用你自己的口吻说一句"。少了这层包装，模型会把
  提示语当成用户发言，开始自问自答。
* 提示语里所有事实都写成陈述句，并显式禁止模型脑补："只能依据括号里
  给出的事实，不要编造没发生的事"。游戏播报最忌讳模型自己加戏。
* 英雄名：国服接口返回的是**中文官方称号**（「虚空恐惧」「诺克萨斯之手」），
  啰嗦、不像人话。提示语里明确要求模型换成玩家平时的叫法（「大虫子」
  「诺手」）；直出文本则靠可选的 ``champion_aliases.json`` 覆盖。国际服
  若返回英文，同一套机制也能把英文换成中文常用名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .game_model import RESULT_LABELS, MatchState

# 事件 → (节流配置 key, 优先级)。优先级越大越"值得打断"。
# key 同时对应 plugin.toml 里的 [neko_lol.announce] 和面板上的开关。
EVENT_KEYS: dict[str, str] = {
    "GameStart": "game_start",
    "FirstBlood": "first_blood",
    "ChampionKill": "champion_kill",
    "Multikill": "multikill",
    "Ace": "ace",
    "DragonKill": "dragon_kill",
    "BaronKill": "baron_kill",
    "HeraldKill": "herald_kill",
    "TurretKilled": "turret_killed",
    "InhibKilled": "inhib_killed",
    "GameEnd": "game_end",
}

# 播报时**不**附带"主人现在 X—Y—Z，N 级"这类战绩的事件。
#
#   * game_start —— 那一刻战绩必然是 0—0—0、1 级，纯噪音。
#   * game_end   —— 它 5 秒后就跟一条带完整战绩的赛后总结，重复。
#   * 先锋 / 推塔 / 兵营 —— 环境档，量大、多是背景进展（"敌方拆了座塔"），
#     播报越短越好；挂一行战绩只会让这类话变长。
#   * 一血 / 小龙 / 大龙 —— 2026-11-17 用户指定去掉。这几个时刻本身已经
#     够清楚，再跟一行战绩是多余的尾巴（早先这几类**是**带的）。
#
# 所以现在只有「团灭」这一种事件播报带战绩行；另有定期小结和赛后总结
# 自带的整块战况，它们不走这里。
_NO_STATS_KEYS: frozenset[str] = frozenset(
    {
        "game_start", "game_end",
        "herald_kill", "turret_killed", "inhib_killed",
        "first_blood", "dragon_kill", "baron_kill",
    }
)

# 播报时**不**带"对局时间"的事件（2026-11-18 用户指定）。
# 这些事件本身已经说清了"发生了什么"，再挂一个时刻只是噪音；
# 而且模型几乎不会把时间念出来（实测 40 条 LOL 回复里只有 2 条提时间），
# 带着也只是白占提示语的篇幅。
#
# 仍然带时间的：一血、对局结束（这两个"什么时候发生"有信息量），
# 以及定期小结 / 赛后总结（时间本来就是它们的正文）。
_NO_TIME_KEYS: frozenset[str] = frozenset(
    {
        "game_start",
        "ace",
        "baron_kill",
        "dragon_kill",
        "herald_kill",
        "turret_killed",
        "inhib_killed",
        "multikill",
    }
)

PRIORITIES: dict[str, int] = {
    "game_end": 90,
    "ace": 80,
    "baron_kill": 70,
    "multikill": 65,
    "first_blood": 60,
    "champion_kill": 55,
    "my_death": 50,
    "my_assist": 25,
    "milestone": 15,
    "dragon_kill": 45,
    "herald_kill": 40,
    "inhib_killed": 35,
    "game_start": 30,
    "turret_killed": 20,
    "summary": 10,
}

_DRAGON_TYPES: dict[str, str] = {
    "Fire": "火龙",
    "Water": "水龙",
    "Earth": "土龙",
    "Air": "风龙",
    "Hextech": "海克斯龙",
    "Chemtech": "炼金龙",
    "Elder": "远古巨龙",
}

# ── 播报档位 ────────────────────────────────────────────────────
# 0 = 关键：永远立刻说，间隔再密也不等（超神/团灭/大龙/赛后）
# 1 = 常规：立刻说，但和上一条至少隔 normal_gap 秒
# 2 = 环境：进窗口合并，安静下来一起说（推塔/兵营/先锋/小结）
TIER_CRITICAL = 0
TIER_NORMAL = 1
TIER_AMBIENT = 2

_KEY_TIERS: dict[str, int] = {
    # 关键
    "game_end": TIER_CRITICAL,
    "ace": TIER_CRITICAL,
    "baron_kill": TIER_CRITICAL,
    # 常规
    "game_start": TIER_NORMAL,
    "first_blood": TIER_NORMAL,
    "champion_kill": TIER_NORMAL,
    "my_death": TIER_NORMAL,
    "multikill": TIER_NORMAL,
    "dragon_kill": TIER_NORMAL,
    # 环境
    "my_assist": TIER_AMBIENT,
    "milestone": TIER_AMBIENT,
    "herald_kill": TIER_AMBIENT,
    "turret_killed": TIER_AMBIENT,
    "inhib_killed": TIER_AMBIENT,
    "summary": TIER_AMBIENT,
}


def tier_for(key: str) -> int:
    """按事件 key 查档位。没配的用常规档。"""
    return _KEY_TIERS.get(key, TIER_NORMAL)


# 连续击杀达到这个数就是"超神"（死一次清零）。
LEGENDARY_STREAK = 8


# 龙魂的中文名。官方客户端是"XX的龙魂"，口语里常简称"XX龙魂"。
# 这里用口语简称 —— 播报是口语场景，念"火龙魂"比"火龙的龙魂"顺。
# 显式列出来而不是用 f"{cn}魂" 硬拼：以后加新龙不会因为拼法出错。
_DRAGON_SOUL_NAMES: dict[str, str] = {
    "Fire": "火龙魂",
    "Water": "水龙魂",
    "Earth": "土龙魂",
    "Air": "风龙魂",
    "Hextech": "海克斯龙魂",
    "Chemtech": "炼金龙魂",
}


def _spaced(name: str) -> str:
    """英文名**前面**补一个空格，中文名不补。

    中英混排时 ``对位的是对面的Darius`` 读起来黏在一起；
    而 ``对位的是对面的亚索`` 中间不该插空格。
    """
    text = str(name or "")
    if text and text[0].isascii() and text[0].isalpha():
        return " " + text
    return text


def _soul_name(dragon_cn: str) -> str:
    """中文龙名 → 龙魂名。查不到就退回"龙魂"。"""
    for raw, cn in _DRAGON_TYPES.items():
        if cn == dragon_cn:
            return _DRAGON_SOUL_NAMES.get(raw, "龙魂")
    return "龙魂"


# 结构名里的队伍标记。国服实测用 ``TChaos`` / ``TOrder``；国际服常见
# ``T1`` / ``T2``。两套都要认 —— 只认一套会让另一套原样漏进播报
# （旧版就出现过"TChaos防御塔被推掉了"）。
_TEAM_TAGS = {
    "T1": "蓝色方", "T2": "红色方",
    "TORDER": "蓝色方", "TCHAOS": "红色方",
}
# 国服实测的塔位编码（两局对照确认）：
#   L0 = 下路, L1 = 中路, L2 = 上路
# 注意这不是国际服的 "L/R/C" 那套 —— 早期按后者写的映射是错的，
# 实测日志里出现过 Turret_TChaos_L0_P3_... 这种格式。
_LANE_TAGS = {
    "L0": "下路", "L1": "中路", "L2": "上路",
    "L": "上路", "C": "中路", "R": "下路",   # 国际服旧格式，保留兼容
    "A": "基地",
}

# P 值 = 该路上的位置序号（从外往里）。
#   P3 = 外塔, P2 = 二塔, P1 = 高地塔, P5/P4 = 门牙塔
_TIER_TAGS = {
    "P3": "外塔", "P2": "二塔", "P1": "高地塔",
    "P5": "门牙塔", "P4": "门牙塔",
    "01": "外塔", "02": "二塔", "03": "高地塔",   # 国际服旧格式
}

# 提示语的固定尾巴：约束模型不要复述格式、不要编造。
def _speak_instruction(
    lead: str = "对主人说一句话（一到两句），像坐在旁边一起看的朋友那样",
) -> str:
    """给模型的收尾指令。``lead`` 是"用你自己的口吻…"那半句，按场景替换。

    早先 summary 自己写了一句"用你自己的口吻点评…"又拼通用尾巴，
    结果"用你自己的口吻"出现两次。
    """
    return (
        f"用你自己的口吻{lead}。"
        "英雄名别照搬上面给的官方全称，用玩家平时的叫法"
        "（「德玛西亚之力」说盖伦）；"
        "给的是英文名就说中文的常用叫法。"
        "只依据上面给出的事实说话，不要编造没写出来的细节，"
        "也不要复述括号里的格式。"
    )


_SPEAK_INSTRUCTION = _speak_instruction()

# 战绩数字的格式说明。少了这句，模型会把 "3—1—2" 当成一个整体，
# 或者按斜杠/日期的习惯去理解，播报时读错顺序。
# 战绩数字的格式说明。少了这句，模型会把 "3—1—2" 当成一个整体，
# 或者按斜杠/日期的习惯去理解，播报时读错顺序。
# 除了说清顺序，还要明确"中间的横线念成杠" —— 否则模型可能念成
# "三到一"、"三减一"，或者干脆跳过不念。
_KDA_LEGEND = (
    "（战绩数字的格式是「击杀—死亡—助攻」，中间的横线念成「杠」，"
    "例如 3—1—2 念作「三杠一杠二」。）"
)


@dataclass
class Cue:
    """一条待播报的内容。"""

    key: str
    text: str
    priority: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    # 未包装的正文（只有事件类 cue 有）。窗口合并要把多条 cue 拼成一句，
    # 拼接时必须用这个而不是 text —— text 里带着"主人正在玩英雄联盟，
    # 对局时间 xx。"那层包装，拼起来会变成套娃。
    body: str = ""
    # 播报档位，决定 Narrator 怎么投递：
    #   0 = 关键（永远立刻，可抢占）
    #   1 = 常规（立刻，但与上一条至少隔 normal_gap 秒）
    #   2 = 环境（进窗口合并，倒计时结束再说）
    tier: int = 1
    # 这条 cue 的事件里有没有助攻数据。合并播报要用它决定要不要补上
    # "助攻只作背景"的约束（单条提示语是自己拼的，合并走的是另一条路）。
    has_assists: bool = False
    # 这条 cue 的提示语里是否真的带了战绩行。调用方用它决定要不要更新
    # 战绩冷却的计时 —— 不能只看"允许带"，否则开局这种本来就不带战绩的
    # 事件也会把冷却重置一遍（实测：开局把冷却重置，导致第一条小龙没战绩）。
    has_stats: bool = False
    # 是否允许被并进"刚才发生了这些事"的窗口。
    # 定期小结说的是"此刻状态"而不是"刚发生的事"，且它的 text 里带着完整
    # 战况列表；一旦被合并，body 只留得下一行比分，信息全丢。所以它不走
    # 合并通道，单独发。
    mergeable: bool = True


def _localize_champion(name: str, champion_names: dict[str, str]) -> str:
    """按覆盖表换个叫法（表里没有就原样返回）。"""
    return champion_names.get(name, name) or name


def _localize_names(text: str, champion_names: dict[str, str]) -> str:
    """把一句话里出现的英雄名替换成中文。用于直出文本（不经模型）。"""
    if not champion_names:
        return text
    for english, chinese in champion_names.items():
        if english and english in text:
            text = text.replace(english, chinese)
    return text


def _parse_structure(raw: Any) -> dict[str, str]:
    """拆解 ``Turret_T1_C_01_A`` 这类结构名。

    格式是 ``<类型>_<队伍>_<路>_<序号>_<位置>``，各段在不同版本里
    可能缺失或多出，所以按下标取、取不到就留空，不抛异常。
    """
    parts = str(raw or "").strip().split("_")
    team_tag = parts[1] if len(parts) > 1 else ""
    lane_tag = parts[2] if len(parts) > 2 else ""
    tier_tag = parts[3] if len(parts) > 3 else ""
    return {
        "team": _TEAM_TAGS.get(team_tag, ""),
        "lane": _LANE_TAGS.get(lane_tag, ""),
        "tier": _TIER_TAGS.get(tier_tag, ""),
        # 认不出来时保留原始 tag，方便日志里排查
        "raw_team": team_tag,
    }


def _turret_label(raw: Any, *, is_aram: bool = False) -> str:
    """``Turret_T1_C_01_A`` → ``中路外塔``。

    只描述**塔本身**（哪条路、哪一层）；"哪一方的"和"谁拆的"由调用方
    结合 KillName 拼，避免这里同时承担两件事。

    ``is_aram=True`` 时**不输出路名** —— 嚎哭深渊只有一条路，
    "上路/中路/下路"在那里没有意义，说出来还会误导模型。
    """
    info = _parse_structure(raw)
    lane = info["lane"]
    tier = info["tier"]
    if not lane and not tier:
        return "一座防御塔"
    # 门牙塔不属于任何一条路 —— 它守在基地门口，说"中路门牙塔"是错的。
    if tier == "门牙塔":
        return "门牙塔"
    if is_aram:
        # tier 自带"塔"字（外塔/二塔/高地塔），别再拼"防御塔"。
        return tier if tier else "一座防御塔"
    return f"{lane}{tier}" if lane and tier else (lane or tier) + "防御塔"


def _inhib_label(raw: Any, *, is_aram: bool = False) -> str:
    """``Inhib_T1_C_01_A`` → ``中路兵营``。

    和塔一样只描述位置；"哪一方的"由调用方拼 —— 结构名里的队伍 tag
    是绝对阵营（蓝/红），而玩家关心的是"我们的/对面的"。

    ``is_aram=True`` 时不输出路名（嚎哭深渊只有一条路）。
    """
    info = _parse_structure(raw)
    lane = info["lane"]
    if is_aram:
        return "兵营"
    return f"{lane}兵营" if lane else "一座兵营"


def _side_from_team_tag(tag: str, state: MatchState) -> str:
    """``T1``/``T2`` 这类队伍 tag → ``"ally"`` / ``"enemy"`` / ``"unknown"``。

    不能直接拿 ``_TEAM_TAGS`` 比中文名：主人的队伍可能是 ORDER 也可能是
    CHAOS（蓝方红方随机），必须和 ``state.me.team`` 对齐才能判对边。
    """
    if state.me is None:
        return "unknown"
    my_team = state.me.team  # "ORDER" / "CHAOS"
    tag = str(tag or "").strip().upper()
    # T1/TOrder 都是蓝色方(ORDER)，T2/TChaos 都是红色方(CHAOS)。
    if tag in ("T1", "ORDER", "TORDER"):
        return "ally" if my_team == "ORDER" else "enemy"
    if tag in ("T2", "CHAOS", "TCHAOS"):
        return "ally" if my_team == "CHAOS" else "enemy"
    return "unknown"


def _killer_phrase(name: str, state: MatchState) -> str:
    """拆塔者 → **主人 / 队友 / 敌方**。

    只需要说清"是哪一边的人拆的"，英雄名反而是噪音（"队友Lulu 拆掉了…"
    读起来啰嗦）。身份靠 ``state.side_of`` 判断 —— 那是开局第一份快照
    建立的 ID → 阵营映射（``allPlayers`` 里每个人都带 ``team``）。
    """
    if not name:
        return ""
    if state.is_me(name):
        return "主人"
    side = state.side_of(name)
    if side == "ally":
        return "队友"
    if side == "enemy":
        return "敌方"
    # 阵容里查不到这个人时，先看是不是小兵/防御塔这类非英雄单位。
    # 实测 2026-11-16：三座塔的击杀者是认不出的名字，播成"有人拆掉了…"。
    # 塔被打掉多半是小兵在磨，说"小兵拆掉了"比"有人"有用得多。
    unit = _non_champion_killer(name)
    if unit:
        return unit
    # 实在认不出（字段异常）时说"有人"，别瞎编阵营。
    return "有人"


# 拿到第几条龙时该提一句。3 = 即将获得龙魂，4 = 已经获得龙魂。
_SOUL_WARN_AT = 3
_SOUL_GRANTED_AT = 4


def _soul_note(
    side: str,
    dragon_counts: dict[str, int] | None,
    dragon_type: str,
    soul_type: str | None,
) -> str:
    """第三条 / 第四条小龙的龙魂提醒。

    龙魂机制（已确认）：**本局第一种小龙的属性就是本局龙魂；某方累计
    击杀 4 条小龙即获得它**。Riot 规则保证一局里前三条龙属性相同，所以
    本局第 3 条被击败的小龙，其属性即本局龙魂，与谁拿的无关。

    * 第 3 条：提醒"再来一条就是 X 龙魂"。
    * 第 4 条：宣告"X 龙魂到手了"。

    ``dragon_counts`` 按阵营累计条数，``soul_type`` 是本局龙魂属性
    （插件在第一条非远古龙出现时记下）。
    """
    if not dragon_counts or side not in ("ally", "enemy"):
        return ""
    count = int(dragon_counts.get(side, 0))
    who = "主人这边" if side == "ally" else "对面"
    if "远古" in dragon_type:
        return ""   # 远古巨龙不参与龙魂

    # 优先用本局记下的龙魂属性；还没记到就退回当前这条（不会发生，
    # 但保证兜底不会输出半句话）。
    soul = _soul_name(soul_type or dragon_type)

    if count == _SOUL_WARN_AT:
        return f"{who}已经三条龙了，再来一条就是{soul}"
    if count == _SOUL_GRANTED_AT:
        return f"{who}拿到第四条，{soul}到手了"
    return ""


def _stolen_suffix(event: dict) -> str:
    """抢龙标记。API 给的是字符串 ``"True"``/``"False"``。"""
    raw = str(event.get("Stolen") or "").strip().lower()
    return "（是抢的！）" if raw == "true" else ""


def _i_am_assister(event: dict, state: MatchState) -> bool:
    """主人在不在 ``Assisters`` 里。"""
    raw = event.get("Assisters")
    if not isinstance(raw, list):
        return False
    return any(state.is_me(str(a).strip()) for a in raw)


def _assist_names(
    event: dict,
    state: MatchState,
    champion_names: dict[str, str],
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    """把 ``Assisters`` 翻译成可以直接念的英雄名列表。

    ``Assisters`` 是玩家名数组（Riot 官方 schema）。国服会不会填、填的是
    玩家名还是"英雄名（电脑）"，只能靠真机确认 —— ``smoke_live.py`` 会把
    原始事件打出来。

    ``exclude`` 用来排除与主语重复的人：击杀者和被杀者都不该出现在
    助攻名单里（早先只排除了主人自己，理论上会出现"被 Zed 击杀
    （助攻：Zed）"这种自相矛盾的话）。
    """
    raw = event.get("Assisters")
    if not isinstance(raw, list) or not raw:
        return []
    skip = {str(x).strip() for x in (exclude or ()) if str(x).strip()}
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name or name in skip or state.is_me(name):
            continue
        who = _champion_of(name, state, champion_names) or name
        if who not in out:
            out.append(who)
    return out


def _assist_phrase(
    event: dict,
    state: MatchState,
    champion_names: dict[str, str],
    *,
    exclude: set[str] | None = None,
) -> str:
    """助攻数据：「（助攻：星籁歌姬、正义巨像）」。

    刻意用"助攻：A、B"这种**数据式**写法，而不是"有 A、B 帮忙"那种
    叙述式写法。提示语里另外会要求模型"别特意点名助攻的人"，
    如果这里已经写成一句完整的话，模型很容易照念一遍。
    """
    names = _assist_names(event, state, champion_names, exclude=exclude)
    if not names:
        return ""
    return f"（助攻：{'、'.join(names)}）"


# 有助攻时才追加到提示语尾部的约束。只在"主人击杀/阵亡"且确实有人助攻时加，
# 不往每条播报里塞。
ASSIST_BACKGROUND_HINT = "（助攻只作背景，不用特意点名谁帮的。）"


def has_assists(
    event: dict,
    state: MatchState,
    champion_names: dict[str, str] | None = None,
    *,
    exclude: set[str] | None = None,
) -> bool:
    """这条击杀有没有"主人以外的人"参与助攻（给调用方决定要不要加提示）。"""
    return bool(
        _assist_names(event, state, champion_names or {}, exclude=exclude)
    )


def build_event_cue(
    event: dict,
    state: MatchState,
    *,
    champion_names: dict[str, str] | None = None,
    only_mine: bool = False,
    dragon_counts: dict[str, int] | None = None,
    soul_type: str | None = None,
    include_stats: bool = True,
) -> Cue | None:
    """把一条 Live Client Data 事件变成待播报的提示。不认识的事件返回 None。

    ``only_mine=True`` 时，一血只在"主人拿到"或"主人被拿"时播报；队友之间
    或对面之间的一血直接返回 ``None``。

    ``include_stats=False`` 时不附战绩行 —— 由调用方按冷却时间决定。
    战绩行每次要带一段格式说明（"中间的横线念成杠…"），资源类事件一场
    能出十几个，逐条带会让每句话都拖着一条尾巴。
    """
    names = champion_names or {}
    event_name = str(event.get("EventName") or "").strip()
    key = EVENT_KEYS.get(event_name)
    if key is None:
        return None

    where = f"{state.map_name}·{state.game_mode}"
    clock = state.game_time_label

    if event_name == "GameStart":
        me = state.me
        mine = f"主人玩的是 {_localize_champion(me.champion, names)}。" if me else ""
        # 分路对位。只有排位/灵活排位顺序固定，且必须是召唤师峡谷征召局
        # （state.lanes_trusted 已经把这两层都卡住了）。
        lane_text = ""
        if state.lanes_trusted and me is not None:
            lane = state.my_lane
            opponent = state.lane_opponent()
            if lane and opponent is not None:
                lane_text = (
                    f"主人走{lane}，对位的是对面的"
                    f"{_spaced(_localize_champion(opponent.champion, names))}。"
                )
            elif lane:
                lane_text = f"主人走{lane}。"
        body = f"主人刚开了一局英雄联盟（{where}）。{mine}{lane_text}"

    elif event_name == "FirstBlood":
        # **实测结论（2026-11-16，可自证）**：FirstBlood 的 Recipient 是
        # **拿到一血的人**（击杀者），不是被杀的那个。
        #
        # 证据：那天的日志里同时出现了
        #   [一血] 抑制同一击杀的人头播报（爱好挨打 → 蛮族之王（电脑））
        #   已发送（first_blood）：主人拿到了一血。
        # 抑制逻辑要求 Recipient ∈ {击杀者, 被杀者}，"主人拿到一血"要求
        # Recipient 是主人；两者交集只有"Recipient = 主人 = 击杀者"。
        #
        # 所以这里不再做"我方但不是我 → 被拿一血"那套反推（前提本来就不
        # 成立），只按事实分三档：主人拿的 / 别人拿的。
        recipient = str(event.get("Recipient") or "").strip()
        if state.is_me(recipient):
            body = "主人拿到了一血！"
        elif only_mine:
            # 不是主人拿的 —— 不关主人的事（only_mine 就是要安静）。
            return None
        elif state.side_of(recipient) == "ally":
            body = "这局的一血被主人这边拿走了。"
        elif state.side_of(recipient) == "enemy":
            body = "这局的一血被对面拿走了。"
        else:
            body = "这局的一血已经产生了。"

    elif event_name == "ChampionKill":
        # 运行时不走这条路（人头由 _process_events 单独处理），但保持它能独立正确工作。
        described = describe_champion_kill(
            event, state, champion_names=names, only_mine=only_mine
        )
        if described is None:
            return None
        desc, kind = described
        body = desc
        if kind == "my_death":
            key = "my_death"

    elif event_name == "Multikill":
        # 多杀**从不**单独播报：接口对一次五杀会额外发 4 条 Multikill，
        # 逐条说就是刷屏，而 ChampionKill 已经把每个人头都说过一遍了。
        # 插件只把它当作"三杀/四杀/五杀"的标签。
        return None

    elif event_name == "Ace":
        acer = str(event.get("Acer") or "").strip()
        side = state.side_of(acer)
        if side == "ally":
            body = "主人这边团灭了对面！"
        elif side == "enemy":
            body = "主人这边被团灭了。"
        else:
            body = "一波团战打完了，有一方被团灭。"

    elif event_name == "DragonKill":
        # 和拆塔同一套说法：你 / 队友 / 敌方。
        dragon = _DRAGON_TYPES.get(str(event.get("DragonType") or ""), "小龙")
        killer = str(event.get("KillerName") or "").strip()
        who = _killer_phrase(killer, state)
        body = f"{who}拿下了{dragon}{_stolen_suffix(event)}"

        # 第三条 / 第四条小龙要额外提醒：龙魂快到手了 / 龙魂到手了。
        # 计数由插件在会话状态里维护（接口不给"这是第几条"）。
        side = state.side_of(killer)
        # 传中文名（"火龙"）而不是原始 DragonType（"Fire"）—— 龙魂要按
        # 属性叫"火龙魂"，英文拼进去很别扭。
        soul = _soul_note(side, dragon_counts, dragon, soul_type)
        if soul:
            body += f"。{soul}"
        body += "。"

    elif event_name == "BaronKill":
        who = _killer_phrase(str(event.get("KillerName") or ""), state)
        body = f"{who}拿下了大龙{_stolen_suffix(event)}。"

    elif event_name == "HeraldKill":
        who = _killer_phrase(str(event.get("KillerName") or ""), state)
        body = f"{who}拿下了峡谷先锋。"

    elif event_name == "TurretKilled":
        # 塔的归属看名字里的队伍 tag；**谁拆的**看 KillerName。
        # 这两件事必须分开判断 —— 只看队伍 tag 的话，敌人拆我们的塔
        # 会播成"主人这边推掉了"，方向完全反了。
        info = _parse_structure(event.get("TurretKilled"))
        tower = _turret_label(event.get("TurretKilled"), is_aram=state.is_aram)
        tower_side = _side_from_team_tag(info["raw_team"], state)
        killer = str(event.get("KillerName") or "").strip()

        if tower_side == "ally":
            # 我们的塔被拆
            who = _killer_phrase(killer, state)
            body = f"{who}拆掉了我们的{tower}。" if who else f"我们的{tower}被拆了。"
        elif tower_side == "enemy":
            who = _killer_phrase(killer, state)
            body = f"{who}拆掉了对面的{tower}。" if who else f"对面的{tower}被拆了。"
        else:
            # 认不出塔属于谁（字段形态变了）时给中性描述，别硬编方向。
            body = f"{tower}被拆了。"

    elif event_name == "InhibKilled":
        # 和拆塔同一套规则：归属看结构名，谁拆的看 KillerName。
        info = _parse_structure(event.get("InhibKilled"))
        inhib = _inhib_label(event.get("InhibKilled"), is_aram=state.is_aram)
        inhib_side = _side_from_team_tag(info["raw_team"], state)
        killer = str(event.get("KillerName") or "").strip()
        who = _killer_phrase(killer, state)
        if inhib_side == "ally":
            body = f"{who}拆掉了我们的{inhib}。" if who else f"我们的{inhib}被拆了。"
        elif inhib_side == "enemy":
            body = f"{who}拆掉了对面的{inhib}。" if who else f"对面的{inhib}被拆了。"
        else:
            body = f"{inhib}被拆了。"

    elif event_name == "GameEnd":
        # GameEnd 这个 key 现在**不再单独产生 cue** —— 它一到就直接播
        # build_post_game_cue 那条完整总结（见 __init__._emit_post_game）。
        # 这里的分支只保留给"直接调用 build_event_cue"的场景（例如测试、
        # 外部工具），保证它单独用也说得对。
        result = str(event.get("Result") or "").strip()
        label = RESULT_LABELS.get(result, "")
        body = f"这局结束了，{label}。" if label else "这局结束了。"

    else:  # pragma: no cover - EVENT_KEYS 已覆盖全部分支
        return None

    me = state.me
    # body 绝大多数分支以"。"结尾，这里不能再补逗号，否则会出现"。，"。
    # 资源类事件（龙/塔/兵营/先锋）不挂当前战绩 —— 那些是地图资源，
    # 跟主人这一刻几杀几刀没有关系，塞进来只会让模型答非所问。
    # body 自己以"。"结尾，补战绩时要先剥掉它再拼，否则会出现"。。"。
    head = _strip_tail(body)
    with_stats = me is not None and include_stats and key not in _NO_STATS_KEYS
    stats = me.one_line() if with_stats else ""
    tail = f"主人现在 {stats}。" if stats else ""
    legend = _KDA_LEGEND if stats else ""
    if key in _NO_TIME_KEYS:
        # 不带时间：(地图·模式) 仍然保留，帮模型确定场景。
        text = (
            f"（主人正在玩英雄联盟，{where}。"
            f"{head}。{tail}{legend}{_SPEAK_INSTRUCTION}）"
        )
    else:
        text = (
            f"（主人正在玩英雄联盟，{where}，对局时间 {clock}。"
            f"{head}。{tail}{legend}{_SPEAK_INSTRUCTION}）"
        )
    return Cue(
        key=key,
        text=text,
        priority=PRIORITIES.get(key, 30),
        meta={"event": event_name},
        body=_normalize_body(body),
        tier=tier_for(key),
        has_stats=bool(stats),
    )


def build_summary_cue(
    state: MatchState,
    *,
    champion_names: dict[str, str] | None = None,
) -> Cue:
    """定期战况小结。"""
    names = champion_names or {}
    detail = _localize_names(state.summary_text(), names)
    text = (
        f"（主人正在玩英雄联盟，这是当前战况：\n{detail}\n"
        f"比分：{state.score_line()}。\n"
        f"{_KDA_LEGEND}\n"
        f"{_speak_instruction('点评一句当前局势，一到两句')}）"
    )
    return Cue(
        key="summary", text=text, priority=PRIORITIES["summary"],
        meta={"kind": "summary"}, tier=TIER_AMBIENT,
        # body 给播报记录用的一行摘要；不设的话测试台会把整段提示语显示出来。
        body=f"当前战况：{state.score_line()}",
        # 不参与窗口合并：它是"此刻状态"，合进"刚才发生了这些事"会语义错乱，
        # 而且会把完整战况列表压缩成一行比分。
        mergeable=False,
    )


def build_post_game_cue(
    state: MatchState,
    *,
    champion_names: dict[str, str] | None = None,
    result: str = "",
    pending_items: list[str] | None = None,
) -> Cue:
    """对局结束播报 —— 这是**唯一的**结束播报，内含完整总结。

    ``result`` 是 ``GameEnd`` 事件里的权威结果（``Win`` / ``Lose`` /
    ``Draw``）。有它就直说胜负；**没有就完全不说输赢** —— 击杀数领先
    不代表赢了（可能被偷家），猜错比不说更糟。

    ``pending_items`` 是窗口里还没说出口的事件正文（推塔、兵营等）。
    对局结束了，那些内容再等窗口就没机会说了，所以并进这条一起报。
    """
    names = champion_names or {}
    detail = _localize_names(state.post_game_text(result), names)
    if RESULT_LABELS.get(str(result or "").strip()):
        # 这是唯一的结束播报，胜负由它来说 —— 不和别处重复，也不会漏。
        verdict_note = "胜负以游戏给的结算结果为准，可以直接说。"
    else:
        verdict_note = (
            "注意：这局没收到游戏的结算结果，所以**不要说输赢**，"
            "也不要根据击杀数或比分推测胜负。只聊战绩和过程。"
        )

    # 对局结束时窗口里还攒着的内容（最后几座塔、兵营）—— 再等就没机会了。
    tail_items = [str(x).strip() for x in (pending_items or []) if str(x).strip()]
    tail_block = ""
    if tail_items:
        tail_block = "对局结束前最后发生的事：" + chr(10) + "；".join(tail_items) + chr(10)

    text = (
        "（主人刚打完一局英雄联盟，这是最后记录到的战况：" + chr(10)
        + f"{detail}" + chr(10)
        + f"{verdict_note}"
        f"{tail_block}"
        f"{_speak_instruction('做个简短赛后总结，两到三句')}"
        "不要编造上面没写的数据。）"
    )
    verdict = RESULT_LABELS.get(str(result or "").strip(), "")
    summary = f"本局结束，时长 {state.game_time_label}"
    if verdict:
        summary += f"，{verdict}"
    return Cue(
        # 这条播报就是「对局结束」，用 game_end 这个 key —— post_game 不再是
        # 独立事件，面板上也只留一个开关。
        key="game_end", text=text, priority=PRIORITIES["game_end"],
        meta={"kind": "game_end", "result": str(result or "").strip()},
        tier=TIER_CRITICAL,
        body=(
            f"{summary}，战绩 {state.me.kda}。"
            if state.me
            else f"{summary}。"
        ),
    )



def render_plain(cue: Cue) -> str:
    """剥掉括号和给模型的指令，得到可以直接给用户看的一行文字。"""
    text = cue.text
    if text.startswith("（"):
        text = text[1:]
    if text.endswith("）"):
        text = text[:-1]
    marker = "用你自己的口吻"
    if marker in text:
        text = text.split(marker, 1)[0].rstrip("，。 ")
    return text.strip()


def is_announced(key: str, announce_flags: dict[str, bool]) -> bool:
    """查 plugin.toml 的 [neko_lol.announce] 开关。缺省视为开。"""
    return announce_flags.get(key, True)


# ---------------------------------------------------------------------------
# 击杀合并：短时间内多个人头合并成一条，避免连杀刷屏。
# ---------------------------------------------------------------------------


def _strip_tail(text: str) -> str:
    """去掉句末标点，交给包装层统一补 —— 否则会拼出"。。"。"""
    return str(text or "").strip().rstrip("。，, ！？!?、;；")


def _normalize_body(text: str) -> str:
    """正文统一以「。」结尾。

    ``Cue.body`` 是给播报记录、窗口合并和 LLM 工具用的未包装正文。
    早先有些分支天生带句号（资源/建筑），有些不带（击杀/团灭），于是
    测试台的记录里同样是"一句话"，有的有句号有的没有。
    """
    stripped = _strip_tail(text)
    return f"{stripped}。" if stripped else ""


@dataclass
class OthersMultikill:
    """**别人**（队友或对面）拿到三杀及以上时的播报。

    这是"只播主人的事"的一条明确例外：别人打出三杀/四杀/五杀值得提一嘴。

    为什么要攒一下再播：``KillStreak`` 是**这条命里的累计击杀数**，每拿到
    一个人头接口就发一条（2、3、4…），连杀过程中的几条间隔可能只有一两秒。
    逐条播就是"三杀！"→"四杀！"连喊好几声。所以按击杀者攒
    ``settle_seconds`` 秒，取**最高**连杀数，只播一次。

    ｜注意｜早先这里写的是"接口对一次五杀会连发四条（2/3/4/5）"。
    翻遍 2026-11-13 ~ 11-16 的日志，**从未**出现过这种毫秒级连发：
    唯一一次"三杀 → 四杀"间隔 10 秒，已经超过攒的窗口。所以这条表述
    是当年的推测，没有实测支撑 —— 攒窗口真正能合并的，是几秒内的快速连杀。
    """

    settle_seconds: float = 4.0
    # 三杀起才播。双杀太常见，说了只是噪音。
    min_streak: int = 3

    _pending: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(
        self,
        killer: str,
        streak: int,
        state: MatchState,
        now: float,
        *,
        champion_names: dict[str, str] | None = None,
    ) -> None:
        """喂一条 Multikill 事件（仅限别人；主人自己的不单独播报）。"""
        name = str(killer or "").strip()
        if not name or streak < self.min_streak:
            return
        who = _champion_of(name, state, champion_names or {}) or name
        prev = self._pending.get(name)
        self._pending[name] = {
            # 后到的连杀数更大（3→4→5），取最大而不是覆盖成最后一条。
            "streak": max(streak, prev["streak"] if prev else 0),
            "who": who,
            "side": state.side_of(name),
            "at": now,
        }

    def flush(self, now: float, state: MatchState) -> list[Cue]:
        """攒够时间的产出播报。返回 0 或 1 条。"""
        out: list[Cue] = []
        for name in list(self._pending):
            item = self._pending[name]
            if now - item["at"] < self.settle_seconds:
                continue
            del self._pending[name]
            cue = _multikill_cue(item, state)
            if cue is not None:
                out.append(cue)
        return out

    def force_flush(self, state: MatchState) -> list[Cue]:
        """不等攒够，立刻产出。对局结束时用。"""
        out: list[Cue] = []
        for name in list(self._pending):
            cue = _multikill_cue(self._pending.pop(name), state)
            if cue is not None:
                out.append(cue)
        return out

    @property
    def pending(self) -> int:
        return len(self._pending)


def _multikill_cue(item: dict[str, Any], state: MatchState) -> Cue | None:
    labels = {3: "三杀", 4: "四杀", 5: "五杀"}
    label = labels.get(item["streak"], f"{item['streak']} 连杀")
    side = item.get("side")
    if side == "ally":
        prefix = "主人这边的 "
    elif side == "enemy":
        prefix = "对面的 "
    else:
        prefix = ""
    who = item.get("who") or "有人"
    body = f"{prefix}{who} 拿了{label}！"
    # 多杀不带对局时间（2026-11-18 用户指定）。
    text = f"（主人正在玩英雄联盟。{body}{_SPEAK_INSTRUCTION}）"
    return Cue(
        key="multikill",
        text=text,
        priority=PRIORITIES["multikill"],
        meta={"kind": "others_multikill", "streak": item["streak"]},
        body=_normalize_body(body),
        # 三杀以上值得立刻说，但不必抢占。
        tier=TIER_NORMAL,
    )


# 主人自己的多杀也值得播 —— 这是"只播主人的事"里最该播的一种。
# 措辞按击杀数分档，语气随连杀数走（超过五杀统一说"五杀"，不造新词）。
_MY_MULTIKILL_LABELS: dict[int, str] = {
    3: "主人拿到三杀了！",
    4: "主人四杀了！",
    5: "主人五杀！",
}


class MyMultikill:
    """**主人自己**拿到三杀及以上时的播报。

    为什么不直接逐条播：``KillStreak`` 是累计连杀数，每拿一个人头发一条
    （2、3、4…），快节奏连杀时几条只隔一两秒。逐条播就是"三杀！"→"四杀！"
    连喊好几声 —— 和别人的多杀同一个问题。所以也攒 ``settle_seconds`` 秒，
    取**最高**那条。

    为什么单独一个类：别人的多杀要标"对面的/主人这边的 + 英雄名"，
    主人自己的话术是另一个形状（没有阵营前缀，语气更兴奋），而且
    **优先级是关键档** —— 这是主人自己的高光，值得打断当前话题。
    """

    settle_seconds: float = 4.0

    _pending: dict[str, Any] | None = None

    def __init__(self) -> None:
        self._pending = None

    def add(self, streak: int, now: float) -> None:
        """喂一条主人自己的 Multikill。只留最高连杀数。"""
        if streak < 3:
            return
        prev = self._pending
        self._pending = {
            "streak": max(streak, prev["streak"] if prev else 0),
            "at": now,
        }

    def flush(self, now: float, state: MatchState) -> list[Cue]:
        if self._pending is None:
            return []
        if now - self._pending["at"] < self.settle_seconds:
            return []
        item = self._pending
        self._pending = None
        cue = _my_multikill_cue(item, state)
        return [cue] if cue is not None else []

    def force_flush(self, state: MatchState) -> list[Cue]:
        """不等攒够，立刻产出。对局结束时用。"""
        if self._pending is None:
            return []
        item = self._pending
        self._pending = None
        cue = _my_multikill_cue(item, state)
        return [cue] if cue is not None else []

    @property
    def pending(self) -> int:
        return 1 if self._pending is not None else 0


def _my_multikill_cue(item: dict[str, Any], state: MatchState) -> Cue | None:
    streak = int(item.get("streak") or 0)
    label = _MY_MULTIKILL_LABELS.get(streak, "主人五杀！")
    # 刻意**不带**战绩、不带对局时间：五杀本身就是最高光，
    # 后面跟一行"13—0—1，16 级"反而把它稀释成一条战报。
    # 这句话要短、要能直接喊出来。
    text = f"（主人刚刚在英雄联盟里拿到了成绩：{label}{_SPEAK_INSTRUCTION}）"
    return Cue(
        key="multikill",
        text=text,
        priority=PRIORITIES["multikill"],
        meta={"kind": "my_multikill", "streak": streak},
        body=_normalize_body(label),
        # 主人自己的多杀值得打断当前话题（别人那条只是背景音，走常规档）。
        tier=TIER_CRITICAL,
    )



def describe_champion_kill(
    event: dict,
    state: MatchState,
    *,
    champion_names: dict[str, str] | None = None,
    only_mine: bool = True,
    announce_my_assists: bool = False,
    with_lane: bool = False,
) -> tuple[str, str] | None:
    """把一条 ``ChampionKill`` 渲染成 (片段, kind)。

    **只播报主人自己参与的事件**：主人击杀、主人阵亡。队友拿人头、队友被
    杀、对面互杀一律返回 ``None`` —— 那些不关主人什么事，说了只是噪音。

    ``with_lane=True`` 时给英雄名补一个分路后缀（``对面的 劫（中单）``）。
    只在排位/灵活排位这类分路可信的局里由调用方打开 —— 普通匹配的数组
    顺序不保证，补上去就是编的。

    ``announce_my_assists=True`` 时多一条例外：**主人助攻的人头也播**。
    这是给辅助位准备的 —— 辅助很少拿人头，光播主人击杀的话一整局都在沉默。
    开关由插件按"峡谷 + 排位 + 辅助位"判断后传入，这里只负责渲染。

    ``only_mine=False`` 时才走完整四象限（保留给想听全场战况的人）：

    ==================  ========================  ================
    谁杀了谁             片段                      kind
    ==================  ========================  ================
    主人 → 对面          主人击杀了对面的 X        ``my_kill``
    对面 → 主人          主人被对面的 X 击杀了     ``my_death``
    主人助攻             主人助攻，队友 X 拿下…    ``my_assist``
    队友 → 对面          主人这边的 X 击杀了…      ``ally_kill``
    对面 → 队友          主人这边的 X 被…击杀     ``ally_death``
    ==================  ========================  ================

    ｜注意｜只判"是不是主人"、其余一律当成"队友被杀"的写法，会把最常见的
    "队友拿了人头"播报成"我们的人被杀了"——方向完全相反。四象限分支保留
    着，正是为了防止这种退化。
    """
    names = champion_names or {}
    killer = str(event.get("KillerName") or "").strip()
    victim = str(event.get("VictimName") or "").strip()
    i_killed = state.is_me(killer)
    i_died = state.is_me(victim)
    # 主人没参与击杀/阵亡，但出现在助攻名单里 —— 辅助位的日常。
    i_assisted = (
        not i_killed and not i_died and _i_am_assister(event, state)
        and announce_my_assists
    )

    if only_mine and not (i_killed or i_died or i_assisted):
        return None

    def _with_lane(who: str, player_name: str) -> str:
        """按需给英雄名补分路后缀，如「劫（中单）」。"""
        if not with_lane or not who:
            return who
        lane = state.lane_of(player_name)
        return f"{who}（{lane}）" if lane else who

    killer_who = _champion_of(killer, state, names) or killer or "某人"
    victim_who = _champion_of(victim, state, names) or victim or "某人"
    killer_who = _with_lane(killer_who, killer)
    victim_who = _with_lane(victim_who, victim)

    if i_assisted:
        # 辅助位的助攻：说清"你帮谁拿下了谁"。
        # 只在这个分支下才可能走到 —— 主人参与击杀/阵亡时优先走上面两条。
        return (
            f"主人助攻，{killer_who} 拿下了对面的 {victim_who}",
            "my_assist",
        )

    if i_died:
        # 主人被杀：先看击杀者是不是防御塔/小兵这类非英雄单位。
        # 否则会播成"主人被对面的 Turret_TChaos_L1_P3_... 击杀了"。
        unit = _non_champion_killer(killer)
        if unit and state.find_player(killer) is None:
            return (
                f"主人被{unit}击杀了"
                f"{_assist_phrase(event, state, names, exclude={killer, victim})}",
                "my_death",
            )

    if i_killed:
        # 主人击杀：带上助攻数据（形如「（助攻：A、B）」），但只是背景——
        # 提示语那边会要求模型别特意点名谁帮的。
        # 早先这里调 _assist_suffix，但它判断的是"主人在不在 Assisters 里"——
        # 主人既然是击杀者就不可能同时是助攻者，那句永远为空，是死代码。
        return (
            f"主人击杀了对面的 {victim_who}"
            f"{_assist_phrase(event, state, names, exclude={killer, victim})}",
            "my_kill",
        )

    if i_died:
        # 主人被杀：告诉主人"对面是谁带着人把你做掉的"。
        # 不带复活倒计时：那是界面上的信息，说出来只会拖长句子。
        return (
            f"主人被对面的 {killer_who} 击杀了"
            f"{_assist_phrase(event, state, names, exclude={killer, victim})}",
            "my_death",
        )

    killer_side = state.side_of(killer)
    victim_side = state.side_of(victim)
    if killer_side == "ally" and victim_side == "enemy":
        return f"主人这边的 {killer_who} 击杀了对面的 {victim_who}", "ally_kill"
    if killer_side == "enemy" and victim_side == "ally":
        return f"主人这边的 {victim_who} 被对面的 {killer_who} 击杀", "ally_death"
    # 敌我认不出来（比如没认出主人是哪位）时给中性描述，别硬编方向。
    return f"{killer_who} 击杀了 {victim_who}", "neutral"


def _champion_of(
    name: str,
    state: MatchState,
    champion_names: dict[str, str],
) -> str:
    """玩家名/英雄名 → 干净的英雄名（认不出来就返回去掉括号后缀的原名）。

    早先这里只比 ``player.name == name``，而且认不出就**原样返回入参**。
    两个后果都在 2026-11-16 的日志里出现过：

    * ``KillerName`` 是防御塔结构名时，播成
      "主人被对面的 Turret_TChaos_L1_P3_2254202041_0 击杀了"；
    * 助攻名单里是 ``牛头酋长（电脑）``，认不出就把"（电脑）"一起念出来。

    现在：先走 ``state.find_player``（它按玩家名、归一化名、英雄名三层匹配），
    再退回原名，并且**剥掉 （电脑）/(Bot) 这类后缀** —— 那是客户端给机器人
    打的标记，不该进播报。
    """
    if not name:
        return ""
    player = state.find_player(name)
    if player is not None and player.champion:
        return _localize_champion(player.champion, champion_names)
    return _strip_bot_suffix(name)


# 客户端给机器人/特殊单位加的后缀，念出来只会让播报变怪。
_BOT_SUFFIX_RE = re.compile(r"（[^）]*）|\([^)]*\)")


def _strip_bot_suffix(name: str) -> str:
    """去掉 ``（电脑）`` / ``(Bot)`` 这类后缀，保留可读的部分。"""
    text = _BOT_SUFFIX_RE.sub("", str(name or "")).strip()
    return text or str(name or "").strip()


# 非英雄单位当击杀者时，播报里改用人类可读的说法。
# 实测 2026-11-16：KillerName 直接给了
# ``Turret_TChaos_L1_P3_2254202041_0`` 这种结构物内部 ID。
_NON_CHAMPION_KILLERS: tuple[tuple[str, str], ...] = (
    ("turret", "防御塔"),
    ("inhib", "兵营"),
    ("minion", "小兵"),
    ("sru_", "野怪"),
    ("horde", "虚空巢虫"),
    ("baron", "大龙"),
    ("dragon", "小龙"),
    ("herald", "峡谷先锋"),
)


def _non_champion_killer(name: str) -> str:
    """``Turret_TChaos_L1_P3_...`` → ``防御塔``。不是特殊单位就返回空串。"""
    text = str(name or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for prefix, label in _NON_CHAMPION_KILLERS:
        if lowered.startswith(prefix) or f"_{prefix}" in lowered:
            return label
    # 纯 ID 形态（没有中文、没有空格、带下划线一长串）也当成特殊单位，
    # 否则会原样念给用户听。
    if "_" in text and not any("一" <= ch <= "鿿" for ch in text):
        return "非英雄单位"
    return ""
