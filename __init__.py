"""英雄联盟陪玩插件 —— N.E.K.O 侧入口。

职责边界（这个文件只做"接线"，不做判断）：

* ``live_client.py``  拿数据
* ``game_model.py``   解析数据
* ``cues.py``         事件 → 给猫娘的中文提示
* ``config.py``       配置
* ``narrator.py``     决定说不说、什么时候说
* 本文件              定时器、状态机、入口与 LLM 工具

安全边界：本插件**只读** Riot 官方 Live Client Data API
（``127.0.0.1:2999``）。不读游戏内存、不注入进程、不发送任何操作指令、
不碰反作弊。这是官方文档化的第三方接口，和读内存那种会被封号的路线
是两回事。

线程模型：轮询跑在 ``@timer_interval`` 的定时器线程上（有独立事件循环），
每个轮询周期内自建 ``aiohttp`` 会话，避免跨事件循环复用连接池。
LLM 工具跑在宿主的事件循环上，只读 ``self._last_state`` 这个引用。
"""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque

from .config import PluginConfig
from .cues import (
    _DRAGON_TYPES,
    _SPEAK_INSTRUCTION,
    ASSIST_BACKGROUND_HINT,
    LEGENDARY_STREAK,
    PRIORITIES,
    TIER_AMBIENT,
    TIER_CRITICAL,
    Cue,
    MyMultikill,
    OthersMultikill,
    _normalize_body,
    _parse_structure,
    _side_from_team_tag,
    build_event_cue,
    build_post_game_cue,
    build_summary_cue,
    describe_champion_kill,
    has_assists,
    is_announced,
    render_plain,
    tier_for,
)
from .game_model import MatchState, load_name_map
from .live_client import LiveClient, NotInGame
from .milestones import LaneMilestones
from .narrator import Narrator

try:  # 正常运行时走真实 SDK
    from plugin.sdk.plugin import (
        Err,
        NekoPluginBase,
        Ok,
        SdkError,
        lifecycle,
        llm_tool,
        neko_plugin,
        plugin_entry,
        timer_interval,
        tr,
        ui,
    )

    # ``@message`` 单独导入：它只服务于"用户正在聊天就少插话"这一个
    # 软性优化。万一某个 SDK 版本没有它，整个插件不该因此加载失败，
    # 退化成"没有静默窗口"即可。
    try:
        from plugin.sdk.plugin import message  # type: ignore[attr-defined]
    except ImportError:

        def message(**_kwargs):  # type: ignore[misc]
            def deco(fn):
                return fn
            return deco

except ModuleNotFoundError:  # 离线跑单元测试时的等价兜底
    def neko_plugin(cls):  # type: ignore[misc]
        return cls

    def _pass_deco(**_kwargs):  # type: ignore[misc]
        def deco(fn):
            return fn
        return deco

    plugin_entry = _pass_deco  # type: ignore[assignment]
    lifecycle = _pass_deco  # type: ignore[assignment]
    timer_interval = _pass_deco  # type: ignore[assignment]
    message = _pass_deco  # type: ignore[assignment]
    llm_tool = _pass_deco  # type: ignore[assignment]

    class _UiStub:  # type: ignore[no-redef]
        action = staticmethod(_pass_deco)
        context = staticmethod(_pass_deco)

    ui = _UiStub()  # type: ignore[assignment]

    def tr(key, default=None, **_kwargs):  # type: ignore[misc]
        return default

    class Err:  # type: ignore[no-redef]
        def __init__(self, error=None):
            self.error = error

    class Ok:  # type: ignore[no-redef]
        def __init__(self, value=None):
            self.value = value

    class SdkError(Exception):  # type: ignore[no-redef]
        pass

    class NekoPluginBase:  # type: ignore[no-redef]
        def __init__(self, ctx=None):
            self.ctx = ctx
            self.logger = None


# 定时器的最小粒度（秒）。配置里的 poll_interval_seconds 会被向上取整到这个值。
# 注意：它只在这里说明，装饰器里必须写字面量 2 —— 静态检查不认常量名。
# 连续多少次拿不到数据才判定"对局结束"。单次失败可能只是网络抖动，
# 立刻宣布结束会产出莫名其妙的赛后总结。
_GAME_OVER_FAILURES = 3
# 本局事件记录的容量上限，供 LLM 工具查询"刚才发生了什么"。
#
# 语义是"整局留存"：一局 30 分钟的峡谷对局大约 30~60 条播报，大乱斗更多。
# 这里给 500 —— 正常对局远到不了，纯粹当保险丝，防止极端情况下无界增长。
# 换局时由 _reset_match_state 清空。
_RECENT_MAX = 500
# 大乱斗：多少秒内的第二次（及以上）阵亡算"连着倒"。
_ARAM_DEATH_WINDOW_SECONDS = 20.0
# 英雄名映射表（放在插件数据目录，都可选）。
# aliases：官方全称 → 玩家常用叫法（国服主力，接口返回「虚空恐惧」这种全称）
# names：通用覆盖表，国际服的英文名 → 中文
_CHAMPION_ALIASES_FILE = "champion_aliases.json"
_CHAMPION_NAMES_FILE = "champion_names.json"

# 面板里可以逐项开关的播报类型。顺序 = 面板上的展示顺序。
# 每个 key 同时是 [neko_lol.announce] 的键、i18n 的 "events.<key>"、
# 以及 narrator 判断"要不要说"时看的那个 key。
_TOGGLE_EVENTS: tuple[str, ...] = (
    "game_start",
    "first_blood",
    "champion_kill",
    "my_death",
    "my_assist",
    "milestone",
    "multikill",
    "ace",
    "dragon_kill",
    "baron_kill",
    "herald_kill",
    "turret_killed",
    "inhib_killed",
    "game_end",
    "summary",
)

# 插件认识的全部事件类型。未知的会打日志提醒（每种一次）。
_KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "GameStart", "GameEnd", "FirstBlood", "ChampionKill", "Multikill", "Ace",
        "DragonKill", "BaronKill", "HeraldKill", "TurretKilled", "InhibKilled",
        # 这两个接口会给，但我们刻意不播报（小兵出生/首座塔）
        "MinionsSpawning", "FirstBrick",
    }
)

# 事件的中文名。面板从这个 dict（经 @ui.context 的 label 字段）读，**不走 i18n**。
# 选项文本不应该由宿主 locale 决定 —— 早先用 t("events.<key>") 时，只要
# locale 解析没落到 zh-CN，面板就会显示英文甚至原始 key。
_EVENT_LABELS: dict[str, str] = {
    "game_start": "对局开始",
    "first_blood": "一血",
    "champion_kill": "我的击杀",
    "my_death": "我的阵亡",
    "my_assist": "主人助攻（辅助位）",
    "milestone": "分路里程碑",
    "multikill": "多杀（含别人三杀）",
    "ace": "团灭",
    "dragon_kill": "小龙",
    "baron_kill": "大龙",
    "herald_kill": "峡谷先锋",
    "turret_killed": "推塔",
    "inhib_killed": "破兵营",
    "game_end": "对局结束",
    "summary": "定期小结",
}


