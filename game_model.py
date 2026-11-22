"""把 Live Client Data API 的原始 JSON 解析成好用的对局模型。

这个模块不碰网络、不碰宿主，纯数据变换，方便离线测试
（见 ``tests/test_offline.py`` 里的 ``TestGameModel`` / ``TestLaneInference``）。

关于英雄名：**接口返回的是中文官方称号，不是英文**（2026-11-15 用国服实测
日志确认：`championName` 回来的是「虚空恐惧」「诺克萨斯之手」「德玛西亚
之力」这类四个字的官方全称）。国际服可能仍返回英文（如 ``Yasuo``）。
所以这里**不做内置映射表**，而是留两张可选表，放在插件数据目录：

1. ``champion_aliases.json``：官方全称 → 玩家常用简称/外号，
   ``{"虚空恐惧": "大虫子", "诺克萨斯之手": "诺手"}``。国服主要用这张。
2. ``champion_names.json``：通用覆盖表，``{"Yasuo": "亚索"}``。国际服用。

两张表都不存在时原样输出，不会报错。播报提示语也会要求模型自己把全称
换成玩家平时的叫法，所以即使没有表，猫娘说的也不是四个字的官方全称。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# mapNumber → 地图中文名。只列常见地图，其余回落 API 给的 mapName。
_MAP_NAMES: dict[int, str] = {
    11: "召唤师峡谷",
    12: "嚎哭深渊",
    21: "极限闪击",
    22: "极限闪击",
    30: "斗魂竞技场",
    33: "无尽狂潮",
}

_GAME_MODES: dict[str, str] = {
    "CLASSIC": "经典",
    "ARAM": "极地大乱斗",
    "PRACTICETOOL": "训练模式",
    "TFT": "云顶之弈",
    "URF": "无限火力",
    "NEXUSBLITZ": "极限闪击",
    "ARENA": "斗魂竞技场",
    "ONEFORALL": "克隆大作战",
    "DOOMBOT": "末日人机",
}

TEAM_LABELS = {"ORDER": "蓝色方", "CHAOS": "红色方"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class PlayerStat:
    """一名玩家在某一个瞬间的战况。"""

    name: str
    champion: str
    team: str
    level: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    cs: int = 0
    current_gold: float = 0.0
    estimated_gold: float = 0.0
    is_dead: bool = False
    respawn_timer: float = 0.0
    items: list[str] = field(default_factory=list)

    @property
    def team_label(self) -> str:
        return TEAM_LABELS.get(self.team, self.team or "未知")

    @property
    def kda(self) -> str:
        # 用破折号而不是斜杠：斜杠在"3/1/2"这种纯数字串里容易被念成
        # 分数或日期，破折号读起来更像"三杠一杠二"。
        return f"{self.kills}—{self.deaths}—{self.assists}"

    @property
    def kda_ratio(self) -> float:
        """(K+A)/D，0 死亡时按死亡数 1 计算，避免除零。"""
        return (self.kills + self.assists) / max(1, self.deaths)

    def one_line(self, *, with_items: bool = False) -> str:
        """播报里的一行战绩：英雄、KDA、等级。

        刻意**不带补刀**、**不带阵亡/复活倒计时**：那些是屏幕上的数字，
        塞进播报只会拖长句子。数据仍保留在 ``cs`` / ``is_dead`` /
        ``respawn_timer`` 字段里，LLM 工具查得到。
        """
        parts = [
            f"{self.champion}",
            f"{self.kda}",
            f"{self.level} 级",
        ]
        # 刻意**不带**阵亡状态和复活倒计时：那是屏幕上的东西，
        # 播报里说出来只会拖长句子、打断节奏。
        if with_items and self.items:
            parts.append("装备：" + "、".join(self.items))
        return "，".join(parts)


@dataclass
class MatchState:
    """一份对局快照。"""

    game_time: float = 0.0
    game_mode: str = ""
    map_name: str = ""
    # 原始 mapNumber。用来判断"是不是嚎哭深渊系"——大乱斗和它的变种
    # （海克斯大乱斗等）都在 12 号地图上，凭 mapNumber 比猜 gameMode
    # 字符串可靠得多。
    map_number: int = 0
    # 是否信任"数组顺序 = 分路顺序"。由插件按配置和地图/模式判定后传入。
    lanes_trusted: bool = False
    me: PlayerStat | None = None
    allies: list[PlayerStat] = field(default_factory=list)
    enemies: list[PlayerStat] = field(default_factory=list)
    my_names: set[str] = field(default_factory=set)
    # 本局双方各拆掉几座塔。接口的 allPlayers 不给塔数，只能由插件
    # 逐个数 TurretKilled 事件（见 __init__._count_turret）。默认 0。
    ally_turrets: int = 0
    enemy_turrets: int = 0

    # ── 构造 ────────────────────────────────────────────────────

    @classmethod
    def from_all_game_data(cls, data: dict, *, trust_lane_order: bool = False) -> MatchState:
        """把一份 allgamedata 变成对局模型。

        ``trust_lane_order``：调用方是否认为"同队数组顺序 = 分路顺序"。
        只有排位 / 灵活排位成立，所以这里再叠一层地图和模式校验 ——
        非召唤师峡谷、非征召局一律不信，避免把随机顺序当分路说出去。
        """
        game_data = data.get("gameData") if isinstance(data.get("gameData"), dict) else {}
        active = data.get("activePlayer") if isinstance(data.get("activePlayer"), dict) else {}
        raw_players = data.get("allPlayers")
        players_raw = raw_players if isinstance(raw_players, list) else []

        my_names = _names_in(active)
        # 逐条解析时保留原始 JSON —— "哪个是我"必须按原始名字精确匹配。
        # 早先的做法是按 (英雄名, 队伍) 回查原始记录，同队同英雄时（克隆
        # 大作战/无尽狂潮，或者自定义局撞英雄）会把队友认成主人本人，
        # 于是所有的"你"都显示成队友的数据。
        parsed: list[tuple[PlayerStat, dict]] = [
            (_player_from_raw(raw), raw)
            for raw in players_raw
            if isinstance(raw, dict)
        ]
        players = [stat for stat, _raw in parsed]

        me = next(
            (stat for stat, raw in parsed if _names_in(raw) & my_names),
            None,
        )
        # 名字一个都没匹配上（区服字段差异）时刻意**不猜**。
        # 猜错的代价是双向的：allies/enemies 会整体错位，而且一旦把猜中的
        # 那个人塞进 my_names，is_me() 就会对真正的对手返回 True，
        # 后面所有队伍归属判断跟着一起错。宁可退化成"读不到自己的数据"。

        allies: list[PlayerStat] = []
        enemies: list[PlayerStat] = []
        if me is not None:
            allies = [p for p in players if p.team == me.team]
            enemies = [p for p in players if p.team != me.team]

        map_number = _as_int(game_data.get("mapNumber"), 0)
        map_name = (
            _MAP_NAMES.get(map_number)
            or str(game_data.get("mapName") or "").strip()
            or "未知地图"
        )
        mode_raw = str(game_data.get("gameMode") or "").strip()
        return cls(
            game_time=_as_float(game_data.get("gameTime"), 0.0),
            game_mode=_GAME_MODES.get(mode_raw, mode_raw),
            map_name=map_name,
            map_number=map_number,
            me=me,
            allies=allies,
            enemies=enemies,
            my_names=my_names,
            # 分路推断的适用条件：调用方同意 + 召唤师峡谷 + 征召模式。
            # 大乱斗只有一条路，克隆大作战/无限火力顺序也无意义。
            lanes_trusted=bool(
                trust_lane_order and map_number == 11 and mode_raw == "CLASSIC"
            ),
        )

    # ── 查询 ────────────────────────────────────────────────────

    @property
    def my_lane_index(self) -> int:
        """主人在己方数组里的下标，也就是分路序号。认不出主人返回 -1。"""
        if self.me is None:
            return -1
        for index, player in enumerate(self.allies):
            if player is self.me or (player.name and player.name == self.me.name):
                return index
        return -1

    @property
    def my_lane(self) -> str:
        """主人的分路名。不可信或认不出时返回空串。"""
        if not self.lanes_trusted:
            return ""
        index = self.my_lane_index
        return LANE_NAMES[index] if 0 <= index < len(LANE_NAMES) else ""

    def lane_of(self, name: str) -> str:
        """某个玩家走哪条路（上单/打野/中单/ADC/辅助）。认不出返回空串。

        只信 ``lanes_trusted``：普通匹配没有位置选择流程，数组顺序不保证，
        硬猜会一本正经地说错。击杀播报用它给英雄名补一个分路后缀。
        """
        if not self.lanes_trusted or not name:
            return ""
        key = name_key(name)
        for index, player in enumerate(self.allies):
            if player.name == name or (key and name_key(player.name) == key):
                return LANE_NAMES[index] if index < len(LANE_NAMES) else ""
        for index, player in enumerate(self.enemies):
            if player.name == name or (key and name_key(player.name) == key):
                return LANE_NAMES[index] if index < len(LANE_NAMES) else ""
        return ""

    def lane_opponent(self) -> PlayerStat | None:
        """主人的对位选手 —— 敌方数组里同下标的那个人。

        只在 ``lanes_trusted`` 为真时返回，否则 None。两边数组长度不一致
        （比如有人秒退、自定义局人数不等）时也返回 None，不硬凑。
        """
        if not self.lanes_trusted:
            return None
        index = self.my_lane_index
        if index < 0 or index >= len(self.enemies):
            return None
        return self.enemies[index]

    @property
    def is_aram(self) -> bool:
        """是不是嚎哭深渊系（大乱斗 / 海克斯大乱斗 / 极地大乱斗变种）。

        判断依据是 **mapNumber == 12**（嚎哭深渊），不是 gameMode 字符串 ——
        大乱斗有多个队列变种（海克斯大乱斗、无限乱斗等），它们的
        gameMode 可能各不相同甚至是我们没见过的代码，但地图都是 12。

        额外兜一层：gameMode 里出现"大乱斗"也算，防止某些区服 mapNumber 缺失。
        """
        if self.map_number == 12:
            return True
        mode = str(self.game_mode or "")
        return "大乱斗" in mode or mode.strip().upper() == "ARAM"

    def is_me(self, name: str) -> bool:
        """这个名字是不是**主人本人**。

        注意：不要拿它判断"是不是主人这边"——队友拿的大龙也是主人这边拿的。
        判断队伍归属用 :meth:`side_of`。
        """
        if not name:
            return False
        return name in self.my_names or name_key(name) in self.my_names
    def side_of(self, name: str) -> str:
        """一个名字属于哪一边：``"ally"`` / ``"enemy"`` / ``"unknown"``。

        事件里的 ``KillerName`` / ``VictimName`` / ``Acer`` 是**玩家名**，
        而播报"谁拿了大龙"要的是队伍归属。两者混用会把队友的功劳记到
        对面头上（实测：队友惩戒掉大龙 → 播报"对面拿下了大龙"）。

        比较必须走 :func:`name_key` 归一化：事件名和人机局的名单名形态
        经常不同（``蛮族之王（电脑）`` vs 平台编的 ``电脑1``）。
        早先用精确比较，训练模式里阵营全判不出来，"有人拿下了土龙"，
        龙魂播报整局失效。
        """
        if not name:
            return "unknown"
        player = self.find_player(name)
        if player is not None:
            return "ally" if player in self.allies or player is self.me else "enemy"
        return "unknown"

    def find_player(self, name: str) -> PlayerStat | None:
        """按名字或英雄名找人（两边都走归一化比较）。

        人机局的事件里经常直接用**英雄名+后缀**当玩家名
        （``蛮族之王（电脑）``），而名单里的玩家名是平台编的
        （``电脑1``），所以必须先按英雄名匹配一次。
        """
        key = name_key(name)
        if not key:
            return None
        for player in (*self.allies, *self.enemies):
            if player.name == name:
                return player
            if key and name_key(player.name) == key:
                return player
            if player.champion and name_key(player.champion) == key:
                return player
        return None

    def find_enemy(self, name: str) -> PlayerStat | None:
        player = self.find_player(name)
        if player is not None and player in self.enemies:
            return player
        # 兼容"传英雄名查敌人"的旧用法
        key = name_key(name)
        for enemy in self.enemies:
            if key and name_key(enemy.champion) == key:
                return enemy
        return None

    @property
    def game_time_label(self) -> str:
        """对局时间，格式 ``时:分:秒``（如 ``00:10:00``、``01:05:30``）。

        早先是 ``分:秒``（``10:00``）。改成带小时的完整形态是为了避免歧义：
        ``10:00`` 在中文语境里容易被念成"十点整"或"十比零"，而 ``00:10:00``
        无论怎么读都是"打了十分钟"。超过一小时的局也不会出现 ``65:20``
        这种怪格式。
        """
        total = max(0, int(self.game_time))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    # ── 渲染 ────────────────────────────────────────────────────

    def summary_text(self, *, with_items: bool = False) -> str:
        """给猫娘看的战况小结（英雄名用接口原样，通常是中文官方全称）。"""
        if self.me is None:
            return f"对局进行到 {self.game_time_label}，但读不到自己的数据。"

        lines = [
            f"对局时间 {self.game_time_label}（{self.map_name}·{self.game_mode}）",
            f"主人：{self.me.one_line(with_items=with_items)}",
        ]
        if self.allies:
            others = [a for a in self.allies if a is not self.me]
            if others:
                lines.append("队友：" + "；".join(a.one_line() for a in others))
        if self.enemies:
            lines.append("敌方：" + "；".join(e.one_line() for e in self.enemies))
        return "\n".join(lines)

    def score_line(self) -> str:
        """一行比分，**不用三段式**。

        早先写成 ``我方 26—26—35 · 敌方 22—31—20``（击杀—死亡—助攻）。
        实测模型会把第二个数（阵亡）当成击杀去比，于是把「领先 4 个」
        说成「落后」——两行的前两位还恰好互补，扫一眼就换位。所以这里
        只给击杀数，并把领先/落后直接算出来：

            "我方击杀 26，敌方击杀 22（我方领先 4 个）"

        详细的 K—D—A 仍在「主人/队友/敌方」那几行里，信息没丢。

        认不出主人时（见 :meth:`from_all_game_data`）分不出敌我，
        此时报"我方 0"是在撒谎，直接说明读不到。
        """
        if self.me is None:
            return "双方比分未知（没认出主人是哪一位）"
        ally_k = sum(a.kills for a in self.allies)
        enemy_k = sum(e.kills for e in self.enemies)
        lead = ally_k - enemy_k
        if lead > 0:
            verdict = f"（我方领先 {lead} 个）"
        elif lead < 0:
            verdict = f"（我方落后 {-lead} 个）"
        else:
            verdict = "（双方持平）"

        # 人头落后却赢了是常事（偷家、翻盘），不说清就会被模型解释成
        # "虽然落后但还是赢了"这种别扭话。塔数能直接解释局面。
        towers = ""
        if self.ally_turrets or self.enemy_turrets:
            towers = (
                f"，我方拆了 {self.ally_turrets} 座塔、"
                f"敌方拆了 {self.enemy_turrets} 座"
            )
            if lead < 0 and self.ally_turrets > self.enemy_turrets:
                towers += "（塔数占优）"
        return f"我方击杀 {ally_k}，敌方击杀 {enemy_k}{verdict}{towers}"

    @staticmethod
    def _human_duration(total_seconds: float) -> str:
        """对局时长说人话：``00:37:58`` → ``37 分 58 秒``。

        赛后总结里念 ``00:37:58`` 很累，而且容易被读成"零点三十七分"。
        不足 1 分钟时只说秒数。
        """
        total = max(0, int(total_seconds))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分 {seconds} 秒"
        if minutes:
            return f"{minutes} 分 {seconds} 秒"
        return f"{seconds} 秒"

    def _item_line(self) -> str:
        """装备一行：满 6 件说「六神装」，否则列件数 + 前三件。"""
        if self.me is None or not self.me.items:
            return "装备：读不到"
        count = len(self.me.items)
        shown = "、".join(self.me.items[:3])
        if count >= 6:
            return f"装备：六神装（{shown} …）"
        return f"装备：{count} 件（{shown}）"

    def _highlights(self) -> list[str]:
        """本局高光：人头王、助攻王、主人的对位对比。

        全部从当前快照算，不猜。认不出主人或不满足条件时跳过对应那行。
        """
        lines: list[str] = []
        everyone = list(self.allies) + list(self.enemies)
        if not everyone:
            return lines

        top_kill = max(everyone, key=lambda p: p.kills)
        if top_kill.kills > 0:
            side = "我方" if top_kill in self.allies else "敌方"
            lines.append(f"人头王：{top_kill.champion} {top_kill.kills} 杀（{side}）")

        top_assist = max(everyone, key=lambda p: p.assists)
        if top_assist.assists > 0 and top_assist is not top_kill:
            side = "我方" if top_assist in self.allies else "敌方"
            lines.append(
                f"助攻王：{top_assist.champion} {top_assist.assists} 助攻（{side}）"
            )

        opponent = self.lane_opponent()
        if self.me is not None and opponent is not None:
            lines.append(
                f"主人对位：{self.me.champion} {self.me.kda} 对 "
                f"{opponent.champion} {opponent.kda}"
            )
        return lines

    def post_game_text(self, result: str = "") -> str:
        """赛后总结。用最后一份快照渲染。

        ``result`` 是 ``GameEnd`` 事件里的 ``Result`` 字段（``Win`` /
        ``Lose`` / ``Draw``）—— **权威结果**。

        拿不到结果时**不猜胜负**：击杀数多不代表赢了（可能是被偷家），
        猜错比不说更糟。这种情况下只报事实（时长、战绩、比分），
        胜负留白，由提示语明确告诉模型"别说输赢"。

        时长用自然语言（「37 分 58 秒」）而不是 ``00:37:58`` —— 后者念起来
        累，还容易被读成"零点三十七分"。
        """
        duration = self._human_duration(self.game_time)
        if self.me is None:
            return f"这局打完了，一共打了 {duration}。"

        label = RESULT_LABELS.get(str(result or "").strip(), "")
        head = f"对局结束：{self.map_name}·{self.game_mode}，一共打了 {duration}"
        head = f"{head}，{label}。" if label else f"{head}。"

        parts = [
            head,
            f"主人：{self.me.one_line()}",
            self._item_line(),
            self.score_line(),
        ]
        parts.extend(self._highlights())
        if self.me.deaths == 0:
            parts.append("主人这局一次没死。")
        return chr(10).join(parts)


# 排位/灵活排位里，接口 allPlayers 里**同队内部的顺序**固定是分路顺序。
# 依据：用户实测确认（2026-11-15），只有排位和灵活排位成立 ——
# 普通匹配没有位置选择流程，顺序不保证。
LANE_NAMES: tuple[str, ...] = ("上单", "打野", "中单", "ADC", "辅助")

# GameEnd 事件里的 Result 字段 → 中文。
RESULT_LABELS: dict[str, str] = {"Win": "赢了", "Lose": "输了", "Draw": "平局"}


def name_key(name: str) -> str:
    """把玩家名归一化成可比较的键。

    事件里的 ``KillerName`` / ``VictimName`` 和名单里的名字**形态经常不同**，
    直接字符串比较会大面积认不出人（2026-11-16 实测：训练模式里阵营全判
    不出来，"有人拿下了土龙"，连带龙魂播报整局失效）。

    两种实测见的差异都要吃掉：

    * ``名字#TAG`` vs ``名字`` —— 国服 riotId 带 TAG。
    * ``蛮族之王（电脑）`` vs ``蛮族之王`` —— 人机局英雄名后面挂中文括号
      后缀；英文客户端会出现 ``(Bot)`` / ``(Computer)``。

    另外还要吃大小写和首尾空白。归一化只用于**比较**，显示仍用原始名字。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    # 去掉 #TAG
    if "#" in text:
        text = text.split("#", 1)[0]
    # 去掉机器人/后缀标记：英文括号或中文括号包着的整段
    text = re.sub(r"（[^）]*）", "", text)
    text = re.sub(r"\([^)]*\)", "", text)
    text = text.strip().lower()
    # 再收一次 #TAG（有的形态是 名字(电脑)#TAG）
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    return text


