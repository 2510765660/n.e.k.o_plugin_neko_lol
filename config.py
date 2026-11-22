"""插件配置：把 plugin.toml 的 ``[neko_lol]`` 段变成有类型、有默认值的对象。

单独成模块的理由：TOML 是用户手改的，类型和缺项都不可信。所有强制转换
和默认值收敛在这里，主流程拿到的永远是干净的值，测试也不用碰 SDK。

每个字段都有默认值，所以 **配置里缺项、写错类型、整段缺失都不会让插件
起不来** —— 最坏情况是回到默认行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .live_client import DEFAULT_BASE_URL
from .narrator import NarratorConfig

# 默认全开。关掉某一项就完全不会为这类事件说话。
# "summary" 也在内 —— 面板上要能关掉定期小结。
DEFAULT_ANNOUNCE: dict[str, bool] = {
    "game_start": True,
    "game_end": True,
    "first_blood": True,
    "champion_kill": True,
    "my_death": True,
    "my_assist": True,
    "milestone": True,
    "multikill": True,
    "ace": True,
    "dragon_kill": True,
    "baron_kill": True,
    "herald_kill": True,
    "turret_killed": True,
    "inhib_killed": True,
    "summary": True,
}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _bool_map(value: Any, defaults: dict[str, bool]) -> dict[str, bool]:
    merged = dict(defaults)
    if isinstance(value, dict):
        for key, raw in value.items():
            if isinstance(key, str):
                merged[key] = _bool(raw, merged.get(key, True))
    return merged


# [neko_lol] 段里被认可的标量键。用于把"键名打错"和"键写错段"
# 变成一条明确的日志，而不是静默地悄悄用默认值。
SCALAR_KEYS: frozenset[str] = frozenset(
    {
        "enabled",
        "dry_run",
        "base_url",
        "allow_http_fallback",
        "http_timeout_seconds",
        "poll_interval_seconds",
        "idle_poll_interval_seconds",
        "summary_interval_seconds",
        "summary_first_at_seconds",
        "aram_summary_first_at_seconds",
        "aram_summary_interval_seconds",
        "aram_kills_ambient",
        "aram_death_only_notable",
        "aram_burst_kills",
        "aram_burst_cooldown_seconds",
        "summary_burst_after_seconds",
        "summary_burst_kills",
        "summary_burst_window_seconds",
        "summary_burst_cooldown_seconds",
        "join_replay_max_game_time_seconds",
        "game_over_grace_seconds",
        "window_seconds",
        "merge_max_items",
        "stats_cooldown_seconds",
        "show_prompt_in_chat",
        "normal_gap_seconds",
        "only_my_events",
        "infer_lane_positions",
        "others_multikill_min_streak",
        "others_multikill_settle_seconds",
        "user_chat_quiet_window_seconds",
        "post_game_summary",
    }
)

# [neko_lol] 段下的子表。
SUB_TABLE_KEYS: frozenset[str] = frozenset({"announce"})


@dataclass
class PluginConfig:
    """``[neko_lol]`` 段的类型化视图。"""

    enabled: bool = True
    dry_run: bool = False

    base_url: str = DEFAULT_BASE_URL
    allow_http_fallback: bool = True
    # 超时给到 5 秒而不是贴着探测延迟。理由：某些安全软件/防火墙过滤驱动
    # 会给"被拒绝的本地连接"叠加固定延迟（实测这台机器上是 2.05 秒/次），
    # 超时设 3 秒就会在慢一点的机器上退化成"超时"而不是干净的"拒绝"。
    # 真正在对局中时响应是毫秒级，5 秒不会误伤。
    http_timeout: float = 5.0

    # 在局内时的轮询间隔。
    poll_interval: float = 2.0
    # 不在局内时的轮询间隔。没在打游戏时每次探测都要走完 https→http
    # 两次失败（在受安全软件影响的机器上约 4 秒），所以空闲时降频。
    # 进游戏有加载画面兜底，晚发现几秒用户完全无感。
    idle_poll_interval: float = 5.0
    # 定期小结：对局第 summary_first_at 秒首播，之后每隔 summary_interval 秒一次。
    # 默认 15 分钟首播、每 5 分钟一次。
    summary_interval: float = 300.0
    summary_first_at: float = 900.0
    # 嚎哭深渊系（大乱斗 / 海克斯大乱斗）节奏更快、局更短：
    # 第 8 分钟首播，之后每 3 分钟一次。
    aram_summary_first_at: float = 480.0
    aram_summary_interval: float = 180.0

    # ── 大乱斗降噪（2026-11-17 用户指定）──────────────────────────
    # 大乱斗人头密度是峡谷的 3~4 倍、时长只有一半，同样的规则会刷屏。
    # 人头/阵亡改走环境档：一波团 5 个人头合并成一句。
    aram_kills_ambient: bool = True
    # 死亡只播有信息量的：连杀 ≥3 被断，或短时间连续阵亡 ≥2 次。
    # 大乱斗里"死一次"本身完全不值得报。
    aram_death_only_notable: bool = True
    # 爆发小结在大乱斗收紧：5 人/30 秒在那里是常态，会每分钟触发一次。
    aram_burst_kills: int = 8
    aram_burst_cooldown: float = 120.0
    # 爆发小结：短时间大量人头时额外播一次（2026-11-17 用户指定）。
    # 只在 summary_burst_after 之后启用；窗口内人头数达标就触发；两次之间
    # 至少隔 summary_burst_cooldown 秒。人头不分敌我 —— 团战两边都在死人。
    # 大乱斗用另一套门槛（aram_burst_*），因为那边人头密度高得多。
    # 同一拍里若出现团灭，本条会被跳过（团灭已经说了这一波）。
    summary_burst_after: float = 900.0
    summary_burst_kills: int = 5
    summary_burst_window: float = 30.0
    summary_burst_cooldown: float = 60.0

    # ── 播报窗口（唯一的节流参数）────────────────────────────────
    # 播报分三档（见 cues.py 的 TIER_*）：
    #   关键档永远立刻说；常规档立刻说但两条之间至少隔 normal_gap；
    #   环境档进窗口合并。窗口默认 10 秒，因为关键内容
    #   已经不走窗口了，窗口只剩推塔/兵营这类环境信息。
    window_seconds: float = 10.0
    # 合并后最多列几条正文，超出折叠成"等共 N 条"。
    merge_max_items: int = 6
    # 战绩行的冷却（秒）：距离上次带战绩的播报不足这个数，就不再挂
    # "主人现在 X—Y—Z，N 级"。资源类事件一场能出十几个，逐条带会像读报表。
    # 0 = 关闭冷却（每次都带）。
    stats_cooldown: float = 60.0
    # 播报的提示文本要不要在聊天窗口里显示。
    # 关掉（默认）时 visibility=[]：模型照样读到并带语音回应，只是聊天里
    # 看不到那一大段括号提示。开着适合调试——能直接看到模型收到了什么。
    show_prompt_in_chat: bool = False
    # 常规档两条播报之间的最小间隔（秒）。间隔不够的会进窗口合并，不丢。
    normal_gap: float = 3.0
    # 用户正在聊天时不主动插话（秒）。0 = 不理会，照常说。
    user_chat_quiet_window: float = 0.0

    # 插件启动/重载时如果对局已经打了这么久，就只从当前进度开始播报，
    # 不回放已经发生过的事件。
    join_replay_max_game_time: float = 90.0

    # 判定"对局结束"需要连续拿不到数据多久（秒）。
    # 单纯按次数判太脆弱：客户端抖一下、安全软件拦一下，几次探测失败就会
    # 误判成对局结束，进而放出假的赛后总结。再加一条时间下限兜住。
    game_over_grace: float = 20.0

    # 只有主人的事才值得说。
    # 打开后：队友拿人头、队友被打死、对面互杀都不播报；一血只在
    # "主人拿到"或"主人被拿一血"时播报。默认开。
    only_my_events: bool = True
    # 按 allPlayers 的数组顺序推断分路（上单→打野→中单→ADC→辅助）。
    # 只有**排位和灵活排位**的顺序才是固定的 —— 普通匹配没有位置选择流程。
    # 接口不给队列类型，所以这个开关由你自己按常玩的模式设。
    infer_lane_positions: bool = True

    # 别人（队友或对面）的连杀播报：达到几杀才说。
    # 0 = 完全不说；3 = 三杀及以上（默认）。
    others_multikill_min_streak: int = 3
    # 别人的连杀攒多久再播（秒）。接口对一次五杀会连发 2/3/4/5 四条，
    # 攒这一下只是为了取最高那条，避免"三杀→四杀→五杀"连播三句。
    others_multikill_settle_seconds: float = 4.0

    post_game_summary: bool = True

    announce: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_ANNOUNCE))

    @classmethod
    def unknown_keys(cls, section: Any) -> set[str]:
        """挑出 [neko_lol] 段里我不认识的标量键。

        最常见的两种手滑：键名拼错，以及写了标量键但位置落在
        ``[neko_lol.announce]`` 之类的子表之后（TOML 会把它归给子表）。
        两种情况都会静默退回默认值，所以要在启动时喊一声。
        """
        if not isinstance(section, dict):
            return set()
        return {
            key
            for key, value in section.items()
            if key not in SCALAR_KEYS
            and key not in SUB_TABLE_KEYS
            and not isinstance(value, dict)
        }

    @classmethod
    def from_raw(cls, section: Any) -> PluginConfig:
        """从 ``[neko_lol]`` 段构造。传入非 dict 一律返回全默认配置。"""
        raw = section if isinstance(section, dict) else {}
        return cls(
            enabled=_bool(raw.get("enabled"), True),
            dry_run=_bool(raw.get("dry_run"), False),
            base_url=_str(raw.get("base_url"), DEFAULT_BASE_URL),
            allow_http_fallback=_bool(raw.get("allow_http_fallback"), True),
            http_timeout=max(0.5, _float(raw.get("http_timeout_seconds"), 5.0)),
            poll_interval=max(2.0, _float(raw.get("poll_interval_seconds"), 2.0)),
            idle_poll_interval=max(
                2.0, _float(raw.get("idle_poll_interval_seconds"), 5.0)
            ),
            summary_interval=max(0.0, _float(raw.get("summary_interval_seconds"), 300.0)),
            summary_first_at=max(
                0.0, _float(raw.get("summary_first_at_seconds"), 900.0)
            ),
            aram_summary_first_at=max(
                0.0, _float(raw.get("aram_summary_first_at_seconds"), 480.0)
            ),
            aram_summary_interval=max(
                0.0, _float(raw.get("aram_summary_interval_seconds"), 180.0)
            ),
            aram_kills_ambient=_bool(raw.get("aram_kills_ambient"), True),
            aram_death_only_notable=_bool(raw.get("aram_death_only_notable"), True),
            aram_burst_kills=max(1, _int(raw.get("aram_burst_kills"), 8)),
            aram_burst_cooldown=max(
                0.0, _float(raw.get("aram_burst_cooldown_seconds"), 120.0)
            ),
            summary_burst_after=max(
                0.0, _float(raw.get("summary_burst_after_seconds"), 900.0)
            ),
            summary_burst_kills=max(
                1, _int(raw.get("summary_burst_kills"), 5)
            ),
            summary_burst_window=max(
                1.0, _float(raw.get("summary_burst_window_seconds"), 30.0)
            ),
            summary_burst_cooldown=max(
                0.0, _float(raw.get("summary_burst_cooldown_seconds"), 60.0)
            ),
            window_seconds=max(0.0, _float(raw.get("window_seconds"), 10.0)),
            merge_max_items=max(1, _int(raw.get("merge_max_items"), 6)),
            stats_cooldown=max(0.0, _float(raw.get("stats_cooldown_seconds"), 60.0)),
            show_prompt_in_chat=_bool(raw.get("show_prompt_in_chat"), False),
            normal_gap=max(0.0, _float(raw.get("normal_gap_seconds"), 3.0)),
            user_chat_quiet_window=max(
                0.0, _float(raw.get("user_chat_quiet_window_seconds"), 0.0)
            ),
            join_replay_max_game_time=max(
                0.0, _float(raw.get("join_replay_max_game_time_seconds"), 90.0)
            ),
            game_over_grace=max(0.0, _float(raw.get("game_over_grace_seconds"), 20.0)),
            only_my_events=_bool(raw.get("only_my_events"), True),
            infer_lane_positions=_bool(raw.get("infer_lane_positions"), True),
            others_multikill_min_streak=_int(
                raw.get("others_multikill_min_streak"), 3
            ),
            others_multikill_settle_seconds=max(
                0.0, _float(raw.get("others_multikill_settle_seconds"), 4.0)
            ),
            post_game_summary=_bool(raw.get("post_game_summary"), True),
            announce=_bool_map(raw.get("announce"), DEFAULT_ANNOUNCE),
        )

    def invariant_warnings(self) -> list[str]:
        """配置里明显不合理的值。单一窗口模型下只剩这几条。"""
        out: list[str] = []
        if self.window_seconds <= 0:
            out.append(
                "window_seconds <= 0：窗口为 0 等于每条都立刻播报，"
                "一波团战会连出十几条。"
            )
        if self.window_seconds > 120:
            out.append(
                f"window_seconds({self.window_seconds:.0f}) 过大："
                f"战况会攒两分钟才说，听起来会很滞后。"
            )
        return out

    def to_narrator(self) -> NarratorConfig:
        return NarratorConfig(
            enabled=self.enabled,
            dry_run=self.dry_run,
            window_seconds=self.window_seconds,
            merge_max_items=self.merge_max_items,
            normal_gap=self.normal_gap,
            user_chat_quiet_window=self.user_chat_quiet_window,
            announce=dict(self.announce),
        )