@neko_plugin
class NekoLolPlugin(NekoPluginBase):
    """英雄联盟陪玩。"""

    def __init__(self, ctx=None):
        super().__init__(ctx)
        self._cfg = PluginConfig()
        self._client = LiveClient()
        self._narrator = Narrator(self._push, config=self._cfg.to_narrator(), log=self._log)

        # 对局状态机
        self._in_game = False
        self._session_adopted = False
        self._cursor = -1
        self._miss_count = 0
        self._miss_started_at = 0.0
        # 这局是否已经播报过对局开始。GameStart 在一局里只该出现一次，
        # 重复出现通常是接口补发或用户重复点击测试台 —— 不该再开口一次。
        self._game_start_announced = False
        # 刚播报过的一血：(被拿者, 事件时间)。同一次击杀接口会**同时**发
        # FirstBlood 和 ChampionKill，只该说"一血"那一句。
        self._first_blood_suppress: tuple[str, float] | None = None
        # 双方各拿了几条小龙。接口不给"这是第几条"，只能自己数。
        # 换局时清零（见 _adopt_session）。
        # 本局双方各拆掉几座塔。接口不给，只能数 TurretKilled 事件。
        # 用来说明"人头落后但塔数占优"这类局面（见 score_line）。
        self._turret_counts: dict[str, int] = {"ally": 0, "enemy": 0}
        self._dragon_counts: dict[str, int] = {"ally": 0, "enemy": 0}
        # 本局的龙魂属性 = **本局第 3 条被击败的小龙**的属性（不分哪方）。
        # 某方累计拿到 4 条即获得这个龙魂，与中途拿了哪些属性无关。
        # 主人自己的连续击杀数。死一次清零 —— "超神"就是这里到 8。
        self._my_streak: int = 0
        # 见过但不认识的事件类型，用来"每种只提醒一次"。
        self._unknown_events: set[str] = set()

        self._soul_type: str = ""
        # 已计入的小龙事件 EventID（防止同一条龙被重复计数）。
        self._dragon_event_ids: set[int] = set()
        # 全场小龙总数（不分阵营），用来判断"第 3 条"。
        self._dragon_total: int = 0
        self._last_state: MatchState | None = None
        # 上一份快照的对局时间。用来区分"同一局的延续"和"换了一局" ——
        # 同一局内 gameTime 只增不减，倒流即换局。
        self._last_game_time: float | None = None
        self._last_poll_at = 0.0
        self._last_summary_at = 0.0
        # 上一次小结时的**对局时间**（秒）。按它推算下一次，避免墙钟漂移。
        self._last_summary_game_time: float = 0.0
        # GameEnd 事件里的权威胜负（Win/Lose/Draw）。赛后总结用它，
        # 而不是按击杀数猜 —— 接口确实给了结果。
        self._game_result: str = ""
        # GameEnd 到了就立刻播那一条合并播报（对局结束 = 完整总结）。
        self._game_end_immediate: bool = False
        # 本局是否已经播过赛后总结 —— GameEnd 路径和数据消失路径都会播，
        # 没有这个标记就会说两遍。
        self._post_game_sent: bool = False
        self._polling = False
        self._others_multi = self._new_others_multi()
        # 主人自己的多杀（攒 4 秒取最高档，只播一句）。
        self._my_multi = MyMultikill()
        self._milestones = LaneMilestones()
        # 上一次带战绩行的时刻（单调时钟），用于冷却。0 = 还没有过。
        self._last_stats_at: float = 0.0
        # 爆发小结：本局人头发生的对局时刻（滑动窗口），以及上次爆发播报的墙钟。
        self._kill_times: Deque[float] = deque()
        self._last_burst_summary_at: float = float("-inf")
        # 本拍是否攒了一条待发的爆发小结 / 是否出现了团灭（用来取消前者）
        self._burst_pending: bool = False
        self._ace_in_batch: bool = False
        # 大乱斗死亡降噪：上一次死亡的对局时刻 + 连续阵亡次数
        self._last_aram_death_at: float = float("-inf")
        self._aram_death_run: int = 0

        self._champion_names: dict[str, str] = {}
        self._recent: Deque[tuple[str, str]] = deque(maxlen=_RECENT_MAX)
        # 本局双方阵容的一行摘要（开局生成，整局保留）。
        # 单独存一份是因为 _recent 会随播报增长，虽然已经改成整局留存，
        # 但阵容是"开局就知道、后面一直要用"的稳定信息，不该混在事件流里。
        self._roster_text: str = ""

    # ── 生命周期 ────────────────────────────────────────────────

    @lifecycle(id="startup")
    async def startup(self, **_):
        self._setup_logging()
        await self._load_config()
        self._log(
            f"[启动] 已就绪：轮询 {self._cfg.poll_interval:.0f}s，"
            f"小结间隔 {self._cfg.summary_interval:.0f}s，"
            f"干跑={'开' if self._cfg.dry_run else '关'}"
        )
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._in_game = False
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, old_config=None, new_config=None, mode="", **_):
        # 宿主改了配置就地重载，不重启进程。
        await self._load_config()
        return Ok({"status": "config_reloaded"})

    @message(id="note_user_chat", source="chat")
    async def note_user_chat(self, text: str = "", sender: str = "", **_):
        """用户说话了 → 推迟主动播报，别抢话。

        注意：这里刻意不记录 ``text`` —— 原始对话是隐私文本，
        按 N.E.K.O 的规矩只能 print，不能进 logger。我们只需要知道
        "发生过一次用户发言"。
        """
        self._narrator.note_user_chat()
        return Ok({"ok": True})

    # ── 主轮询 ──────────────────────────────────────────────────

    # seconds 必须是**字面量**：neko-plugin 的静态检查会读这个关键字，
    # 传常量名会被判成"must declare seconds > 0"而卡住上架。
    @timer_interval(id="poll_match", seconds=2, auto_start=True)
    async def poll_match(self, **_):
        if not self._cfg.enabled:
            return Ok({"status": "disabled"})

        # 重入保护。这不是洁癖：探测失败要花几秒（见 config 里对
        # http_timeout 的说明），完全可能比定时器间隔还长。一旦两次
        # 轮询重叠，游标推进和事件去重就会互相踩，所以宁可跳过这一跳。
        if self._polling:
            return Ok({"status": "busy"})

        now = time.monotonic()
        # 局内快轮询，局外降频（局外每次探测都要白跑两次连接）。
        interval = (
            self._cfg.poll_interval if self._in_game else self._cfg.idle_poll_interval
        )
        if now - self._last_poll_at < interval:
            self._narrator.flush(now)
            return Ok({"status": "idle"})
        self._last_poll_at = now

        self._polling = True
        try:
            return await self._poll_body(now)
        finally:
            self._polling = False

    async def _poll_body(self, now: float) -> Ok:
        try:
            # 一次轮询共用一个 HTTP 会话取两个端点 —— 也保证事件和战绩
            # 来自同一拍，不会错位。
            events, data = await self._client.snapshot()
        except NotInGame:
            self._on_data_missing(now)
            self._narrator.flush(now)
            return Ok({"status": "not_in_game"})

        self._miss_count = 0
        self._miss_started_at = 0.0
        state = MatchState.from_all_game_data(
            data, trust_lane_order=self._cfg.infer_lane_positions
        )
        self._last_state = state
        # 塔数由插件自己数，接口不提供 —— 每拍同步进快照，score_line 才读得到。
        state.ally_turrets = self._turret_counts["ally"]
        state.enemy_turrets = self._turret_counts["enemy"]

        if not self._in_game:
            self._in_game = True
            self._session_adopted = False
            self._log("[对局] 检测到对局数据")

        if not self._session_adopted:
            self._adopt_session(state, events, now)
        # 必须在 _adopt_session 之后再更新：它要拿这个值判断"这是不是
        # 同一局的延续"。提前赋值会让它每次都以为在延续，新局永不回放。
        self._last_game_time = state.game_time

        self._process_events(events, state, now)

        # 别人的三杀以上：攒够 settle 秒再播，一波连杀只播最高那条。
        # 提交后仍由档位（常规档）和窗口决定什么时候真正说出口。
        for cue in self._others_multi.flush(now, state):
            self._record_recent(state, cue)
            self._narrator.submit(cue, now)

        # 主人自己的多杀：攒 4 秒取最高档，只播一句。
        for cue in self._my_multi.flush(now, state):
            self._record_recent(state, cue)
            self._narrator.submit(cue, now)

        # 分路里程碑：按当前快照判断有没有跨过阶段性门槛。
        # 只播一次的那几种靠 milestones 自带的 said 集合去重。
        if is_announced("milestone", self._cfg.announce):
            for m in self._milestones.check(state):
                cue = Cue(
                    key="milestone",
                    text=f"（主人正在玩英雄联盟。{m.text}{_SPEAK_INSTRUCTION}）",
                    priority=PRIORITIES["milestone"],
                    meta={"kind": "milestone", "milestone": m.key},
                    body=_normalize_body(m.text),
                    tier=tier_for("milestone"),
                    # 里程碑是"状态总结"，不是"刚发生的事"。合进窗口会被
                    # 套上"刚才发生了这些事：他杀了个人；他 10 个助攻了"，
                    # 模型会把它当成并列事件讲，甚至说成"刚拿到第 10 个助攻"。
                    # 和定期小结同一类问题，独立播。
                    mergeable=False,
                )
                self._record_recent(state, cue)
                self._narrator.submit(cue, now)

        # 这一波安静下来了就把尾部汇总补上（没有就返回 None）。

        self._maybe_summary(state, now)

        self._narrator.flush(now)
        return Ok({"status": "in_game", "game_time": state.game_time_label})

    # ── 对局状态机 ──────────────────────────────────────────────

    def _reset_match_state(self) -> None:
        """把"本局内"的状态全部归零。

        这些字段的语义都是"本局"的：上一局的 5 连杀不该让这一局刚开局就被
        判成超神，上一局的龙魂也不该延续。集中在这里重置，免得 __init__ 和
        _adopt_session 各写一遍、哪天加了新字段漏掉一处。
        """
        self._others_multi = self._new_others_multi()
        self._my_multi = MyMultikill()
        self._milestones = LaneMilestones()
        self._game_start_announced = False
        # 上一局的播报和阵容不能带进新局 —— 否则开局问"刚才发生了什么"
        # 会答出上一局的事，问"两边都有谁"会对不上当前阵容。
        self._recent.clear()
        self._roster_text = ""
        # 刚播报过的一血：(被拿者, 事件时间)。同一次击杀接口会**同时**发
        # FirstBlood 和 ChampionKill，只该说"一血"那一句。
        self._first_blood_suppress: tuple[str, float] | None = None
        # 本局双方各拆掉几座塔（跨局要清零）。
        self._turret_counts = {"ally": 0, "enemy": 0}
        # 双方各拿了几条小龙。接口不给"这是第几条"，只能自己数。
        self._dragon_counts: dict[str, int] = {"ally": 0, "enemy": 0}
        # 爆发小结的取样窗口：新的一局必须清空，上一局的人头不能带进来。
        self._kill_times = deque()
        self._last_burst_summary_at = float("-inf")
        self._burst_pending = False
        self._ace_in_batch = False
        self._last_aram_death_at = float("-inf")
        self._aram_death_run = 0
        # 已计入的小龙事件 EventID（防止同一条龙重复计数导致龙魂偏移）。
        self._dragon_event_ids: set[int] = set()
        # 全场小龙总数（不分阵营），用来判断"第 3 条"。
        self._dragon_total: int = 0
        # 本局龙魂属性 = 本局第 3 条被击败的小龙（不分哪方）。
        self._soul_type: str = ""
        # 主人自己的连续击杀数。死一次清零 —— "超神"就是这里到 8。
        self._my_streak: int = 0
        # 见过但不认识的事件类型，用来"每种只提醒一次"。
        self._unknown_events: set[str] = set()
        # 上一局的小结进度和结算结果都不能带进新局。
        self._last_summary_game_time: float = 0.0
        self._game_result: str = ""
        # GameEnd 到了就立刻播那一条合并播报（对局结束 = 完整总结）。
        self._game_end_immediate: bool = False
        # 本局是否已经播过赛后总结 —— GameEnd 路径和数据消失路径都会播，
        # 没有这个标记就会说两遍。
        self._post_game_sent: bool = False

    def _adopt_session(self, state: MatchState, events: list[dict], now: float) -> None:
        """接管一局对局，决定要不要回放已经发生过的事件。

        三种情况必须区别对待：

        * **同一局的延续**（对局开始后短暂断流又恢复）—— 保留事件游标，
          绝不重放。早先的写法在这里把游标清成 -1，结果局内断一次流就
          会把开局到现在的事件全部重播一遍。
        * 对局**刚刚开始**（gameTime 很小）—— 从 EventID 0 开始处理，
          这样 GameStart、一血这些开场事件不会被吞掉。
        * 插件**中途**启动/重载（gameTime 已经很大）—— 把游标推到当前
          最大 EventID，否则一启动就会把整局历史重放给猫娘。

        区分"延续"和"新的一局"靠的是对局时间：同一局内 gameTime 只增不减，
        所以时间倒流就意味着换了一局。
        """
        self._session_adopted = True
        self._last_summary_at = now

        prev_time = self._last_game_time
        if prev_time is not None and state.game_time >= prev_time:
            # 同一局的延续：游标和人头合并器都原样保留，绝不重放。
            self._log(
                f"[对局] 数据恢复（{state.game_time:.0f}s），保持进度继续，不回放"
            )
            return

        # 走到这里说明是新的一局（或首次接管）—— 本局状态全部归零。
        self._reset_match_state()

        if prev_time is not None:
            self._log(
                f"[对局] 检测到新对局（时间从 {prev_time:.0f}s 回到 "
                f"{state.game_time:.0f}s）"
            )

        if state.game_time > self._cfg.join_replay_max_game_time:
            self._cursor = max((_event_id(e) for e in events), default=-1)
            # 中途加入时也不该一上来就播定期小结（"这是当前战况"）。
            # 定期小结现在是直接发、不进窗口的，不压掉的话接管那一拍就会
            # 冒出半局战况。把它记成"已经播过"，下一次落到 +interval。
            self._last_summary_game_time = state.game_time
            self._log(
                f"[对局] 中途加入（已进行 {state.game_time:.0f} 秒），"
                f"从当前进度开始播报，不回放历史"
            )
            # 连杀数/小龙数无法从接口恢复（它只给累计 KDA，不给"当前连杀"；
            # 小龙总数也不在快照里）。所以这里计数从 0 开始 —— 代价是
            # 这一局的"超神""龙魂"可能晚触发一次。写进日志便于排查，
            # 免得日后看到"8 连杀了怎么没播超神"一头雾水。
            self._log(
                "[对局] 中途加入：连杀/小龙计数从 0 开始"
                "（接口不提供当前连杀），本局超神与龙魂判定可能偏晚"
            )
        else:
            self._cursor = -1
            self._log(f"[对局] 新对局开始：{state.map_name}·{state.game_mode}")

    def _emit_post_game(self, now: float) -> None:
        """播一次赛后总结。已经播过就不再播（GameEnd 路径优先）。"""
        if self._post_game_sent:
            return
        if not self._cfg.post_game_summary or self._last_state is None:
            return
        # 面板上的「对局结束」开关 —— 现在它是唯一的结束播报，
        # 关掉就等于"对局结束什么都不说"。
        if not is_announced("game_end", self._cfg.announce):
            self._log("[播报] 对局结束播报已在面板上关闭，跳过")
            return
        # 窗口里还攒着的推塔/兵营等 —— 对局结束了，再等窗口就没机会说，
        # 所以并进这一条一起报，而不是先单独发一轮。
        pending = self._narrator.take_pending_bodies()
        cue = build_post_game_cue(
            self._last_state,
            champion_names=self._champion_names,
            result=self._game_result,
            pending_items=pending,
        )
        self._record_recent(self._last_state, cue)
        # 赛后总结错过就永远没机会说了，跳过所有间隔检查。
        self._narrator.submit(cue, now, force=True)
        self._post_game_sent = True

    def _on_data_missing(self, now: float) -> None:
        """拿不到数据。可能是没在打，也可能只是一次抖动。"""
        if not self._in_game:
            return
        self._miss_count += 1
        if self._miss_started_at <= 0.0:
            self._miss_started_at = now
        elapsed = now - self._miss_started_at

        # 两个条件都要满足才判定结束：次数够多 **且** 持续够久。
        # 只看次数的话，客户端抖一下就会放出假的赛后总结。
        if self._miss_count < _GAME_OVER_FAILURES or elapsed < self._cfg.game_over_grace:
            self._log(
                f"[对局] 数据中断（第 {self._miss_count} 次，已持续 {elapsed:.0f} 秒）"
            )
            return

        self._in_game = False
        self._session_adopted = False
        self._miss_count = 0
        self._miss_started_at = 0.0
        self._log("[对局] 对局数据持续消失，判定本局结束")

        # 把窗口里还没到期的人头先捞出来。接口一消失这些片段就没了，
        # 而"水晶爆炸前刚拿的那个人头"恰恰是值得说的。
        # 先把窗口里攒着的结算掉 —— 对局都结束了，再等窗口就没意义了。
        # 必须排在赛后总结之前：总结是 force 的，会先把自己那份发掉。
        self._narrator.force_flush(now)

        for cue in self._others_multi.force_flush(self._last_state):
            self._record_recent(self._last_state, cue)
            self._narrator.submit(cue, now)

        # 主人自己的多杀也别落下：如果刚拿完就结束，攒着的那句要放出来。
        for cue in self._my_multi.force_flush(self._last_state):
            self._record_recent(self._last_state, cue)
            self._narrator.submit(cue, now)

        self._emit_post_game(now)

        # 刻意**不**重置 self._cursor：万一是误判，恢复后还要靠它接着往下走。
        # 真正换局时由 _adopt_session 依据 gameTime 倒流来重置。

    def _note_unknown_event(self, name: str) -> None:
        """没处理过的事件类型，每种只打一次日志。

        接口新增事件（比如 FirstBrick）时，静默丢弃会让它永远不被发现。
        用集合去重，避免每轮轮询都刷一行。
        """
        if name in self._unknown_events:
            return
        self._unknown_events.add(name)
        self._log(f"[事件] 未识别的事件类型：{name}（已忽略，需要的话可以加播报）")

    def _process_events(self, events: list[dict], state: MatchState, now: float) -> None:
        """把本轮的新事件分发给对应的处理器。

        只做三件事：过滤、预扫一血、逐个分发。具体每种事件怎么播，
        在 ``_handle_*`` 里 —— 早先全塞在这一个函数里（200 多行），
        改一处要读完整个函数才敢动手。
        """
        # 过滤掉非 dict 的垃圾项：接口偶发返回半截数据时，轮询循环必须活着。
        fresh = sorted(
            (
                e
                for e in events
                if isinstance(e, dict) and _event_id(e) > self._cursor
            ),
            key=_event_id,
        )
        if not fresh:
            return

        # 本拍是否出现团灭 —— 爆发小结要等整拍处理完才知道，见 _flush_burst_summary。
        self._ace_in_batch = False

        # 预扫一血：同一次"一血"接口会同时发 FirstBlood 和 ChampionKill 两条，
        # 先记下被拿者，后面好把对应的人头跳过。
        self._prescan_first_blood(fresh)

        for event in fresh:
            self._cursor = max(self._cursor, _event_id(event))
            name = str(event.get("EventName") or "")
            if name and name not in _KNOWN_EVENTS:
                self._note_unknown_event(name)

            if name == "ChampionKill":
                self._handle_champion_kill(event, state, now)
            elif name == "Multikill":
                self._handle_multikill(event, state, now)
            elif name == "Ace":
                self._ace_in_batch = True
                self._handle_ace(event, state, now)
            elif name == "DragonKill":
                self._handle_dragon(event, state, now)
            elif name in ("TurretKilled", "InhibKilled"):
                self._note_structure_if_unparsed(event, name, state)
                if name == "TurretKilled":
                    self._count_turret(event, state)
                self._handle_plain_event(event, state, now)
            elif name == "GameEnd":
                self._record_game_result(event)
                # 对局结束了 —— 这条播报就是**唯一的结束播报**：
                # 「这局结束了 + 完整总结」，并且把窗口里还没说出口的
                # 推塔/兵营等也一并带上（_emit_post_game 里取）。
                # 标记成"本拍结束就发"，等这批事件全部处理完再发 —— 否则
                # GameEnd 后面还跟着的人头/推塔会漏算（接口偶发把 GameEnd
                # 夹在中间）。
                self._game_end_immediate = True
            else:
                self._handle_plain_event(event, state, now)

        # 整拍处理完再结算爆发小结。
        self._flush_burst_summary(state, now)

        # GameEnd 到了就立刻播那一条合并播报。
        if self._game_end_immediate:
            self._game_end_immediate = False
            self._emit_post_game(now)

    # ── 事件处理器 ──────────────────────────────────────────────

    def _prescan_first_blood(self, events: list[dict]) -> None:
        """记下本批里的一血归属，供 ChampionKill 抑制用。"""
        for event in events:
            if str(event.get("EventName") or "") != "FirstBlood":
                continue
            recipient = str(event.get("Recipient") or "").strip()
            if recipient:
                self._first_blood_suppress = (
                    recipient,
                    float(event.get("EventTime") or 0.0),
                )

    def _announces_assists(self, state: MatchState) -> bool:
        """要不要播"主人助攻"的人头。

        只给辅助位开：辅助很少拿人头，只播主人击杀的话一整局几乎不吭声。
        条件是**同时**成立：

        * 分路可信 —— ``lanes_trusted`` 已经把「召唤师峡谷 + 征召模式 +
          用户打开 infer_lane_positions」三层卡住了（接口不给队列类型，
          排位和普通匹配在这一层无法区分，所以依赖那个开关的前提）。
        * 主人就是辅助（同队顺序里的第 5 位）。
        * 走的是"只播主人的事"模式 —— ``only_my_events=false`` 时队友人头
          本来就会播，再补一条助攻是重复。
        * 面板上「主人助攻」开关没被关掉。
        """
        if not self._cfg.only_my_events:
            return False
        if not is_announced("my_assist", self._cfg.announce):
            return False
        return bool(state.lanes_trusted and state.my_lane == "辅助")

    def _handle_champion_kill(self, event: dict, state: MatchState, now: float) -> None:
        """人头 / 阵亡。带连杀计数与超神判定。"""
        # 爆发小结的取样必须放在最前面 —— 团战两边都在死人，不能只数主人的。
        self._note_kill_for_burst(state, now)

        if self._is_first_blood_duplicate(event):
            return

        # 里程碑需要逐次累计（对位单杀次数、转移击杀次数、被对位抓没抓）。
        self._milestones.note_kill(event, state)

        described = describe_champion_kill(
            event,
            state,
            champion_names=self._champion_names,
            only_mine=self._cfg.only_my_events,
            announce_my_assists=self._announces_assists(state),
            # 排位模式（峡谷 + 征召 + 用户开了分路推断）才给英雄名补分路。
            # lanes_trusted 已经把这三层都卡住了，普通匹配不会走到这里。
            with_lane=state.lanes_trusted,
        )
        # None = 队友的人头 / 队友被杀 / 对面互杀。这些不关主人的事。
        if described is None:
            return

        fragment, kind = described
        streak_before = self._my_streak
        if kind == "my_kill":
            self._my_streak += 1
        elif kind == "my_death":
            self._my_streak = 0

        streak_note = self._streak_note_for(kind, streak_before)

        # 大乱斗降噪：死亡只播有信息量的（连杀被断 / 短时间连续阵亡）。
        if kind == "my_death":
            notable, extra_note = self._aram_death_filter(state, streak_before)
            if not notable:
                return
            if extra_note and not streak_note:
                streak_note = extra_note

        if kind == "my_death":
            key = "my_death"
        elif kind == "my_assist":
            key = "my_assist"
        else:
            key = "champion_kill"
        if not is_announced(key, self._cfg.announce):
            return

        body = _normalize_body(f"{fragment}{streak_note}")

        # 有助攻时追加一条约束：助攻只是背景，别特意点名谁帮的。
        # 只加在"主人击杀/阵亡"上 —— 辅助位那条本身就是助攻播报，
        # 再告诉模型"别提助攻"会自相矛盾。
        assist_hint = ""
        has_assist_data = has_assists(
            event,
            state,
            self._champion_names,
            exclude={
                str(event.get("KillerName") or "").strip(),
                str(event.get("VictimName") or "").strip(),
            },
        )
        if kind in ("my_kill", "my_death") and has_assist_data:
            assist_hint = ASSIST_BACKGROUND_HINT

        cue = Cue(
            key=key,
            # body 已经被 _normalize_body 补过句号，这里不能再加一个，
            # 否则会拼出"影流之主。。"
            text=f"（主人正在玩英雄联盟。{body}{assist_hint}{_SPEAK_INSTRUCTION}）",
            priority=PRIORITIES[key],
            meta={"kind": kind, "streak": self._my_streak},
            body=body,
            tier=self._kill_tier(key, streak_note),
            has_assists=has_assist_data,
        )
        # 大乱斗：人头/阵亡降成环境档，一波团合并成一句。
        # 例外——带连杀附注的（超神/被终结）仍是关键档，那是真高光。
        if (
            state.is_aram
            and self._cfg.aram_kills_ambient
            and not streak_note
            and kind in ("my_kill", "my_death")
        ):
            cue.tier = TIER_AMBIENT
        self._record_recent(state, cue)
        self._narrator.submit(cue, now)

    def _aram_death_filter(
        self, state: MatchState, streak_before: int
    ) -> tuple[bool, str]:
        """大乱斗的死亡要不要播。返回 (是否播, 附注)。

        ARAM 里一个玩家一局能死 15~20 次，逐条播是纯噪音。只保留两种有
        信息量的死亡：

        * **连杀被断**（死前 ≥3 连杀）→ "连杀被断了"；
        * **短时间连续阵亡**（20 秒内凑成第二次）→ "这波连着倒了两次"。
          只在凑成第二次那一刻播一次；后面继续死不再刷。
        """
        if not (state.is_aram and self._cfg.aram_death_only_notable):
            return True, ""

        moment = state.game_time
        gap = moment - self._last_aram_death_at
        if 0 <= gap <= _ARAM_DEATH_WINDOW_SECONDS:
            self._aram_death_run += 1
        else:
            self._aram_death_run = 1
        self._last_aram_death_at = moment

        notes: list[str] = []
        if streak_before >= 3:
            notes.append(f"连杀被断了（本来 {streak_before} 连杀）")
        # 连续阵亡只在"凑成第二次"那一刻播一次。否则连死 5 次会播 5 条，
        # 每条都带"连着倒下 N 次"，等于把噪音换个说法。
        if self._aram_death_run == 2:
            notes.append("这波连着倒了两次")
        if not notes:
            return False, ""
        return True, "，" + "，".join(notes)

    def _is_first_blood_duplicate(self, event: dict) -> bool:
        """这条击杀是不是刚播过的一血那一击。是则消耗标记并返回 True。"""
        suppress = self._first_blood_suppress
        if suppress is None:
            return False

        killer = str(event.get("KillerName") or "").strip()
        victim = str(event.get("VictimName") or "").strip()
        event_time = float(event.get("EventTime") or 0.0)
        if suppress[0] not in (killer, victim) or abs(event_time - suppress[1]) > 3.0:
            return False

        self._first_blood_suppress = None
        self._log(f"[一血] 抑制同一击杀的人头播报（{killer} → {victim}）")
        return True

    def _streak_note_for(self, kind: str, streak_before: int) -> str:
        """超神 / 差一个头被终结时的额外文本。其余情况返回空串。

        用逗号接在前半句后面（不另起一句）：正文会被 `_normalize_body`
        统一补成「。」结尾，早先单独写" 主人超神了！"时那个感叹号会被
        换成句号，读起来是两句话硬挤在一起。
        """
        if kind == "my_kill" and self._my_streak == LEGENDARY_STREAK:
            self._log("[连杀] 主人达到 8 连杀 → 超神")
            return "，主人超神了"
        if kind == "my_death" and streak_before == LEGENDARY_STREAK - 1:
            self._log(f"[连杀] 主人 {streak_before} 连杀被终结")
            return "，被终结了，可惜还差一个人头就能超神"
        return ""

    def _kill_tier(self, key: str, streak_note: str) -> int:
        """普通击杀走常规档；超神/被终结提到关键档。"""
        if streak_note:
            return TIER_CRITICAL
        return tier_for(key)

    def _handle_multikill(self, event: dict, state: MatchState, now: float) -> None:
        """多杀。主人和别人的都先攒一下，取最高连杀数只播一句。"""
        killer = str(event.get("KillerName") or "")
        if state.is_me(killer):
            # 主人自己的多杀：攒 4 秒取最高档再播。
            # 早先这里一律 return（理由是"ChampionKill 已经逐条说过每个人头"），
            # 但那只解释了为什么不必播"三杀"的过程，不能解释为什么连
            # "五杀"都不提 —— 2026-11-16 主人拿五杀，日志里只有普通击杀。
            # 逐条播也不行：KillStreak 是累计数，连杀过程里几条只隔一两秒，
            # 会连喊"三杀、四杀、五杀"好几声。
            try:
                streak = int(event.get("KillStreak"))
            except (TypeError, ValueError):
                return
            self._my_multi.add(streak, now)
            return
        if self._cfg.others_multikill_min_streak <= 0:
            return
        self._others_multi.add(
            killer,
            _as_int(event.get("KillStreak")),
            state,
            now,
            champion_names=self._champion_names,
        )

    def _allow_stats(self, now: float) -> bool:
        """这次播报要不要带战绩行。

        资源类事件一场能出十几个，每条都挂"主人现在 8—1—0，16 级"再加一句
        格式说明，整局会像在读报表（2026-11-16 用户反馈"带战绩太多了"）。
        这里按时间冷却：距离上次带战绩的播报不足 ``stats_cooldown`` 秒就省掉。
        数字本身仍可从小结/赛后总结/对话工具里拿到。
        """
        if self._cfg.stats_cooldown <= 0:
            return True
        if self._last_stats_at <= 0:
            return True
        return (now - self._last_stats_at) >= self._cfg.stats_cooldown

    def _mark_stats(self, now: float) -> None:
        self._last_stats_at = now

    def _handle_ace(self, event: dict, state: MatchState, now: float) -> None:
        if not is_announced("ace", self._cfg.announce):
            return
        allow_stats = self._allow_stats(now)
        cue = build_event_cue(
            event, state,
            champion_names=self._champion_names,
            only_mine=self._cfg.only_my_events,
            include_stats=allow_stats,
        )
        if cue is None:
            return
        if cue.has_stats:
            self._mark_stats(now)
        self._record_recent(state, cue)
        self._narrator.submit(cue, now)

    def _handle_dragon(self, event: dict, state: MatchState, now: float) -> None:
        """小龙：先累加计数（第三/四条要提龙魂），再走通用播报。"""
        self._count_dragon(event, state)
        self._handle_plain_event(event, state, now)

    def _count_turret(self, event: dict, state: MatchState) -> None:
        """数一座塔。归属看结构名里的队伍 tag（T1/T2、TOrder/TChaos）。

        接口的 allPlayers 不给塔数，只能自己数 —— 赛后总结靠它解释
        "人头落后但塔数占优"这种局面。
        """
        info = _parse_structure(event.get("TurretKilled"))
        side = _side_from_team_tag(info["raw_team"], state)
        if side in self._turret_counts:
            self._turret_counts[side] += 1
            self._log(
                f"[塔] {side} 第 {self._turret_counts[side]} 座"
                f"（{event.get('TurretKilled')!r}）"
            )

    def _count_dragon(self, event: dict, state: MatchState) -> None:
        """累加双方小龙数与全场总数，第 3 条时锁定龙魂属性。

        远古巨龙**不计入** —— 它是决胜龙，只给增益。计入的话，一方拿满
        3 条元素龙 + 1 条远古后计数变 4，真正的第 4 条元素龙会变成第 5 条，
        龙魂播报永远不出现。
        """
        # 同一条龙只能计一次。这条去重不是洁癖：龙魂是按"全场第 3 条"锁定的，
        # 多计一条就会把龙魂锁定到更早的龙上，整局的龙魂播报全错。
        eid = _event_id(event)
        if eid in self._dragon_event_ids:
            self._log(f"[小龙] 跳过重复事件 #{eid}（已计过，避免龙魂锁定偏移）")
            return
        self._dragon_event_ids.add(eid)

        side = state.side_of(str(event.get("KillerName") or "").strip())
        dtype_raw = str(event.get("DragonType") or "")
        dtype_cn = _DRAGON_TYPES.get(dtype_raw)
        if dtype_cn is None:
            dtype_cn = "小龙"
            self._log(f"[小龙] 未知的 DragonType：{dtype_raw!r}，请补 _DRAGON_TYPES")

        # 远古巨龙不算龙魂（见 docstring）。
        if "远古" in dtype_cn:
            self._log(f"[小龙] 远古巨龙，不计入龙魂计数（{side}）")
            return

        # 认不出阵营也**要计入总数** —— 它毕竟是本局的一条龙。早先这里直接
        # return，于是"有人拿下了土龙"这种认不出阵营的龙会让总数少 1，
        # 龙魂被锁定到后面那条龙上（2026-11-16 实测到的偏移）。
        if side in self._dragon_counts:
            self._dragon_counts[side] += 1
        else:
            self._log(
                f"[小龙] 认不出击杀者阵营（{event.get('KillerName')!r}），"
                f"仍计入总数但不算进任何一方"
            )

        self._dragon_total += 1
        if self._dragon_total == 3 and not self._soul_type:
            self._soul_type = dtype_cn
            self._log(f"[小龙] 本局第 3 条已出现，龙魂属性 = {dtype_cn}")

        detail = (
            f"{side} 第 {self._dragon_counts[side]} 条"
            if side in self._dragon_counts
            else "阵营未知 第 ? 条"
        )
        # 每条都打全场序号：龙魂判定只看这个数，出问题时一眼能对出来。
        self._log(
            f"[小龙] 本局第 {self._dragon_total} 条，{detail}（{dtype_cn}）"
        )

    def _note_structure_if_unparsed(
        self, event: dict, name: str, state: MatchState | None = None
    ) -> None:
        """结构物解析失败 / 击杀者认不出时打日志，便于排查。

        两件事都要能查：
        * 结构名格式变了（解析不出路）；
        * 击杀者不在名单里（播报会退化成"有人拆掉了…"）。
        2026-11-16 实测有三座塔的击杀者认不出，但当时没有任何日志可查。
        """
        field = event.get("TurretKilled" if name == "TurretKilled" else "InhibKilled")
        if not _parse_structure(field)["lane"]:
            self._log(
                f"[结构物] 解析不出路：raw={field!r} "
                f"killer={event.get('KillerName')!r}"
            )
        killer = str(event.get("KillerName") or "").strip()
        if state is not None and killer and state.side_of(killer) == "unknown":
            self._log(
                f"[结构物] 认不出击杀者阵营：killer={killer!r} "
                f"（播报会退化成「有人拆掉了…」）"
            )

    def _record_game_result(self, event: dict) -> None:
        """GameEnd 带着权威结果（Win/Lose/Draw），赛后总结要用它。"""
        self._game_result = str(event.get("Result") or "").strip()
        self._log(f"[对局] 结算结果：{self._game_result or '未提供'}")

    def _handle_plain_event(self, event: dict, state: MatchState, now: float) -> None:
        """其余事件：生成 cue → 去重开局 → 提交。"""
        allow_stats = self._allow_stats(now)
        cue = build_event_cue(
            event, state,
            champion_names=self._champion_names,
            only_mine=self._cfg.only_my_events,
            dragon_counts=self._dragon_counts,
            soul_type=self._soul_type,
            include_stats=allow_stats,
        )
        if cue is None:
            return
        # 只有真的带了战绩行才重置冷却 —— 开局/推塔这些本来就不带的
        # 事件不该影响下一次带战绩的时机。
        if cue.has_stats:
            self._mark_stats(now)

        # 一局只有一个开局。重复的 GameStart（接口补发、或测试台连点）
        # 放过去就会说两次开局。
        if cue.key == "game_start":
            if self._game_start_announced:
                return
            self._game_start_announced = True
            # 开局就把阵容定下来：整局保留在 _roster_text，并写一条进
            # _recent，这样"刚才发生了什么"里第一条就是阵容。
            self._roster_text = self._build_roster_text(state)
            self._recent.append((state.game_time_label, "阵容｜" + self._roster_text))
            self._log(f"[对局] 阵容：{self._roster_text}")

        self._record_recent(state, cue)
        self._narrator.submit(cue, now)

    def _note_kill_for_burst(self, state: MatchState, now: float) -> None:
        """记录人头发生时刻，判断要不要因为"一波团战"补一次小结。

        判定用的是**对局时间**（人头按 GameTime 排序稳定），冷却用墙钟
        （避免时间倒流/暂停时误判）。
        """
        window = self._cfg.summary_burst_window
        moment = state.game_time
        self._kill_times.append(moment)
        # 只留窗口内的
        while self._kill_times and moment - self._kill_times[0] > window:
            self._kill_times.popleft()

        if moment < self._cfg.summary_burst_after:
            return          # 前 15 分钟不启用（用户 2026-11-17 指定）
        # 大乱斗 5 人/30 秒是常态，用它那套（默认 8 人 / 120 秒冷却）会
        # 整局刷十几条小结；这里按地图取不同的门槛。
        if state.is_aram:
            need = self._cfg.aram_burst_kills
            cooldown = self._cfg.aram_burst_cooldown
        else:
            need = self._cfg.summary_burst_kills
            cooldown = self._cfg.summary_burst_cooldown
        if len(self._kill_times) < need:
            return
        if now - self._last_burst_summary_at < cooldown:
            return
        if not is_announced("summary", self._cfg.announce):
            return

        # 不在这里直接发 —— 团灭事件通常紧跟在第 5 个人头后面同一拍到达，
        # 当场发就会出现"当前战况：…"后立刻又一句"主人这边团灭了对面"。
        # 记成待发，等本拍处理完再决定（见 _flush_burst_summary）。
        self._burst_pending = True

    def _flush_burst_summary(self, state: MatchState, now: float) -> None:
        """本拍结束时结算爆发小结。团灭已经把这一波说清楚了，就跳过。"""
        if not self._burst_pending:
            return
        self._burst_pending = False
        self._kill_times.clear()
        self._last_burst_summary_at = now
        # 顺手把定期小结的进度也推一格：这一波刚播过，下一轮不该马上又来。
        self._last_summary_game_time = state.game_time
        if self._ace_in_batch:
            self._log("[小结] 本拍有团灭，跳过爆发小结（团灭已经说了这波的事）")
            return
        need = (
            self._cfg.aram_burst_kills if state.is_aram
            else self._cfg.summary_burst_kills
        )
        self._log(
            f"[小结] 对局 {state.game_time_label} 爆发触发"
            f"（{self._cfg.summary_burst_window:.0f} 秒内 {need} 个人头）"
        )
        cue = build_summary_cue(state, champion_names=self._champion_names)
        self._record_recent(state, cue)
        self._narrator.submit(cue, now)

    def _maybe_summary(self, state: MatchState, now: float) -> None:
        """定期小结：对局第 15 分钟首播，之后每 5 分钟一次。

        调度基准是**对局时间**而不是墙钟 —— 暂停/断流后接上时，播报点
        仍然落在 15:00、20:00、25:00 这些整洁的刻度上。
        另有一条独立的"爆发小结"（见 _note_kill_for_burst）。
        """
        # 大乱斗系（嚎哭深渊 map 12）节奏快得多：第 8 分钟首播、每 3 分钟一次。
        # 海克斯大乱斗等变种也在这张地图上，靠 mapNumber 一并覆盖。
        if state.is_aram:
            interval = self._cfg.aram_summary_interval
            first_at = self._cfg.aram_summary_first_at
        else:
            interval = self._cfg.summary_interval
            first_at = self._cfg.summary_first_at
        if interval <= 0:
            return
        if state.game_time < first_at:
            return

        # 下一次该播的对局时刻：首播点 + N×间隔。
        due = first_at
        if self._last_summary_game_time > 0:
            due = self._last_summary_game_time + interval
        if state.game_time + 1e-6 < due:
            return

        self._last_summary_game_time = state.game_time
        self._last_summary_at = now
        # 定时小结也要占住爆发小结的冷却位，否则第 15 分钟那一刻会出现
        # "定时小结刚播完 4 秒，爆发小结又来一条"。
        self._last_burst_summary_at = now
        cue = build_summary_cue(state, champion_names=self._champion_names)
        self._record_recent(state, cue)
        # 日志写在 submit **之前**：小结现在是不可合并档、在 submit 里直接
        # 发，先 submit 会看到"已发送"排在"[小结] 播报"前面，读日志时顺序反了。
        self._log(
            f"[小结] 对局 {state.game_time_label} 播报"
            f"（下次约 {int(due + interval) // 60} 分钟）"
        )
        self._narrator.submit(cue, now)

    # ── 播报 ────────────────────────────────────────────────────

    def _push(self, text: str, meta: dict[str, Any]) -> dict[str, Any]:
        """真正把话递给猫娘的通道。

        ``ai_behavior="respond"`` 让模型读进去并回一句 —— 这就是桌面宠物
        带语音说话的那条正规通道。
        """
        # visibility 和 ai_behavior 是**独立**的两件事：
        #   * visibility 只决定"看不看得到"，空列表 = 聊天/HUD 里都不显示；
        #   * ai_behavior="respond" 仍然让模型读进去并回一句（带语音）。
        # 所以默认关掉时，猫娘照样说话，只是那段括号提示不占聊天窗口。
        result = self.push_message(
            source="neko_lol",
            visibility=["chat"] if self._cfg.show_prompt_in_chat else [],
            ai_behavior="respond",
            parts=[{"type": "text", "text": text}],
            metadata=meta,
        )
        return result if isinstance(result, dict) else {"submitted": True}

    def _build_roster_text(self, state: MatchState) -> str:
        """本局双方阵容的一行摘要。

        排位/灵活排位（``lanes_trusted``）时给每个英雄补分路后缀，例如
        ``虚空恐惧（上单）``；普通匹配和大乱斗不补 —— 那些模式的数组顺序
        不代表分路，硬加等于编造。

        认不出某个人时不写空括号，直接只留英雄名。
        """

        def label(player) -> str:
            champion = player.champion or player.name or "未知"
            if not state.lanes_trusted:
                return champion
            lane = state.lane_of(player.name)
            return f"{champion}（{lane}）" if lane else champion

        allies = "、".join(label(p) for p in state.allies) or "读不到"
        enemies = "、".join(label(p) for p in state.enemies) or "读不到"
        return f"我方：{allies} ｜ 敌方：{enemies}"

    def _record_recent(self, state: MatchState, cue: Cue) -> None:
        """记一份"刚才发生了什么"，供 LLM 工具查询。"""
        self._recent.append((state.game_time_label, render_plain(cue)))

    def _new_others_multi(self) -> OthersMultikill:
        """别人连杀的处理器。阈值 0 表示完全不说。"""
        return OthersMultikill(
            settle_seconds=self._cfg.others_multikill_settle_seconds,
            min_streak=max(1, self._cfg.others_multikill_min_streak),
        )

    def _log(self, message: str) -> None:
        logger = getattr(self, "logger", None)
        if logger is not None:
            try:
                logger.info(message)
                return
            except Exception:
                pass
        print(f"[neko_lol] {message}")

    # ── 配置 ────────────────────────────────────────────────────

    def _setup_logging(self) -> None:
        level = (os.environ.get("NEKO_LOG_LEVEL") or "INFO").strip().upper()
        try:
            self.logger = self.enable_file_logging(log_level=level)
        except Exception:
            pass  # 基类 logger 已经够用，不要为日志配置把插件搞崩

    async def _load_config(self, raw: dict[str, Any] | None = None) -> None:
        """加载配置。``raw`` 传入时直接用（测试注入），否则问宿主拿。"""
        if raw is None:
            raw = {}
            try:
                dumped = await self.config.dump()
                if isinstance(dumped, dict):
                    raw = dumped
            except Exception as exc:
                self._log(f"[配置] 读取失败，全部使用默认值：{type(exc).__name__}: {exc}")

        section = raw.get("neko_lol")
        self._cfg = PluginConfig.from_raw(section)
        for warning in self._cfg.invariant_warnings():
            self._log(f"[配置] 冲突：{warning}")

        unknown = PluginConfig.unknown_keys(section)
        if unknown:
            self._log(
                f"[配置] 有 {len(unknown)} 个键不认识，已忽略（多半是拼错或写错了段）："
                f"{'、'.join(sorted(unknown))}"
            )
        self._client = LiveClient(
            base_url=self._cfg.base_url,
            timeout=self._cfg.http_timeout,
            allow_http_fallback=self._cfg.allow_http_fallback,
        )
        self._narrator.update_config(self._cfg.to_narrator())
        # 两张表合并：别名表优先（它管的是国服返回的官方全称）。
        aliases = load_name_map(self._resolve_overlay_path(_CHAMPION_ALIASES_FILE))
        names = load_name_map(self._resolve_overlay_path(_CHAMPION_NAMES_FILE))
        self._champion_names = {**names, **aliases}
        if aliases or names:
            self._log(
                f"[英雄名] 已加载映射 {len(aliases)} 条别名 / {len(names)} 条覆盖"
            )

    def _resolve_overlay_path(self, filename: str) -> Path:
        """覆盖表的位置。拿不到 data 目录就退到插件安装目录。"""
        try:
            return Path(self.data_path(filename))
        except Exception:
            base = getattr(self, "storage_dir", None) or getattr(self, "plugin_dir", None)
            if isinstance(base, Path):
                return base / filename
            return Path(filename)

    # ── 给 LLM 的工具 ───────────────────────────────────────────

    @llm_tool(
        name="lol_match_status",
        description=(
            "查询主人当前英雄联盟对局的实时状态：是否在局内、对局时间、自己的英雄/"
            "KDA/等级/装备、双方比分与十人名单。"
            "当主人问'我这局打得怎么样''现在什么局势''我几个头了'时调用。"
            "英雄名接口给的是官方全称（如'虚空恐惧'），回答时换成玩家常用的"
            "简短叫法或外号（如'大虫子'）。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def lol_match_status(self) -> dict:
        state = self._last_state
        if not self._in_game or state is None:
            return {
                "in_game": False,
                "hint": "主人当前不在英雄联盟对局中（或刚进对局、数据还没就绪）。直接如实回答即可，不要编造战况。",
            }
        return {
            "in_game": True,
            "game_time": state.game_time_label,
            "map": state.map_name,
            "mode": state.game_mode,
            "me": _player_dict(state.me),
            "score": state.score_line(),
            # 阵容摘要（排位带分路后缀）。开局就定下，整局不变。
            "roster": self._roster_text or self._build_roster_text(state),
            "allies": [_player_dict(p) for p in state.allies],
            "enemies": [_player_dict(p) for p in state.enemies],
            # 整局留存，不再只给最近 6 条 —— 主人问"开局谁打谁"要能答上。
            "recent_events": [text for _clock, text in self._recent],
        }

    @llm_tool(
        name="lol_player_status",
        description=(
            "查询当前对局里某一名玩家或某个英雄的实时状态（KDA、等级、装备、"
            "是否阵亡）。参数可以填英雄名（如「虚空恐惧」或 Yasuo）或玩家名，不区分大小写。"
            "当主人问'对面亚索发育怎么样''对面打野几级了'时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "who": {
                    "type": "string",
                    "description": "英雄名（如「虚空恐惧」「诺克萨斯之手」或 Yasuo）或玩家名。",
                },
            },
            "required": ["who"],
        },
    )
    async def lol_player_status(self, *, who: str = "") -> dict:
        state = self._last_state
        if not self._in_game or state is None:
            return {"found": False, "hint": "主人当前不在对局中。"}
        needle = str(who or "").strip().lower()
        if not needle:
            return {"found": False, "hint": "没给查询目标。"}

        players = list(state.allies) + list(state.enemies)
        match = next(
            (
                p
                for p in players
                if p.champion.lower() == needle or p.name.lower() == needle
            ),
            None,
        )
        if match is None:
            match = next(
                (p for p in players if needle in p.champion.lower() or needle in p.name.lower()),
                None,
            )
        if match is None:
            names = "、".join(p.champion for p in players)
            return {"found": False, "hint": f"这一局里没有叫 {who} 的人。场上英雄：{names}"}

        side = "我方" if match in state.allies else "敌方"
        return {
            "found": True,
            "side": side,
            "player": _player_dict(match),
            "is_me": match is state.me,
        }

    @llm_tool(
        name="lol_recent_events",
        description=(
            "查询这局英雄联盟里最近发生过的事件（击杀、阵亡、大小龙、拆塔、团灭等）。"
            "当主人问'刚才发生了什么''我上个人头怎么拿的'时调用。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def lol_recent_events(self) -> dict:
        if not self._recent:
            return {
                "events": [],
                "hint": "这局还没有记录到事件。"
                if self._in_game
                else "主人当前不在对局中。",
            }
        return {
            "events": [
                {"at": clock, "what": text} for clock, text in list(self._recent)[-10:]
            ]
        }

    # ── 面板：播报开关 ─────────────────────────────────────────

    @ui.context(id="lol_settings")
    async def lol_settings(self):
        """面板的 props.state。

        只给面板"事实"，中文名随 context 下发，面板不查语言包。

        ``event_groups`` 是按用途分好的开关（人头 / 资源 / 进程 / 测试），
        面板照这个顺序渲染就是想要的分区，不用在 TSX 里再维护一份分组表。
        """
        state = self._last_state
        lane_info: dict[str, Any] = {"lane": "", "trusted": False, "opponent": ""}
        if state is not None and state.lanes_trusted:
            opponent = state.lane_opponent()
            lane_info = {
                "lane": state.my_lane,
                "trusted": True,
                "opponent": opponent.champion if opponent is not None else "",
            }

        return {
            "events": [
                {
                    "key": key,
                    # 中文名随 context 下发，面板直接渲染，不查语言包。
                    "label": _EVENT_LABELS.get(key, key),
                    "enabled": bool(self._cfg.announce.get(key, True)),
                }
                for key in _TOGGLE_EVENTS
            ],
            # 面板分组：key 必须在 events 里，否则面板会渲染出空组。
            "event_groups": [
                {
                    "id": "kills",
                    "title": "人头与战况",
                    "keys": ["champion_kill", "my_death", "my_assist", "multikill",
                             "first_blood", "ace"],
                },
                {
                    "id": "objectives",
                    "title": "地图资源",
                    "keys": ["dragon_kill", "baron_kill", "herald_kill",
                             "turret_killed", "inhib_killed"],
                },
                {
                    "id": "session",
                    "title": "对局与提醒",
                    "keys": ["game_start", "game_end", "summary", "milestone"],
                },
            ],
            "dry_run": bool(self._cfg.dry_run),
            "show_prompt_in_chat": bool(self._cfg.show_prompt_in_chat),
            "enabled": bool(self._cfg.enabled),
            "in_game": bool(self._in_game),
            "game_time": state.game_time_label if state is not None else "",
            "queued": self._narrator.queue_depth,
            "backend": self._cfg.base_url,
            # ── 面板展示用的实时状态 ──────────────────────────
            "lane": lane_info,
            # 里程碑进度（没有对局或分路不可信时为空对象）
            "milestone": (
                self._milestones.snapshot(state)
                if state is not None and state.lanes_trusted
                else {}
            ),
            "milestone_enabled": bool(self._cfg.announce.get("milestone", True)),
            "score": state.score_line() if state is not None else "",
            "window_seconds": self._cfg.window_seconds,
            "normal_gap": self._cfg.normal_gap,
        }

    @ui.action(
        id="set_announce",
        label=tr("actions.set_announce.label", default="切换事件播报"),
        refresh_context=True,
    )
    @plugin_entry(
        id="set_announce",
        name=tr("entries.set_announce.name", default="切换事件播报"),
        description=tr(
            "entries.set_announce.description",
            default="打开或关闭某一类对局事件的播报。由插件面板调用。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "event": {
                    "type": "string",
                    "description": "事件 key，例如 champion_kill、dragon_kill、summary。",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "true 开启播报，false 关闭。",
                },
            },
            "required": ["event"],
            "additionalProperties": False,
        },
    )
    async def set_announce(self, event: str = "", enabled: bool = True, **_):
        key = str(event or "").strip()
        if key not in _TOGGLE_EVENTS:
            return Err(SdkError(f"未知的播报事件：{key!r}"))

        want = bool(enabled)
        # 先改内存，保证立即生效；再去落盘，让改动活过重启。
        # 落盘失败也照样返回成功 —— 本次运行确实已经改了，只是重启会回去。
        self._cfg.announce[key] = want
        self._narrator.update_config(self._cfg.to_narrator())
        persisted = await self._persist_config(
            {"neko_lol": {"announce": {key: want}}}
        )
        self._log(
            f"[设置] 播报 {key} → {'开' if want else '关'}"
            f"（持久化{'成功' if persisted else '失败，本次运行内仍生效'}）"
        )
        return Ok(
            {
                "message": f"{key} {'已开启' if want else '已关闭'}",
                "event": key,
                "enabled": want,
                "persisted": persisted,
            }
        )

    @ui.action(
        id="set_show_prompt_in_chat",
        label=tr("actions.set_show_prompt_in_chat.label", default="切换提示文本显示"),
        refresh_context=True,
    )
    @plugin_entry(
        id="set_show_prompt_in_chat",
        name=tr("entries.set_show_prompt_in_chat.name", default="切换提示文本显示"),
        description=tr(
            "entries.set_show_prompt_in_chat.description",
            default="控制播报的提示文本要不要显示在聊天窗口。关掉时猫娘照样带语音回应，只是聊天里看不到那段括号提示。由插件面板调用。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "true 在聊天里显示提示文本，false 隐藏。",
                },
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
    )
    async def set_show_prompt_in_chat(self, enabled: bool = False, **_):
        want = bool(enabled)
        self._cfg.show_prompt_in_chat = want
        persisted = await self._persist_config(
            {"neko_lol": {"show_prompt_in_chat": want}}
        )
        self._log(
            f"[设置] 聊天里显示提示文本 → {'开' if want else '关'}"
            f"（持久化{'成功' if persisted else '失败，本次运行内仍生效'}）"
        )
        return Ok({"enabled": want, "persisted": persisted})

    @ui.action(
        id="set_dry_run",
        label=tr("actions.set_dry_run.label", default="切换干跑"),
        refresh_context=True,
    )
    @plugin_entry(
        id="set_dry_run",
        name=tr("entries.set_dry_run.name", default="切换干跑"),
        description=tr(
            "entries.set_dry_run.description",
            default="干跑模式下完整识别和仲裁，但只写日志、不让猫娘说话。由插件面板调用。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "true 进入干跑（不真的播报），false 恢复正常。",
                },
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
    )
    async def set_dry_run(self, enabled: bool = True, **_):
        want = bool(enabled)
        self._cfg.dry_run = want
        self._narrator.update_config(self._cfg.to_narrator())
        persisted = await self._persist_config({"neko_lol": {"dry_run": want}})
        self._log(
            f"[设置] 干跑 → {'开' if want else '关'}"
            f"（持久化{'成功' if persisted else '失败，本次运行内仍生效'}）"
        )
        return Ok({"enabled": want, "persisted": persisted})

    async def _persist_config(self, patch: dict[str, Any]) -> bool:
        """把配置补丁写回宿主，让改动活过重启。

        两个 SDK 入口都试一遍（不同版本暴露的可能是其中任一个）。
        都失败也不影响本次运行：内存里的配置已经改过了。
        """
        ctx = getattr(self, "ctx", None)
        update_own = getattr(ctx, "update_own_config", None)
        if callable(update_own):
            try:
                await update_own(patch)
                return True
            except Exception as exc:
                self._log(
                    f"[设置] ctx.update_own_config 失败：{type(exc).__name__}: {exc}"
                )

        config = getattr(self, "config", None)
        update = getattr(config, "update", None)
        if callable(update):
            try:
                await update(patch)
                return True
            except Exception as exc:
                self._log(f"[设置] config.update 失败：{type(exc).__name__}: {exc}")

        return False

    # ── 给插件管理器/UI 的入口 ─────────────────────────────────

    @plugin_entry(
        id="lol_status",
        name=tr("entries.lol_status.name", default="陪玩状态"),
        description=tr(
            "entries.lol_status.description",
            default="查看陪玩插件当前状态：是否连上对局、在不在局内、最近播报了什么。",
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def lol_status(self, **_):
        state = self._last_state
        return Ok(
            {
                "enabled": self._cfg.enabled,
                "dry_run": self._cfg.dry_run,
                "in_game": self._in_game,
                "endpoint": self._cfg.base_url,
                "poll_interval_seconds": self._cfg.poll_interval,
                "summary_interval_seconds": self._cfg.summary_interval,
                "queued": self._narrator.queue_depth,
                "game_time": state.game_time_label if state else None,
                "score": state.score_line() if state else None,
                "recent": [text for _clock, text in list(self._recent)[-5:]],
            }
        )

    @ui.action(
        id="lol_summary_now",
        label=tr("entries.lol_summary_now.name", default="立刻说战况"),
        icon="gamepad",
    )
    @plugin_entry(
        id="lol_summary_now",
        name=tr("entries.lol_summary_now.name", default="立刻说战况"),
        description=tr(
            "entries.lol_summary_now.description",
            default="不等定期小结，立刻让猫娘点评一次当前战况。仅在局内有效。",
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def lol_summary_now(self, **_):
        state = self._last_state
        if not self._in_game or state is None:
            return Err(SdkError("当前不在英雄联盟对局中"))
        cue = build_summary_cue(state, champion_names=self._champion_names)
        self._record_recent(state, cue)
        status = self._narrator.submit(cue, force=True)
        return Ok({"status": status, "game_time": state.game_time_label})

    @plugin_entry(
        id="lol_diagnose",
        name=tr("entries.lol_diagnose.name", default="连接自检"),
        description=tr(
            "entries.lol_diagnose.description",
            default="直接探测 Live Client Data API，报告能不能连上、走的 https 还是 http。"
                    "装完插件第一次用这个确认国服环境是否正常。",
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def lol_diagnose(self, **_):
        result: dict[str, Any] = {
            "endpoint": self._cfg.base_url,
            "http_fallback": self._cfg.allow_http_fallback,
        }
        try:
            data = await self._client.all_game_data()
        except NotInGame as exc:
            result.update(
                reachable=False,
                detail=str(exc)[:200],
                hint="端口 2999 没有响应。如果此刻没在打英雄联盟，这是正常的；"
                     "如果正在对局中仍然如此，检查是否有代理/加速器拦截了本地回环地址。",
            )
            return Ok(result)

        state = MatchState.from_all_game_data(
            data, trust_lane_order=self._cfg.infer_lane_positions
        )
        result.update(
            reachable=True,
            protocol=self._client.scheme,
            game_time=state.game_time_label,
            map=state.map_name,
            mode=state.game_mode,
            me=state.me.one_line() if state.me else None,
            score=state.score_line(),
        )
        return Ok(result)


def _player_dict(player) -> dict | None:
    if player is None:
        return None
    return {
        "name": player.name,
        "champion": player.champion,
        "team": player.team_label,
        "kda": player.kda,
        "cs": player.cs,
        "level": player.level,
        "estimated_gold": int(round(player.estimated_gold)),
        "is_dead": player.is_dead,
        "respawn_in_seconds": round(player.respawn_timer, 1) if player.is_dead else 0,
        "items": list(player.items),
    }


def _event_id(event: Any) -> int:
    """事件 ID。非 dict 或字段畸形一律当 0，绝不让它抛异常。"""
    return _as_int(event.get("EventID")) if isinstance(event, dict) else 0


def _as_int(value: Any, default: int = 0) -> int:
    """宽松的整数转换。接口偶尔给字符串，畸形数据不能让轮询循环崩掉。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