def _names_in(raw: dict) -> set[str]:
    """从一份玩家/自己 JSON 里提取"这个人可能叫什么"的所有写法。

    不同区服/版本分别可能填 ``riotId``（``名字#TAG``）、``summonerName``
    或两者都有；国服常见 ``riotId``，且事件里的名字是 ``#`` 前面那段。
    归一化之后的形式也一并放进来，方便 :meth:`MatchState.side_of` 直接比。
    """
    names: set[str] = set()
    for key in ("riotId", "summonerName", "displayName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            names.add(value)
            if "#" in value:
                names.add(value.split("#", 1)[0])
    names |= {name_key(n) for n in names if name_key(n)}
    return names


def _player_from_raw(raw: dict) -> PlayerStat:
    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    raw_items = raw.get("items")
    item_list = raw_items if isinstance(raw_items, list) else []

    items: list[str] = []
    item_gold = 0.0
    for item in item_list:
        if not isinstance(item, dict):
            continue
        label = str(item.get("displayName") or "").strip()
        count = max(1, _as_int(item.get("count"), 1))
        if label:
            items.append(label if count == 1 else f"{label}x{count}")
        item_gold += _as_float(item.get("price"), 0.0) * count

    current_gold = _as_float(raw.get("currentGold"), 0.0)
    return PlayerStat(
        name=_pick_name(raw),
        champion=str(raw.get("championName") or "未知英雄").strip(),
        team=str(raw.get("team") or "").strip(),
        level=_as_int(raw.get("level"), 0),
        kills=_as_int(scores.get("kills"), 0),
        deaths=_as_int(scores.get("deaths"), 0),
        assists=_as_int(scores.get("assists"), 0),
        cs=_as_int(scores.get("creepScore"), 0),
        current_gold=current_gold,
        # 总经济 = 手上金币 + 已购装备价值。是个估算，接口不给累计经济。
        estimated_gold=current_gold + item_gold,
        is_dead=bool(raw.get("isDead")),
        respawn_timer=_as_float(raw.get("respawnTimer"), 0.0),
        items=items,
    )


def _pick_name(raw: dict) -> str:
    for key in ("riotId", "summonerName", "displayName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            return value.split("#", 1)[0] if "#" in value else value
    return ""


def load_name_map(path: Path) -> dict[str, str]:
    """读取可选的「原名 → 想显示的名字」映射表。

    用于两张表：``champion_aliases.json``（官方全称 → 常用叫法，国服主力）
    和 ``champion_names.json``（通用覆盖，国际服英文名 → 中文）。
    文件不存在、不是 JSON 对象、值不是字符串，都静默返回空表 —— 这是可选
    装饰，不该让插件起不来。
    """
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
    }


# 兼容旧名：早先只有 champion_names.json，调用方还在用这个名字。
load_champion_names = load_name_map
