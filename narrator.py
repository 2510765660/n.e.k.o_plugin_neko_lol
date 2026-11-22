"""播报仲裁：决定"这条到底要不要说、什么时候说"。

## 模型（v3：单一窗口合并）

只有一条规则：

1. 每一条想说的话都**直接提交**，不排队、不看同类间隔。
2. 每条 cue 带一个**档位**，决定它怎么投递（见 ``NarratorConfig``）。
3. 窗口内新来的话**合并到一条**里，窗口结束（倒计时归零）时一起播报。

于是"触发"永远立即生效（内容不会因为间隔被丢），而"发声"是节流的 ——
一波团战里连出五个人头，除了**刚好隔够 normal_gap 的那一条会立刻发**，
其余都会并成一句"刚才这一波：…"。

为什么推翻之前那套：旧版有即时/常规两条通道，各自一套间隔，还叠了
队列保鲜期、静默窗口、逐类间隔。结果是：

* 参数之间互相饿死（保鲜期 < 全局间隔 → 排队等到过期，永远不播）；
* 行为难以预测（"为什么我的人头没播"要翻五个参数才能解释）；
* 同一个事件可能被两条通道各说一遍。

要调的有两个数：``normal_gap``（常规档之间至少隔多久）和
``window_seconds``（环境档攒多久）。想更安静就调大它们。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .cues import ASSIST_BACKGROUND_HINT, Cue, is_announced

# push 回调：(提示文本, 元数据) → push_message 的返回信封
PushFn = Callable[[str, dict[str, Any]], dict[str, Any]]
LogFn = Callable[[str], None]

# 窗口合并（≥2 条）产物的 key。刻意**不**复用任何一个真实事件的 key ——
# 早先借用 "multikill" 会让"敌方拆掉两座塔"的日志写成 multikill，排查时
# 完全误导。它也不在 announce 开关表里，所以不会被单一事件开关误伤
# （组成它的每条在 submit 时已经各自过了开关）。
MERGED_KEY = "merged"

# 重复句子的计数单位：结构物用「座」，其余一律用「次」。
# 这个集合决定"座"，不决定"要不要计数" —— 人头重复同样会标「（2 次）」。
_COUNTABLE_KEYS: frozenset[str] = frozenset({"turret_killed", "inhib_killed"})


@dataclass
class NarratorConfig:
    enabled: bool = True
    dry_run: bool = False

    # ── 三档投递 ────────────────────────────────────────────────
    # 关键档（tier 0）：永远立刻说，任何间隔都不等。
    # 常规档（tier 1）：立刻说，但和上一条至少隔 normal_gap 秒；
    #                  间隔不够时和低档一起进窗口，不丢。
    # 环境档（tier 2）：进窗口合并，安静 window_seconds 秒后一起说。
    normal_gap: float = 3.0
    window_seconds: float = 10.0

    # 一次合并最多列多少条正文，超出折叠成"等共 N 条"。
    # 防止一波团战攒出十几句话塞给模型。
    merge_max_items: int = 6

    # 用户正在聊天时不主动插话（秒）。窗口结束时如果还在这个区间里，
    # 就顺延到窗口结束再说。
    user_chat_quiet_window: float = 0.0

    # 事件开关
    announce: dict[str, bool] = field(default_factory=dict)


@dataclass
class _Pending:
    """窗口内攒下的一条内容。"""

    cue: Cue
    at: float


class Narrator:
    """把 Cue 合并成实际的 ``push_message`` 调用。"""

    def __init__(
        self,
        push: PushFn,
        *,
        config: NarratorConfig,
        log: LogFn | None = None,
    ) -> None:
        self._push = push
        self._log = log or (lambda _msg: None)
        self.config = config
        self._quiet_until: float = 0.0
        # 窗口状态：第一进来时开窗，窗口结束就发。
        self._pending: list[_Pending] = []
        self._window_started_at: float = 0.0
        # 上一次真正播报的时间（仅用于统计/日志）。
        self._last_emit_at: float = 0.0

    # ── 配置热更新 ──────────────────────────────────────────────

    def update_config(self, config: NarratorConfig) -> None:
        self.config = config

    def note_user_chat(self, now: float | None = None) -> None:
        """用户刚说了话 —— 推迟主动播报。"""
        moment = time.monotonic() if now is None else now
        self._quiet_until = moment + max(0.0, self.config.user_chat_quiet_window)
        if self.config.user_chat_quiet_window > 0:
            self._log(f"[仲裁] 用户刚说话，{self.config.user_chat_quiet_window:.0f} 秒内不主动播报")

    def in_quiet_window(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return moment < self._quiet_until

    # ── 提交 ────────────────────────────────────────────────────

    def submit(
        self,
        cue: Cue,
        now: float | None = None,
        *,
        force: bool = False,
    ) -> str:
        """提交一条 cue。永远接受，返回处理结果供日志和测试断言。

        投递由 cue 的档位决定：

        * 关键档（tier 0）或 ``force=True``：永远立刻，不看间隔、不碰窗口。
        * 常规档（tier 1）：离上一条够 ``normal_gap`` 就立刻，否则进窗口。
        * 环境档（tier 2）：一律进窗口，等倒计时结束合并播报。
        * ``mergeable=False``：不进窗口，直接发。定期小结用这条 —— 它是
          "此刻状态"而非"刚发生的事"，合并会让它语义错乱、信息丢失。

        **静默窗口**（``user_chat_quiet_window``）对所有档位一视同仁：
        用户在聊天期间，连关键档也不插话，一律攒进窗口等窗口结束。
        早先只有合并路径看它，直发路径照说不误 —— 用户设了静默却仍被打断。
        """
        moment = time.monotonic() if now is None else now
        cfg = self.config
        if not cfg.enabled:
            return "disabled"
        if not is_announced(cue.key, cfg.announce):
            return "muted"

        tier = getattr(cue, "tier", 1)

        # 静默窗口优先于一切档位：用户正在聊天时不主动插话。
        # 进窗口攒着而不是丢弃 —— 窗口结束后（此时静默期已过）会一起说。
        quiet = moment < self._quiet_until
        if quiet and not force:
            return self._buffer(cue, moment, reason="静默期")

        if force or tier <= 0:
            # 关键档：**永远立刻**，不看任何间隔，也不碰窗口 ——
            # 窗口有自己的倒计时，关键事件插进来不该把它提前吐出来。
            return self._emit(cue, moment, merged=0)

        if not getattr(cue, "mergeable", True):
            # 不允许合并的 cue（定期小结）直接发。它说的是"此刻状态"，
            # 攒进"刚才发生了这些事"列表会让模型把它当成一条事件来概括，
            # 而且合并只保留 body，完整战况就丢了。
            return self._emit(cue, moment, merged=0)

        if tier == 1 and moment - self._last_emit_at >= max(0.0, cfg.normal_gap):
            # 常规档且已经隔够了：立刻说。
            return self._emit(cue, moment, merged=0)

        # 到这儿说明是环境档，或者常规档但离上一条太近 —— 进窗口攒着，
        # 攒够时间一起说。**不丢**。
        return self._buffer(cue, moment, reason="间隔")

    def _buffer(self, cue: Cue, now: float, *, reason: str) -> str:
        """把 cue 放进窗口。第一条开窗，其余合并。"""
        if not self._pending:
            self._window_started_at = now
            self._pending.append(_Pending(cue=cue, at=now))
            self._log(
                f"[窗口] 开窗（{cue.key}，{reason}），"
                f"{self.config.window_seconds:.0f} 秒内合并"
            )
            return "buffered"

        self._pending.append(_Pending(cue=cue, at=now))
        self._log(
            f"[窗口] 合并（{cue.key}，{reason}），"
            f"窗口内已有 {len(self._pending)} 条"
        )
        return "merged"

    # ── 结算 ────────────────────────────────────────────────────

    def flush(self, now: float | None = None) -> int:
        """窗口倒计时到了就结算。返回真正发出的条数。

        通常只有 1 条（合并后的），但静默期可能把不参与合并的小结也攒进来，
        所以这里按"可合并 / 不可合并"分开结算，最多会发 2 条。
        """
        moment = time.monotonic() if now is None else now
        if not self._pending or not self.config.enabled:
            return 0
        if moment - self._window_started_at < max(0.0, self.config.window_seconds):
            return 0
        # 用户正在聊天就顺延，等静默期过了再说，别抢话。
        if moment < self._quiet_until:
            return 0

        pending = self._pending
        self._pending = []
        return self._drain(pending, moment)

    def force_flush(self, now: float | None = None) -> int:
        """不等窗口，立刻结算。对局结束时用。"""
        moment = time.monotonic() if now is None else now
        if not self._pending:
            return 0
        pending = self._pending
        self._pending = []
        return self._drain(pending, moment)

    def take_pending_bodies(self) -> list[str]:
        """把窗口里攒着的正文取出来并清空窗口。

        对局结束时用：那些推塔/兵营再等窗口就没机会说了，所以不打成独立
        播报，而是并进"对局结束"那条一起报。
        """
        bodies = [
            (p.cue.body or p.cue.text).strip().rstrip("。，, ！？!?、;；")
            for p in self._pending
        ]
        self._pending = []
        return [b for b in bodies if b]

    def _drain(self, pending: list["_Pending"], moment: float) -> int:
        """把攒下的一组 cue 发出去：可合并的拼一条，不可合并的各自单独发。"""
        mergeable = [p for p in pending if getattr(p.cue, "mergeable", True)]
        standalone = [p.cue for p in pending if not getattr(p.cue, "mergeable", True)]
        sent = 0

        merged = self._build_merged(mergeable)
        if merged is not None:
            result = self._emit(merged, moment, merged=len(mergeable))
            sent += 1 if result in ("sent", "dry_run") else 0
        for cue in standalone:
            result = self._emit(cue, moment, merged=0)
            sent += 1 if result in ("sent", "dry_run") else 0
        return sent

    @property
    def queue_depth(self) -> int:
        """窗口里攒了多少条（面板/诊断用）。"""
        return len(self._pending)

    def window_remaining(self, now: float | None = None) -> float:
        """窗口还剩多少秒。没开窗返回 0。

        必须接受注入时钟：插件和测试台都用受控时钟（``neko_lol.time``），
        直接读 ``time.monotonic()`` 会算出错误的值（实测返回 0）。
        """
        if not self._pending:
            return 0.0
        moment = time.monotonic() if now is None else now
        elapsed = moment - self._window_started_at
        return max(0.0, self.config.window_seconds - elapsed)

    @property
    def is_open(self) -> bool:
        return bool(self._pending)

    # ── 内部 ────────────────────────────────────────────────────

    def _build_merged(self, pending: list[_Pending]) -> Cue | None:
        """把窗口里的多条内容合成一条 Cue。"""
        if not pending:
            return None

        items = [p.cue for p in pending]
        # 去重：同一句话在窗口内说一次就够，**但要保留重复次数**。
        # 两座门牙塔的 body 完全相同，早先丢了一条，"两座都掉了"就播成"一座"。
        # 现在统一标次数，单位按事件类型选：
        #   结构物 → 「（2 座）」；其它（人头等）→ 「（2 次）」。
        # 早期给所有重复都套"座"，结果人头被标成「（3 座）」—— 实测踩过。
        grouped: dict[str, int] = {}
        order: list[str] = []
        priority_of: dict[str, int] = {}
        countable: dict[str, bool] = {}
        for cue in items:
            body = (cue.body or cue.text).strip().rstrip("。，, ！？!?、;；")
            if not body:
                continue
            priority_of.setdefault(body, cue.priority or 0)
            countable.setdefault(body, cue.key in _COUNTABLE_KEYS)
            grouped[body] = grouped.get(body, 0) + 1
            if grouped[body] == 1:
                order.append(body)

        if not order:
            return None

        # 优先级只用来决定**谁被截断掉**，不决定显示顺序。
        # 早先按优先级排序输出，于是"二塔 → 高地塔 → 兵营"被念成
        # "兵营 → 二塔 → 高地塔"—— 建筑明明是从外往里塌的，读起来是倒放。
        #
        # 重复次数也要标出来：两座门牙塔是「（2 座）」，同一个英雄被连杀
        # 两次是「（2 次）」。早先后者直接吞掉一条，信息就没了。
        def _annotate(body: str) -> str:
            count = grouped[body]
            if count <= 1:
                return body
            unit = "座" if countable[body] else "次"
            return f"{body}（{count} {unit}）"

        limit = max(1, self.config.merge_max_items)
        by_priority = sorted(
            range(len(order)),
            key=lambda index: (-priority_of[order[index]], index),
        )
        kept = {order[index] for index in by_priority[:limit]}
        listed = [_annotate(body) for body in order if body in kept]
        more = len(order) - len(listed)
        joined = "；".join(listed)
        if more > 0:
            joined += f"；等共 {len(order)} 条"

        # 合并后的事件 key：单条沿用原 key；多条给一个独立的 key。
        # 早先多条也硬贴成 "multikill"，于是"敌方拆掉两座塔"的日志里
        # 写着 multikill —— 排查时完全误导（2026-11-15 日志实证）。
        # 独立 key 还有个好处：它不属于任何单一事件开关，不会被误伤。
        top = max(items, key=lambda c: c.priority or 0)
        top_body = (top.body or top.text).strip().rstrip("。，, ！？!?、;；")

        # 合并后是否算"多条"，看**去重后的句子数**而不是原始条数：
        # 同一句话来两次（两座门牙塔）本质仍是一句话，不该套"刚才发生了
        # 这些事"的壳，也不该记成两条事件。
        merged_count = len(order)

        if merged_count == 1:
            # 只有一条时不要套"刚才发生了这些事"的壳 —— 对着单件事做概括
            # 听起来很别扭，直接用原提示语。
            text = top.text
            # 但同一句话重复出现时（两座门牙塔）joined 带着「（2 座）」，
            # 那个信息不能丢 —— 把标注插进原提示语里，而不是整段替换。
            if grouped.get(top_body, 1) > 1 and top.body:
                text = text.replace(
                    top.body.strip().rstrip("。，, ！？!?、;；"),
                    joined,
                    1,
                )
        else:
            text = (
                f"（主人正在玩英雄联盟，刚才这一段时间里发生了这些事：\n"
                f"{joined}。\n"
                f"用你自己的口吻对主人说一两句话概括一下，"
                f"挑你觉得最值得说的讲，别逐条复述上面的列表。"
                f"英雄名别照搬官方全称，用玩家平时的叫法。"
                # 合并里的条目如果是"主人击杀"，body 会带「（助攻：…）」，
                # 但那条"助攻只作背景"的约束原本只加在单条提示语里，
                # 合并时被丢掉了 —— 结果合并播报反而更容易念出助攻名单。
                f"{ASSIST_BACKGROUND_HINT if any(getattr(c, 'has_assists', False) for c in items) else ''}"
                f"只依据上面给出的事实，不要编造。）"
            )
        return Cue(
            key=top.key if merged_count == 1 else MERGED_KEY,
            text=text,
            priority=top.priority,
            meta={
                "kind": "merged" if merged_count > 1 else (top.meta or {}).get("kind", ""),
                "merged": merged_count,
                "lead": (top.body or top.text)[:80],
            },
            # body 统一以「。」结尾（和事件 cue 一致）。早先直接塞 joined，
            # 日志里就出现「队友拿下了炼金龙」这种没句号的半句。
            body=f"{joined}。" if joined and not joined.endswith(("。", "）")) else joined,
        )

    def _emit(self, cue: Cue, now: float, *, merged: int = 0) -> str:
        cfg = self.config
        if cfg.dry_run:
            self._last_emit_at = now
            self._log(f"[干跑] 本该播报（{cue.key}，{merged or 1} 条合并）：{cue.text[:120]}")
            return "dry_run"

        try:
            result = self._push(cue.text, {"context_type": "neko_lol_event", **cue.meta})
        except Exception as exc:  # 插件进程不能因为播报失败而崩
            self._log(f"[播报] push_message 抛异常（{cue.key}）：{type(exc).__name__}: {exc}")
            return "error"

        if not isinstance(result, dict) or not result.get("submitted", True):
            reason = (result or {}).get("reason") if isinstance(result, dict) else "bad_result"
            self._log(f"[播报] 宿主拒收（{cue.key}）：{reason}")
            return "rejected"

        self._last_emit_at = now
        self._log(
            f"[播报] 已发送（{cue.key}，合并 {merged or 1} 条）：{cue.body[:80]}"
        )
        return "sent"

    def is_quiet(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return moment < self._quiet_until
