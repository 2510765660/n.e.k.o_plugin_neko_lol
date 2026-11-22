"""对局状态机的集成测试：用假客户端驱动完整的一局，不需要 N.E.K.O，也不需要真的在打游戏。

这里覆盖的是 ``__init__.py`` 里那段最容易出错、又最难手工验证的逻辑：
事件游标、开局/中途加入的判定、人头合并、定期小结、以及"什么时候判定
对局结束"。这些逻辑如果错了，表现是"猫娘重复播报"或"整局没反应"，
真机上要复现得打一整局，所以必须在离线把它钉死。
"""

from __future__ import annotations

import unittest

import fixtures
import neko_lol
from neko_lol.live_client import NotInGame


class FakeClock:
    """受控时钟。插件内部只用 ``time.monotonic()``，替换掉即可。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SilentLogger:
    def info(self, *_a, **_k):
        pass

    warning = info
    error = info
    debug = info


class FakeClient:
    """假的 Live Client Data API。``in_game=False`` 时抛 NotInGame。"""

    def __init__(self) -> None:
        self.in_game = True
        self.events: list[dict] = []
        self.data: dict = fixtures.make_all_game_data()
        self._prefer_http = False

    async def snapshot(self) -> tuple[list[dict], dict]:
        """和真实客户端同形：一次取回事件 + 快照。"""
        if not self.in_game:
            raise NotInGame("connection refused")
        return list(self.events), self.data

    async def event_data(self) -> list[dict]:
        if not self.in_game:
            raise NotInGame("connection refused")
        return list(self.events)

    async def all_game_data(self) -> dict:
        if not self.in_game:
            raise NotInGame("connection refused")
        return self.data


class Harness:
    """把插件接上假时钟、假客户端、假播报通道。"""

    def __init__(self, clock: FakeClock, raw_config: dict | None = None) -> None:
        self.clock = clock
        self.pushed: list[dict] = []
        self.plugin = neko_lol.NekoLolPlugin(ctx=None)
        self.plugin.logger = SilentLogger()
        self.plugin.push_message = self._push
        self.client = FakeClient()
        # 测试里默认用短窗口（10 秒），否则每个用例都得推进 30 秒。
        # 需要验证真实 30 秒行为的用例会显式覆盖这个值。
        cfg = dict(raw_config or {})
        cfg.setdefault("window_seconds", 10.0)
        self.raw_config = cfg

    def _push(self, **kwargs) -> dict:
        self.pushed.append(kwargs)
        return {"submitted": True}

    async def start(self) -> None:
        # 真实宿主给的是 {"neko_lol": {...}} 这种带段名的整份配置，
        # 夹层照抄，免得测试和线上读的不是同一个形状。
        await self.plugin._load_config({"neko_lol": self.raw_config})
        self.plugin._client = self.client

    async def poll(self, times: int = 1, dt: float = 2.0) -> None:
        for _ in range(times):
            await self.plugin.poll_match()
            self.clock.advance(dt)

    def _window(self) -> float:
        return float(self.raw_config.get("window_seconds", 10.0))

    async def settle(self, extra: float = 2.0) -> None:
        """推进到窗口结束，让合并后的播报真正发出来。"""
        await self.poll(times=int((self._window() + extra) / 2) + 1, dt=2.0)

    @property
    def texts(self) -> str:
        return " | ".join(
            p.get("parts", [{}])[0].get("text", "") for p in self.pushed
        )

    def count(self, needle: str) -> int:
        return self.texts.count(needle)

    def pushes_containing(self, needle: str) -> list[str]:
        """哪些播报里出现了这段文字（用来验证"合并成一条"而不是"发了两条"）。"""
        return [
            p.get("parts", [{}])[0].get("text", "")
            for p in self.pushed
            if needle in p.get("parts", [{}])[0].get("text", "")
        ]


def _start_fresh_game(h: Harness, *, game_time: float = 5.0) -> None:
    h.client.in_game = True
    h.client.data = fixtures.make_all_game_data(game_time=game_time)
    h.client.events = [fixtures.simple_event(0, "GameStart")]


class TestSessionLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self._real_time = neko_lol.time
        neko_lol.time = self.clock  # type: ignore[assignment]

    def tearDown(self) -> None:
        neko_lol.time = self._real_time  # type: ignore[assignment]

    async def _harness(self, raw_config: dict | None = None) -> Harness:
        h = Harness(self.clock, raw_config)
        await h.start()
        return h

    # ── 开局 ────────────────────────────────────────────────────

    async def test_detects_and_announces_game_start(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        self.assertTrue(h.plugin._in_game)
        self.assertIn("刚开了一局", h.texts)
        self.assertIn("Yasuo", h.texts)

    async def test_midgame_join_does_not_replay_history(self):
        """插件中途启动时，绝不能把已经打完的半局重放给猫娘。"""
        h = await self._harness()
        h.client.data = fixtures.make_all_game_data(game_time=900.0)
        h.client.events = [
            fixtures.simple_event(0, "GameStart"),
            fixtures.kill_event(1, "我", "敌人A"),
            fixtures.simple_event(2, "BaronKill", KillerName="我"),
        ]
        await h.poll(times=3)
        self.assertTrue(h.plugin._in_game)
        self.assertEqual(h.pushed, [], f"不该回放历史，却播报了：{h.texts}")

    async def test_early_join_replays_from_first_event(self):
        """开局就接上时，一血这种开场事件不能被吞掉。"""
        h = await self._harness()
        h.client.data = fixtures.make_all_game_data(game_time=3.0)
        h.client.events = [fixtures.simple_event(0, "GameStart")]
        await h.poll(times=1)
        h.client.events.append(
            fixtures.simple_event(1, "FirstBlood", Recipient="我", at=20.0)
        )
        await h.poll(times=12, dt=2.0)
        self.assertIn("一血", h.texts)

    # ── 人头 ────────────────────────────────────────────────────

    async def test_kill_announced_once_after_coalesce_window(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=20, dt=2.0)  # 覆盖 40 秒虚拟时间

        self.assertEqual(h.count("击杀了对面的 Zed"), 1, h.texts)

    async def test_kill_and_death_use_their_table_priorities(self):
        """优先级一律查 PRIORITIES，别再硬编码。

        早先 `_handle_champion_kill` 把 priority 写死成 60，于是阵亡
        （表里 50）和人头（60）优先级一样 —— 混合窗口里"最值得说"的
        挑错了，日志和面板也跟着失真。
        """
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        # 拦下真正交给 narrator 的 cue，看它们的 priority。
        seen: dict[str, int] = {}
        real_submit = h.plugin._narrator.submit

        def capture(cue, now=None, *, force=False):
            seen.setdefault(cue.key, cue.priority)
            return real_submit(cue, now, force=force)

        h.plugin._narrator.submit = capture  # type: ignore[method-assign]

        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=2)

        h.client.events.append(fixtures.kill_event(2, "敌人A", "我"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=20, dt=2.0)

        self.assertEqual(
            seen.get("champion_kill"), neko_lol.PRIORITIES["champion_kill"],
            f"人头优先级没查表：{seen}",
        )
        self.assertEqual(
            seen.get("my_death"), neko_lol.PRIORITIES["my_death"],
            f"阵亡优先级没查表（应比人头低）：{seen}",
        )
        self.assertNotEqual(seen["my_death"], seen["champion_kill"])

    async def test_assist_background_hint_only_when_assists(self):
        """有助攻才在提示语里追加"别特意点名"，没助攻不加。"""
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(
            fixtures.kill_event(1, "我", "敌人A", assisters=["队友A"])
        )
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=12, dt=2.0)
        self.assertIn("不用特意点名", h.texts, h.texts)
        self.assertIn("助攻：", h.texts)

    async def test_no_assist_hint_without_assists(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=12, dt=2.0)
        self.assertNotIn("不用特意点名", h.texts)
        self.assertNotIn("助攻：", h.texts)

    async def test_my_death_announced(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(fixtures.kill_event(1, "敌人A", "我"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=20, dt=2.0)

        self.assertRegex(h.texts, r"主人被对面的 Zed(（上单）)? 击杀了")

    async def test_first_blood_recipient_is_the_killer(self):
        """FirstBlood 的 Recipient 是**拿到一血的人**，不是被杀的那个。

        2026-11-16 日志自证：同一秒里既有
          [一血] 抑制同一击杀的人头播报（爱好挨打 → 蛮族之王（电脑））
          已发送（first_blood）：主人拿到了一血。
        抑制要求 Recipient ∈ {击杀者, 被杀者}，"主人拿到"要求 Recipient 是
        主人 —— 交集只有"Recipient = 主人 = 击杀者"。早先注释写反了，
        代码碰巧对；这条测试把它钉死。
        """
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        # 主人（"我"）拿到一血
        h.client.events.append(fixtures.simple_event(1, "FirstBlood", Recipient="我", at=30.0))
        h.client.events.append(fixtures.kill_event(2, "我", "敌人A", at=30.0))
        h.client.data = fixtures.make_all_game_data(game_time=30.0)
        await h.poll(times=10, dt=2.0)

        self.assertIn("主人拿到了一血", h.texts, h.texts)
        self.assertNotIn("被拿一血", h.texts, h.texts)
        self.assertNotIn("一血被对面拿走", h.texts, h.texts)

    async def test_first_blood_by_teammate_is_silent_when_only_mine(self):
        """only_my_events 开着时，队友拿的一血不关主人事，不说。"""
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(fixtures.simple_event(1, "FirstBlood", Recipient="队友A", at=30.0))
        h.client.data = fixtures.make_all_game_data(game_time=30.0)
        await h.poll(times=10, dt=2.0)

        self.assertNotIn("一血", h.texts, f"队友的一血不该播：{h.texts}")

    async def test_first_blood_suppresses_its_own_kill_announcement(self):
        """同一击杀会同时产生 FirstBlood 和 ChampionKill，只该说"一血"。

        接口对"一血"这件事发两条事件：FirstBlood（Recipient）和同一击杀的
        ChampionKill（KillerName/VictimName）。不去重就会同时播报
        "主人拿到了一血"和"主人击杀了对面的 X"。
        """
        for order in ("first_blood_first", "kill_first"):
            h = await self._harness()
            _start_fresh_game(h)
            await h.poll(times=1)

            events = [
                fixtures.simple_event(1, "FirstBlood", Recipient="我", at=30.0),
                fixtures.kill_event(2, "我", "敌人A", at=30.0),
            ]
            if order == "kill_first":
                events.reverse()
            h.client.events.extend(events)
            h.client.data = fixtures.make_all_game_data(game_time=30.0)
            await h.poll(times=8, dt=2.0)

            self.assertIn("一血", h.texts, f"[{order}] 一血没播：{h.texts}")
            self.assertNotIn(
                "击杀了对面的 Zed", h.texts,
                f"[{order}] 一血同时播了人头：{h.texts}",
            )
            self.assertEqual(h.count("一血"), 1, f"[{order}] 一血播了多次：{h.texts}")

    async def test_later_kill_is_not_suppressed(self):
        """抑制只针对一血那一次击杀，之后的击杀照常播。"""
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(fixtures.simple_event(1, "FirstBlood", Recipient="我", at=30.0))
        h.client.events.append(fixtures.kill_event(2, "我", "敌人A", at=30.0))
        h.client.data = fixtures.make_all_game_data(game_time=30.0)
        await h.poll(times=10, dt=2.0)

        h.client.events.append(fixtures.kill_event(3, "我", "敌人B", at=120.0))
        h.client.data = fixtures.make_all_game_data(game_time=120.0)
        await h.poll(times=20, dt=2.0)

        self.assertIn("LeeSin", h.texts, f"一血之后的击杀被误伤：{h.texts}")

    async def test_double_kill_merged_into_one_announcement(self):
        """同一轮里的两个人头合成一条，不是刷两条。"""
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.events.append(fixtures.kill_event(2, "我", "敌人B"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=20, dt=2.0)

        # 用「击杀了对面的 X」这种真实措辞找，不要用裸英雄名：
        # 提示语的固定尾巴里就有 Zed 当举例，裸名字会误命中。
        both = [
            t for t in h.pushes_containing("击杀了对面的 Zed")
            if "击杀了对面的 LeeSin" in t
        ]
        self.assertEqual(len(both), 1, f"两个人头应当合成一条播报：{h.texts}")
        self.assertEqual(h.count("击杀了对面的 Zed"), 1, h.texts)
        self.assertEqual(h.count("击杀了对面的 LeeSin"), 1, h.texts)

    async def test_duplicate_game_start_is_announced_once(self):
        """一局只该有一个开局播报。

        重复的 GameStart（接口补发，或测试台连点两次）如果放过去，会被全局
        间隔挡住、排队 20 秒后再补一句"主人刚开了一局" —— 一局说两次开局。
        """
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        self.assertEqual(h.count("刚开了一局"), 1)

        # 再来一次 GameStart（新的 EventID，模拟接口重复）
        h.client.events.append(fixtures.simple_event(99, "GameStart"))
        await h.settle()
        await h.settle()
        self.assertEqual(h.count("刚开了一局"), 1, f"开局被重复播报了：{h.texts}")

    async def test_new_match_resets_the_game_start_guard(self):
        """第二局必须能正常播报开局。"""
        h = await self._harness({"game_over_grace_seconds": 4})
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)

        _start_fresh_game(h, game_time=4.0)   # 新一局，gameTime 倒流
        await h.poll(times=30, dt=2.0)
        self.assertEqual(h.count("刚开了一局"), 2, f"第二局没播开局：{h.texts}")

    # ── 即时通道：拿到人头立马播报 ──────────────────────────────



    async def test_muted_kill_is_not_announced_instantly(self):
        """面板上关掉「击杀与阵亡」后，即时通道也不能硬发。"""
        h = await self._harness()
        await h.plugin.set_announce(event="champion_kill", enabled=False)
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        before = len(h.pushed)

        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=10, dt=2.0)

        self.assertEqual(len(h.pushed), before, f"关掉的开关被绕过了：{h.texts}")


    async def test_same_event_never_announced_twice(self):
        """游标必须真的推进，否则每轮轮询都会重播同一个事件。"""
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.events.append(
            fixtures.simple_event(1, "BaronKill", KillerName="我", at=60.0)
        )
        h.client.data = fixtures.make_all_game_data(game_time=60.0)
        # 即时通道现在有 12 秒总闸，要跑够时间它才会补发出来
        await h.poll(times=40, dt=2.0)
        self.assertEqual(h.count("拿下了大龙"), 1, h.texts)

    # ── 定期小结 ────────────────────────────────────────────────

    async def test_summary_not_before_minimum_game_time(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.data = fixtures.make_all_game_data(game_time=60.0)  # < 120s 门槛
        await h.poll(times=40, dt=2.0)
        self.assertNotIn("这是当前战况", h.texts)

    async def test_summary_fires_on_interval(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.data = fixtures.make_all_game_data(game_time=1000.0)
        # 首播点是 900 秒（第 15 分钟），跑到 1000 秒就该触发
        await h.poll(times=20, dt=2.0)
        self.assertIn("这是当前战况", h.texts)

    async def test_summary_can_be_disabled(self):
        h = await self._harness({"summary_interval_seconds": 0})
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.data = fixtures.make_all_game_data(game_time=1000.0)
        await h.poll(times=60, dt=2.0)
        self.assertNotIn("这是当前战况", h.texts)

    # ── 结束 ────────────────────────────────────────────────────

    async def test_single_failure_does_not_end_the_game(self):
        """单次请求失败只是抖动，不能立刻宣布对局结束。"""
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.in_game = False
        await h.poll(times=1)
        self.assertTrue(h.plugin._in_game)
        self.assertNotIn("刚打完一局", h.texts)

    async def test_failure_count_alone_is_not_enough(self):
        """次数够了但持续时间不够，同样不能判结束。"""
        h = await self._harness({"game_over_grace_seconds": 3600})
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.in_game = False
        await h.poll(times=20, dt=2.0)
        self.assertTrue(h.plugin._in_game, "宽限期没到就判结束，会放出假的赛后总结")
        self.assertNotIn("刚打完一局", h.texts)

    async def test_consecutive_failures_end_the_game_with_summary(self):
        h = await self._harness({"game_over_grace_seconds": 4})
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertFalse(h.plugin._in_game)
        self.assertIn("刚打完一局", h.texts)

    async def test_transient_outage_mid_game_does_not_replay(self):
        """局内短暂断流后再恢复，绝不能把开局重播一遍。

        这是实测抓到的真 bug：早先的写法在数据消失时把事件游标清成 -1，
        于是客户端抖一下 → 判定"对局结束" → 恢复时 gameTime 还在 90 秒内
        → 走"新对局"分支 → 把已经播过的 GameStart 和所有击杀重播一遍。
        """
        h = await self._harness({"game_over_grace_seconds": 4})
        _start_fresh_game(h)
        await h.poll(times=1)

        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=10, dt=2.0)
        self.assertEqual(h.count("击杀了对面的 Zed"), 1, h.texts)

        # 断流 10 秒（超过宽限期），然后原局恢复、时间继续往前走
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        h.client.in_game = True
        h.client.data = fixtures.make_all_game_data(game_time=70.0)
        await h.poll(times=15, dt=2.0)

        self.assertTrue(h.plugin._in_game, "恢复后应当继续在对局状态")
        self.assertEqual(h.count("刚开了一局"), 1, f"开局被重播了：{h.texts}")
        self.assertEqual(h.count("击杀了对面的 Zed"), 1, f"击杀被重播了：{h.texts}")


    async def test_post_game_summary_can_be_disabled(self):
        h = await self._harness(
            {"post_game_summary": False, "game_over_grace_seconds": 4}
        )
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertFalse(h.plugin._in_game)
        self.assertNotIn("刚打完一局", h.texts)

    async def test_second_game_starts_clean(self):
        """打完一局再开一局，事件游标必须重置，否则第二局全程静音。"""
        h = await self._harness({"game_over_grace_seconds": 4})
        _start_fresh_game(h)
        await h.poll(times=1)
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertFalse(h.plugin._in_game)

        # 第二局：EventID 从头开始，gameTime 倒流
        _start_fresh_game(h, game_time=4.0)
        # 赛后总结是 force 发出的，会占掉全局间隔，所以"第二局开局"
        # 这条要等全局间隔过去才会补发（不能刚说完总结又立刻开口）。
        await h.poll(times=30, dt=2.0)
        self.assertTrue(h.plugin._in_game)
        self.assertEqual(h.count("刚开了一局"), 2, h.texts)

    # ── 探测成本 ────────────────────────────────────────────────

    async def test_idle_polls_less_often(self):
        """没在打游戏时必须降频。

        局外每次探测都要白跑 https+http 两次失败；在受安全软件影响的机器上
        单次被拒连接就有约 2 秒延迟，按局内频率猛敲会白白占满定时器。
        """
        h = await self._harness({"idle_poll_interval_seconds": 10})
        h.client.in_game = False

        calls: list[int] = []
        orig = h.client.snapshot

        async def counting():
            calls.append(1)
            return await orig()

        h.client.snapshot = counting
        await h.poll(times=10, dt=2.0)  # 20 秒虚拟时间

        self.assertLessEqual(len(calls), 3, f"20 秒内探测了 {len(calls)} 次，降频没生效")
        self.assertGreaterEqual(len(calls), 1, "一次都不探测就永远发现不了开局")

    async def test_in_game_polls_at_full_rate(self):
        """进了局内要立刻恢复快轮询，否则事件播报会明显延迟。"""
        h = await self._harness({"idle_poll_interval_seconds": 30})
        _start_fresh_game(h)
        await h.poll(times=1)

        calls: list[int] = []
        orig = h.client.snapshot

        async def counting():
            calls.append(1)
            return await orig()

        h.client.snapshot = counting
        await h.poll(times=10, dt=2.0)  # 20 秒

        # 2 秒间隔 → 大约 10 次
        self.assertGreaterEqual(len(calls), 8, f"局内只轮询了 {len(calls)} 次")

    async def test_overlapping_polls_are_skipped(self):
        """探测比定时器还慢时，绝不能有两次轮询同时跑。

        这不是洁癖：并发的两次轮询会同时读同一个事件游标，把同一个事件
        播报两遍。宁可跳过这一跳。
        """
        import asyncio

        h = await self._harness()
        _start_fresh_game(h)

        entered: list[int] = []
        release = asyncio.Event()
        orig = h.client.snapshot

        async def slow():
            entered.append(1)
            await release.wait()
            return await orig()

        h.client.snapshot = slow
        first = asyncio.create_task(h.plugin.poll_match())
        await asyncio.sleep(0)  # 让第一次真正进入 await

        second = await h.plugin.poll_match()
        self.assertEqual(second.value["status"], "busy")

        release.set()
        await first
        self.assertEqual(len(entered), 1, "第二次轮询不该真的发出去")

    async def test_guard_released_after_poll_finishes(self):
        """重入锁必须在 finally 里放掉，否则一次异常就让轮询永久停摆。"""
        h = await self._harness()
        _start_fresh_game(h)

        def boom(*_a, **_k):
            raise RuntimeError("模拟内部炸了")

        h.plugin._process_events = boom
        with self.assertRaises(RuntimeError):
            await h.plugin.poll_match()
        self.assertFalse(h.plugin._polling, "异常路径没放锁")

        h.plugin._process_events = type(h.plugin)._process_events.__get__(h.plugin)
        await h.plugin.poll_match()
        self.assertTrue(h.plugin._in_game, "放锁之后应当能继续正常轮询")

    # ── 面板开关 ────────────────────────────────────────────────

    async def test_settings_context_lists_every_toggle(self):
        h = await self._harness()
        context = await h.plugin.lol_settings()
        self.assertEqual(
            [item["key"] for item in context["events"]],
            list(neko_lol._TOGGLE_EVENTS),
        )
        self.assertTrue(all(item["enabled"] for item in context["events"]))
        self.assertFalse(context["dry_run"])
        self.assertFalse(context["in_game"])

    async def test_context_ships_chinese_labels(self):
        """面板直接渲染 label，所以 context 里必须带中文名。"""
        h = await self._harness()
        context = await h.plugin.lol_settings()
        for item in context["events"]:
            self.assertEqual(item["label"], neko_lol._EVENT_LABELS[item["key"]])
            self.assertRegex(item["label"], r"[一-鿿]")

    async def test_context_reflects_runtime_state(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        context = await h.plugin.lol_settings()
        self.assertTrue(context["in_game"])
        self.assertTrue(context["game_time"])
        self.assertEqual(context["events"][0]["key"], "game_start")

    async def test_toggle_flips_narrator_immediately(self):
        h = await self._harness()
        result = await h.plugin.set_announce(event="champion_kill", enabled=False)
        self.assertEqual(result.value["event"], "champion_kill")
        self.assertFalse(result.value["enabled"])
        self.assertFalse(h.plugin._cfg.announce["champion_kill"])
        self.assertFalse(h.plugin._narrator.config.announce["champion_kill"])

    async def test_toggle_reports_persistence_failure_honestly(self):
        """离线/宿主不可达时，改动仍生效，但必须如实回报没落盘。"""
        h = await self._harness()
        result = await h.plugin.set_announce(event="ace", enabled=False)
        self.assertFalse(result.value["persisted"], "没写进配置却报成功就是骗人")

    async def test_unknown_event_key_is_rejected(self):
        h = await self._harness()
        result = await h.plugin.set_announce(event="not_a_real_event", enabled=False)
        self.assertIsInstance(result, neko_lol.Err)

    async def test_disabling_summary_actually_silences_it(self):
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.plugin.set_announce(event="summary", enabled=False)

        h.client.data = fixtures.make_all_game_data(game_time=300.0)
        await h.poll(times=180, dt=2.0)
        self.assertNotIn("这是当前战况", h.texts)

    async def test_disabling_post_game_actually_silences_it(self):
        h = await self._harness({"game_over_grace_seconds": 4})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.plugin.set_announce(event="post_game", enabled=False)

        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertFalse(h.plugin._in_game)
        self.assertNotIn("刚打完一局", h.texts)

    async def test_disabling_game_start_silences_it(self):
        h = await self._harness()
        await h.plugin.set_announce(event="game_start", enabled=False)
        _start_fresh_game(h)
        await h.poll(times=10, dt=2.0)
        self.assertTrue(h.plugin._in_game)
        self.assertNotIn("刚开了一局", h.texts)

    async def test_dry_run_toggle(self):
        h = await self._harness()
        result = await h.plugin.set_dry_run(enabled=True)
        self.assertTrue(result.value["enabled"])
        self.assertTrue(h.plugin._cfg.dry_run)
        self.assertTrue(h.plugin._narrator.config.dry_run)

        await h.plugin.set_dry_run(enabled=False)
        self.assertFalse(h.plugin._cfg.dry_run)

    async def test_new_match_rebuilds_player_mapping(self):
        """换局后 ID→英雄+阵营 的映射必须按新快照重建，不能沿用上一局。

        玩家 ID 不变、英雄换了，是最容易出错的场景：如果映射被缓存，
        第二局的播报会拿上一局的英雄名去说。
        """
        h = await self._harness({"game_over_grace_seconds": 4, "window_seconds": 5})

        def roster(me, ally, enemy):
            return [
                fixtures.make_player("我", me, "ORDER"),
                fixtures.make_player("队友A", ally, "ORDER"),
                fixtures.make_player("敌人A", enemy, "CHAOS"),
            ]

        # 第一局
        h.client.data = fixtures.make_all_game_data(
            game_time=60, players=roster("Yasuo", "Lulu", "Zed")
        )
        h.client.events = [fixtures.simple_event(0, "GameStart")]
        await h.poll(times=2, dt=2.0)
        self.assertEqual(h.plugin._last_state.me.champion, "Yasuo")

        # 断流 → 判定结束
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertFalse(h.plugin._in_game)

        # 第二局：同一个 ID，换英雄
        h.client.in_game = True
        h.client.events = []
        h.client.data = fixtures.make_all_game_data(
            game_time=2, players=roster("Garen", "Morgana", "Darius")
        )
        await h.poll(times=4, dt=2.0)

        self.assertTrue(h.plugin._in_game, "没认出第二局")
        self.assertEqual(h.plugin._last_state.me.champion, "Garen", "映射没重建")
        self.assertEqual(h.plugin._cursor, -1, "游标没重置，会漏掉开局事件")

        # 新局的拆塔播报应当用新英雄/新阵营判断
        cue = __import__("neko_lol.cues", fromlist=["x"]).build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "敌人A",
             "TurretKilled": "Turret_TOrder_L2_P3_2_0"},
            h.plugin._last_state,
        )
        self.assertIn("敌方拆掉了我们的", cue.body, cue.body)

    async def test_unknown_side_dragon_still_counts_toward_soul(self):
        """认不出阵营的龙也必须计入"全场第几条"。

        2026-11-16 实测：认不出阵营就 return，总数少算一条，龙魂被锁定到
        后面那条龙上 —— 整局的龙魂播报跟着错。阵营只影响"谁拿的"，
        不影响"这是第几条"。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        # 第一条：主人拿（火）→ 第 1 条
        # 第二条：认不出的名字（风）→ 仍应是第 2 条
        # 第三条：主人拿（土）→ 应是第 3 条，龙魂 = 土
        events = [
            ("我", "Fire"),
            ("路人甲", "Air"),
            ("我", "Earth"),
        ]
        for i, (killer, dtype) in enumerate(events, 1):
            h.client.events.append(
                fixtures.simple_event(20 + i, "DragonKill",
                                      KillerName=killer, DragonType=dtype)
            )
            h.client.data = fixtures.make_all_game_data(game_time=60 + i * 5)
            await h.poll(times=1, dt=2.0)
            await h.settle()

        self.assertEqual(h.plugin._dragon_total, 3, "认不出阵营的龙没计入总数")
        self.assertEqual(h.plugin._soul_type, "土龙", "龙魂锁定错了")

    async def test_duplicate_dragon_event_is_not_counted_twice(self):
        """同一个 EventID 被重复投递时只能计一次。

        龙魂按"全场第 3 条"锁定，多计一条就会锁到更早的龙上。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        dup = fixtures.simple_event(31, "DragonKill", KillerName="我", DragonType="Fire")
        h.client.events.append(dup)
        h.client.data = fixtures.make_all_game_data(game_time=70)
        await h.poll(times=1, dt=2.0)
        await h.settle()
        self.assertEqual(h.plugin._dragon_total, 1)

        # 强制再把同一条塞回去（模拟接口重发）
        h.client.events = list(h.client.events) + [dict(dup)]
        h.client.data = fixtures.make_all_game_data(game_time=75)
        await h.poll(times=2, dt=2.0)
        self.assertEqual(h.plugin._dragon_total, 1, "同一条龙被重复计数了")

    async def test_dragon_soul_uses_third_dragon_of_the_match(self):
        """龙魂属性 = **本局第 3 条被击败的小龙**的属性（不分哪方）。

        某方累计拿 4 条即获得这个龙魂，与中途拿了哪些属性无关。
        注意第 3 条可能是对面拿的 —— 龙魂属性依然按它算。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        # 火 → 水 → 土 → 火
        for i, dtype in enumerate(["Fire", "Water", "Earth", "Fire"], 1):
            h.client.events.append(
                fixtures.simple_event(10 + i, "DragonKill",
                                      KillerName="我", DragonType=dtype)
            )
            h.client.data = fixtures.make_all_game_data(game_time=60 + i * 5)
            await h.poll(times=1, dt=2.0)
            await h.settle()

        self.assertEqual(h.plugin._dragon_counts["ally"], 4)
        # 全场第 3 条是土龙 → 龙魂属性就是土龙
        self.assertEqual(h.plugin._soul_type, "土龙", "本局龙魂属性没记对")
        self.assertIn("土龙魂到手了", h.texts, f"龙魂属性错了：{h.texts}")

    async def test_streak_resets_on_death(self):
        """连杀数在主人阵亡时清零 —— 超神的前提是不死。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        eid = 10
        for i in range(8):   # 8 连杀 → 超神
            h.client.events.append(fixtures.kill_event(eid, "我", f"敌人{(i % 3) + 1}"))
            eid += 1
            h.client.data = fixtures.make_all_game_data(game_time=30 + i * 3)
            await h.poll(times=1, dt=3.0)

        self.assertEqual(h.plugin._my_streak, 8, "连杀没累加")
        self.assertIn("超神", h.texts, f"没播超神：{h.texts}")
        # 只有第 8 杀带特殊文本，其余不该出现"超神"
        self.assertEqual(h.texts.count("超神"), 1, f"超神说了不止一次：{h.texts}")
        # 措辞要和前半句用逗号连起来，不能是"?电脑） 主人超神了。"
        self.assertIn("，主人超神了", h.texts, f"超神措辞不对：{h.texts}")

        # 死一次 → 清零
        h.client.events.append(fixtures.kill_event(eid, "敌人A", "我"))
        h.client.data = fixtures.make_all_game_data(game_time=100)
        await h.poll(times=1, dt=3.0)
        self.assertEqual(h.plugin._my_streak, 0, "死亡后连杀没清零")

        # 再杀一个 → 从 1 开始，不该是超神
        h.client.events.append(fixtures.kill_event(eid + 1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=105)
        await h.poll(times=1, dt=3.0)
        self.assertEqual(h.plugin._my_streak, 1)

    async def test_all_counters_reset_on_new_match(self):
        """连杀数、小龙数、龙魂属性都必须按局清零。

        这四个计数是"本局内"的语义：上一局的 5 连杀不该让这一局刚开局
        就被判成超神；上一局的龙魂也不该延续。
        """
        h = await self._harness({"window_seconds": 5, "game_over_grace_seconds": 4})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        # 第一局：5 连杀 + 3 条龙
        eid = 10
        for i in range(5):
            h.client.events.append(fixtures.kill_event(eid, "我", f"敌人{(i % 3) + 1}"))
            eid += 1
            h.client.data = fixtures.make_all_game_data(game_time=30 + i * 3)
            await h.poll(times=1, dt=3.0)
        for i in range(3):
            h.client.events.append(
                fixtures.simple_event(eid, "DragonKill", KillerName="我", DragonType="Fire")
            )
            eid += 1
            h.client.data = fixtures.make_all_game_data(game_time=50 + i * 3)
            await h.poll(times=1, dt=3.0)

        self.assertEqual(h.plugin._my_streak, 5)
        self.assertEqual(h.plugin._dragon_total, 3)
        self.assertEqual(h.plugin._dragon_counts["ally"], 3)
        self.assertEqual(h.plugin._soul_type, "火龙")

        # 结束第一局
        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertFalse(h.plugin._in_game)

        # 第二局
        h.client.in_game = True
        h.client.events = []
        h.client.data = fixtures.make_all_game_data(game_time=2)
        await h.poll(times=4, dt=2.0)
        self.assertTrue(h.plugin._in_game, "没认出第二局")

        self.assertEqual(h.plugin._my_streak, 0, "连杀没清零")
        self.assertEqual(h.plugin._dragon_total, 0, "小龙总数没清零")
        self.assertEqual(h.plugin._dragon_counts, {"ally": 0, "enemy": 0}, "小龙计数没清零")
        self.assertEqual(h.plugin._soul_type, "", "龙魂属性没清零")

    async def test_terminated_one_short_of_legendary(self):
        """7 连杀被终结时加特殊文本，其他连杀数不加。"""
        async def run(kills: int) -> str:
            h = await self._harness({"window_seconds": 5})
            _start_fresh_game(h)
            await h.poll(times=1)
            await h.settle()
            base = len(h.pushed)
            eid = 10
            for i in range(kills):
                h.client.events.append(
                    fixtures.kill_event(eid, "我", f"敌人{(i % 3) + 1}")
                )
                eid += 1
                h.client.data = fixtures.make_all_game_data(game_time=30 + i * 3)
                await h.poll(times=1, dt=3.0)
            h.client.events.append(fixtures.kill_event(eid, "敌人A", "我"))
            h.client.data = fixtures.make_all_game_data(game_time=90)
            await h.poll(times=1, dt=3.0)
            await h.settle()
            return " | ".join(p["parts"][0]["text"] for p in h.pushed[base:])

        for kills, expect in ((7, True), (5, False), (8, False)):
            texts = await run(kills)
            got = "被终结了" in texts
            self.assertEqual(got, expect, f"{kills} 连杀被终结：{'出现了' if got else '没出现'}")

    async def test_soul_type_from_enemy_third_dragon(self):
        """第 3 条小龙是**对面**拿的，龙魂属性依然按它算。

        这是最容易写错的地方：龙魂属性属于"这一局"，不属于"哪一方"。
        谁拿到第 4 条，谁就获得这个属性的龙魂。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        # 对面拿土龙、土龙，我拿水龙（全场第 3 条）→ 龙魂 = 水龙
        script = [("敌人A", "Earth"), ("敌人A", "Earth"), ("我", "Water")]
        for i, (killer, dtype) in enumerate(script, 1):
            h.client.events.append(
                fixtures.simple_event(10 + i, "DragonKill",
                                      KillerName=killer, DragonType=dtype)
            )
            h.client.data = fixtures.make_all_game_data(game_time=60 + i * 5)
            await h.poll(times=1, dt=2.0)
            await h.settle()

        self.assertEqual(h.plugin._dragon_total, 3)
        self.assertEqual(h.plugin._soul_type, "水龙", "第 3 条（水龙）没记成龙魂")
        self.assertEqual(h.plugin._dragon_counts["enemy"], 2)
        self.assertEqual(h.plugin._dragon_counts["ally"], 1)

    async def test_burst_summary_on_kill_flurry(self):
        """15 分钟后，30 秒内 ≥5 个人头 → 补一次小结。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        def summaries() -> int:
            return sum(1 for p in h.pushed[base:]
                       if "这是当前战况" in p["parts"][0]["text"])

        # 15:00 之后，5 个人头挤在 30 秒窗口内（两边都有）
        kill_base = 900.0
        for i in range(5):
            killer = "我" if i % 2 == 0 else "敌人A"
            victim = "敌人A" if i % 2 == 0 else "我"
            h.client.events.append(
                fixtures.kill_event(200 + i, killer, victim, at=kill_base + i * 4)
            )
            h.client.data = fixtures.make_all_game_data(game_time=kill_base + i * 4)
            await h.poll(times=1, dt=2.0)
            await h.settle()
        self.assertGreaterEqual(summaries(), 1, f"爆发没触发小结：{h.texts}")

    async def test_burst_summary_not_before_fifteen_minutes(self):
        """15 分钟之前不启用爆发小结（用户 2026-11-17 指定）。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        def summaries() -> int:
            return sum(1 for p in h.pushed[base:]
                       if "这是当前战况" in p["parts"][0]["text"])

        # 第 5 分钟就来一波五连杀
        for i in range(6):
            h.client.events.append(
                fixtures.kill_event(300 + i, "我", "敌人A", at=300.0 + i * 2)
            )
            h.client.data = fixtures.make_all_game_data(game_time=300.0 + i * 2)
            await h.poll(times=1, dt=1.0)
        await h.settle()
        self.assertEqual(summaries(), 0, f"15 分钟前不该爆发小结：{h.texts}")

    async def test_burst_and_periodic_share_the_cooldown(self):
        """定时小结刚播完，爆发小结不该紧跟着再来一条。

        实测踩过：第 15 分钟定时小结播完 4 秒，五连杀又把爆发小结顶出来，
        连着两句"当前战况"。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        def summaries() -> int:
            return sum(1 for p in h.pushed[base:]
                       if "这是当前战况" in p["parts"][0]["text"])

        # 正好在首播点 900 秒那一刻来五连杀
        eid = 700
        for i in range(5):
            h.client.events.append(
                fixtures.kill_event(eid, "我", "敌人A", at=900.0 + i)
            )
            eid += 1
            h.client.data = fixtures.make_all_game_data(game_time=900.0 + i)
            await h.poll(times=1, dt=1.0)
            await h.settle()
        self.assertEqual(summaries(), 1, f"撞车播了两条小结：{h.texts}")

    async def test_aram_kills_are_merged_not_announced_one_by_one(self):
        """大乱斗人头走环境档：一波多个击杀合并成一句。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        h.client.data = fixtures.make_all_game_data(game_time=10.0, map_number=12)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        for i, victim in enumerate(("敌人A", "敌人B", "敌人C"), 1):
            h.client.events.append(fixtures.kill_event(500 + i, "我", victim))
            h.client.data = fixtures.make_all_game_data(
                game_time=10.0 + i, map_number=12
            )
            await h.poll(times=1)
        await h.settle()

        texts = [p["parts"][0]["text"] for p in h.pushed[base:]]
        self.assertLessEqual(len(texts), 1, f"人头没合并：{texts}")
        self.assertTrue(texts and "刚才这一段时间" in texts[0], texts)

    async def test_aram_routine_deaths_are_silent(self):
        """大乱斗里"死一次"不播；只有连杀被断 / 连着倒两次才播。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        h.client.data = fixtures.make_all_game_data(game_time=10.0, map_number=12)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        # 孤立的一次死亡（20 秒内没有第二次）→ 不播
        h.client.events.append(fixtures.kill_event(600, "敌人A", "我", at=100.0))
        h.client.data = fixtures.make_all_game_data(game_time=100.0, map_number=12)
        await h.poll(times=1)
        await h.settle()
        self.assertNotIn("被对面的", h.texts.split("|")[-1] if h.pushed[base:] else "")

        deaths = [p for p in h.pushed[base:]
                  if "被对面的" in p["parts"][0]["text"]]
        self.assertEqual(len(deaths), 0, f"孤立死亡被播了：{h.texts}")

    async def test_aram_double_death_is_announced_once(self):
        """20 秒内连着倒下两次 → 只播一条；之后继续死不再刷。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        h.client.data = fixtures.make_all_game_data(game_time=10.0, map_number=12)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        eid = 700
        for i in range(5):
            h.client.events.append(
                fixtures.kill_event(eid, "敌人A", "我", at=200.0 + i * 2)
            )
            eid += 1
            h.client.data = fixtures.make_all_game_data(
                game_time=200.0 + i * 2, map_number=12
            )
            await h.poll(times=1)
            await h.settle()

        deaths = [p["parts"][0]["text"] for p in h.pushed[base:]
                  if "被对面的" in p["parts"][0]["text"]]
        self.assertEqual(len(deaths), 1, f"连死 5 次播了 {len(deaths)} 条：{deaths}")
        self.assertIn("连着倒了两次", deaths[0])

    async def test_aram_burst_needs_eight_kills(self):
        """大乱斗爆发小结门槛 8 人（峡谷是 5 人）。"""
        async def fired(n: int) -> bool:
            h = await self._harness({"window_seconds": 5})
            _start_fresh_game(h)
            h.client.data = fixtures.make_all_game_data(game_time=5.0, map_number=12)
            h.client.events = [fixtures.simple_event(0, "GameStart")]
            await h.poll(times=1)
            # 先让 1000 秒那一刻的定时小结播掉，再等冷却过去
            h.client.data = fixtures.make_all_game_data(game_time=1000.0, map_number=12)
            await h.poll(times=3, dt=2.0)
            for _ in range(70):
                h.clock.advance(2.0)
            base = len(h.pushed)
            eid = 900
            for i in range(n):
                h.client.events.append(
                    fixtures.kill_event(eid, "我", "敌人A", at=1000.0 + i)
                )
                eid += 1
                h.client.data = fixtures.make_all_game_data(
                    game_time=1000.0 + i, map_number=12
                )
                await h.poll(times=1, dt=0.5)
            await h.poll(times=5, dt=1.0)
            return any("这是当前战况" in p["parts"][0]["text"] for p in h.pushed[base:])

        self.assertFalse(await fired(5), "5 杀就触发了，门槛没生效")
        self.assertTrue(await fired(8), "8 杀没触发")

    async def test_roster_is_captured_at_game_start(self):
        """开局记下双方阵容；排位带分路后缀，非排位不带。"""
        h = await self._harness({"window_seconds": 1})
        _start_fresh_game(h)
        await h.poll(times=1)

        roster = h.plugin._roster_text
        self.assertTrue(roster, "开局没记阵容")
        self.assertIn("我方：", roster)
        self.assertIn("敌方：", roster)
        # 默认夹具是峡谷（分路可信）→ 每个英雄后面都该有分路后缀
        self.assertIn("（上单）", roster, f"排位没带分路：{roster}")
        self.assertIn("（打野）", roster, roster)

        # 阵容也要进 _recent，"刚才发生了什么"能看到
        self.assertTrue(
            any(t.startswith("阵容｜") for _at, t in h.plugin._recent),
            f"_recent 里没有阵容：{list(h.plugin._recent)}",
        )

    async def test_roster_has_no_lane_suffix_in_aram(self):
        h = await self._harness({"window_seconds": 1})
        h.client.in_game = True
        h.client.data = fixtures.make_all_game_data(game_time=5.0, map_number=12)
        h.client.events = [fixtures.simple_event(0, "GameStart")]
        await h.poll(times=1)
        self.assertNotIn("（上单）", h.plugin._roster_text,
                         f"大乱斗不该有分路：{h.plugin._roster_text}")

    async def test_recent_and_roster_clear_between_matches(self):
        """换局时上一局的播报和阵容都要清掉。"""
        h = await self._harness({"window_seconds": 1})
        _start_fresh_game(h)
        await h.poll(times=1)
        first_roster = h.plugin._roster_text
        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=60.0)
        await h.poll(times=5)
        self.assertGreater(len(h.plugin._recent), 1)

        # 断流 → 新局（gameTime 倒流）
        h.client.in_game = False
        await h.poll(times=12, dt=2.0)
        h.client.in_game = True
        # 换一套阵容，才能验证 roster 真的重算了
        h.client.data = fixtures.make_all_game_data(game_time=0.0, players=[
            fixtures.make_player("我", "暗裔剑魔", "ORDER"),
            fixtures.make_player("队友A", "疾风剑豪", "ORDER"),
            fixtures.make_player("敌人A", "离群之刺", "CHAOS"),
            fixtures.make_player("敌人B", "影流之镰", "CHAOS"),
        ])
        h.client.events = [fixtures.simple_event(100, "GameStart")]
        await h.poll(times=2)

        self.assertNotEqual(h.plugin._roster_text, first_roster, "阵容没更新")
        self.assertIn("暗裔剑魔", h.plugin._roster_text, h.plugin._roster_text)
        # 新局只该有「阵容」+「开局」两条，上一局的人头和击杀必须没了
        self.assertEqual(len(h.plugin._recent), 2,
                         f"旧局播报没清：{list(h.plugin._recent)}")
        texts = [t for _a, t in h.plugin._recent]
        self.assertTrue(any(t.startswith("阵容｜") for t in texts), texts)
        self.assertFalse(
            any("击杀了对面的" in t for t in texts),
            f"上一局的击杀还在：{texts}",
        )

    async def test_ace_suppresses_burst_summary(self):
        """同一拍里出现团灭时，跳过爆发小结（团灭已经把这一波说清楚了）。

        实测踩过：五连杀触发小结，紧接着 Ace 又播一条，连着两句都在说同一波团。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        # 五连杀，同一拍跟一条团灭
        eid = 800
        for i in range(5):
            h.client.events.append(
                fixtures.kill_event(eid, "我", "敌人A", at=1000.0 + i)
            )
            eid += 1
        h.client.events.append(
            fixtures.simple_event(eid, "Ace", Acer="我", at=1005.0)
        )
        h.client.data = fixtures.make_all_game_data(game_time=1005.0)
        await h.poll(times=1)
        await h.poll(times=8, dt=1.0)

        texts = [p["parts"][0]["text"] for p in h.pushed[base:]]
        self.assertTrue(
            any("团灭了对面" in t for t in texts), f"团灭没播：{h.texts}"
        )
        self.assertFalse(
            any("这是当前战况" in t for t in texts),
            f"团灭和小结撞车了：{h.texts}",
        )

    async def test_burst_window_is_thirty_seconds(self):
        """窗口 30 秒：跨度 24 秒的 5 个头发作，跨度 32 秒的不算。"""
        async def fired(spacing: float) -> bool:
            h = await self._harness({"window_seconds": 5})
            _start_fresh_game(h)
            await h.poll(times=1)
            await h.settle()
            # 先消化掉 15 分钟那次定时小结
            h.client.data = fixtures.make_all_game_data(game_time=905.0)
            await h.poll(times=3, dt=2.0)
            await h.settle()
            for _ in range(35):
                h.clock.advance(2.0)      # 让小结冷却过去
            base = len(h.pushed)
            eid = 900
            for i in range(5):
                h.client.events.append(
                    fixtures.kill_event(eid, "我", "敌人A", at=1000.0 + i * spacing)
                )
                eid += 1
                h.client.data = fixtures.make_all_game_data(
                    game_time=1000.0 + i * spacing
                )
                await h.poll(times=1, dt=0.5)
            await h.poll(times=5, dt=1.0)
            return any(
                "这是当前战况" in p["parts"][0]["text"] for p in h.pushed[base:]
            )

        self.assertTrue(await fired(6.0), "24 秒内 5 个头的波没触发")
        self.assertFalse(await fired(8.0), "跨度 32 秒超过了 30 秒窗口却触发")

    async def test_burst_summary_has_cooldown(self):
        """两次爆发小结之间至少隔冷却时间（默认 60 秒）。"""
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        def summaries() -> int:
            return sum(1 for p in h.pushed[base:]
                       if "这是当前战况" in p["parts"][0]["text"])

        # 小结是"不可合并"档，submit 当拍就发，不需要 settle 推进窗口 ——
        # 用 dt=0.2 让墙钟慢慢走，两波之间才不至于自己超出 60 秒冷却。
        eid = 400
        for wave, base_time in enumerate((910.0, 930.0)):
            for i in range(5):
                h.client.events.append(
                    fixtures.kill_event(eid, "我", "敌人A", at=base_time + i)
                )
                eid += 1
                h.client.data = fixtures.make_all_game_data(game_time=base_time + i)
                await h.poll(times=1, dt=0.2)
            if wave == 0:
                first = summaries()
                self.assertGreaterEqual(first, 1, f"第一波没触发：{h.texts}")
        self.assertEqual(summaries(), first, "冷却期里又播了小结")

    async def test_summary_schedule_uses_game_time(self):
        """小结在对局第 15 分钟首播，之后每 5 分钟一次。

        调度基准是**对局时间**不是墙钟：暂停、断流、重连都不该让播报点
        漂移。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        def summaries() -> int:
            return sum(1 for p in h.pushed[base:]
                       if "这是当前战况" in p["parts"][0]["text"])

        # 14:59 还不该播
        h.client.data = fixtures.make_all_game_data(game_time=899)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 0, "没到 15 分钟就播了")

        # 15:01 → 首播
        h.client.data = fixtures.make_all_game_data(game_time=901)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 1, f"15 分钟没播：{h.texts}")

        # 19:59 → 距首播还不到 5 分钟，不该再播
        h.client.data = fixtures.make_all_game_data(game_time=1199)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 1, "没到 20 分钟又播了")

        # 20:01 → 第二次
        h.client.data = fixtures.make_all_game_data(game_time=1201)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 2, f"20 分钟没播：{h.texts}")

    async def test_elder_dragon_not_counted_toward_soul(self):
        """远古巨龙不计入龙魂计数。

        计入的话：一方拿 3 条元素龙 + 1 条远古 = 计数 4，真正的第 4 条
        元素龙会变成第 5 条，龙魂播报永远不出现。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        script = [
            ("我", "Fire"), ("我", "Fire"), ("我", "Fire"),
            ("我", "Elder"),          # 远古：不该计入
            ("我", "Fire"),           # 这才是第 4 条元素龙
        ]
        for i, (killer, dtype) in enumerate(script, 1):
            h.client.events.append(
                fixtures.simple_event(10 + i, "DragonKill",
                                      KillerName=killer, DragonType=dtype)
            )
            h.client.data = fixtures.make_all_game_data(game_time=60 + i * 5)
            await h.poll(times=1, dt=2.0)
            await h.settle()

        # 3 条元素龙 + 远古 + 第 4 条元素龙 → 阵营计数应是 4（不是 5）
        self.assertEqual(h.plugin._dragon_counts["ally"], 4,
                         f"远古被算进去了：{h.plugin._dragon_counts}")
        self.assertEqual(h.plugin._dragon_total, 4)
        self.assertIn("火龙魂到手了", h.texts, f"龙魂没播：{h.texts}")

    async def test_post_game_uses_authoritative_result(self):
        """赛后总结用 GameEnd 给的权威结果，不再按击杀数猜。

        即使击杀数落后（按推断会判"输了"），只要 GameEnd 说 Win，
        总结就必须说赢了。
        """
        h = await self._harness({"game_over_grace_seconds": 4, "window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        # 制造"击杀数落后"的局面：敌人多拿几个人头
        for i in range(4):
            h.client.events.append(
                fixtures.kill_event(10 + i, "敌人A", "队友A")
            )
        h.client.events.append(fixtures.simple_event(20, "GameEnd", Result="Win"))
        h.client.data = fixtures.make_all_game_data(game_time=1800)
        await h.poll(times=2, dt=2.0)

        self.assertEqual(h.plugin._game_result, "Win", "没记下结算结果")

        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        texts = h.texts
        self.assertIn("赢了", texts, f"没用权威结果：{texts}")
        self.assertNotIn("按击杀数推断", texts)
        self.assertNotIn("措辞上别说死", texts)

    async def test_post_game_does_not_guess_result_when_missing(self):
        """没收到 GameEnd 时，总结**不许猜输赢**。

        击杀数领先不代表赢了（可能被偷家）。猜错比不说更糟，
        所以只报事实，胜负留白。
        """
        h = await self._harness({"game_over_grace_seconds": 4, "window_seconds": 5})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        h.client.in_game = False
        await h.poll(times=6, dt=2.0)

        texts = h.texts
        self.assertNotIn("赢了", texts, f"猜了胜负：{texts}")
        self.assertNotIn("输了", texts, f"猜了胜负：{texts}")
        self.assertIn("不要说输赢", texts, f"没有禁止模型猜：{texts}")

    async def test_aram_summary_cadence(self):
        """大乱斗系列小结：第 8 分钟首播，之后每 3 分钟一次。

        海克斯大乱斗等变种也走这套 —— 它们都在嚎哭深渊（map 12）上。
        """
        h = await self._harness({"window_seconds": 5})
        _start_fresh_game(h)

        def aram(game_time: float) -> dict:
            data = fixtures.make_all_game_data(game_time=game_time)
            data["gameData"].update({"mapNumber": 12, "gameMode": "ARAM"})
            return data

        h.client.data = aram(10)
        await h.poll(times=2, dt=2.0)
        await h.settle()
        base = len(h.pushed)

        def summaries() -> int:
            return sum(1 for p in h.pushed[base:]
                       if "这是当前战况" in p["parts"][0]["text"])

        # 7:59 不播
        h.client.data = aram(479)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 0, "没到 8 分钟就播了")

        # 8:01 首播
        h.client.data = aram(481)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 1, f"8 分钟没播：{h.texts}")

        # 10:59 还不到 11 分钟（8+3）
        h.client.data = aram(659)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 1, "没到 11 分钟又播了")

        # 11:01 第二次
        h.client.data = aram(661)
        await h.poll(times=3, dt=2.0)
        await h.settle()
        self.assertEqual(summaries(), 2, f"11 分钟没播：{h.texts}")

    async def test_game_end_flushes_pending_window(self):
        """GameEnd 一到就结算窗口 —— 对局结束了，攒着的推塔再不说是没机会了。"""
        h = await self._harness({"window_seconds": 30})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        # 攒两条环境档进窗口
        for i, (name, extra) in enumerate((
            ("TurretKilled", {"TurretKilled": "Turret_TChaos_L2_P3_1_0"}),
            ("InhibKilled", {"InhibKilled": "Inhib_TChaos_L1_P1_1_0"}),
        ), 1):
            h.client.events.append(
                fixtures.simple_event(10 + i, name, KillerName="我", **extra)
            )
            h.client.data = fixtures.make_all_game_data(game_time=60 + i * 3)
            await h.poll(times=1, dt=2.0)

        self.assertEqual(h.plugin._narrator.queue_depth, 2, "事件没进窗口")
        base = len(h.pushed)

        # GameEnd 触发
        h.client.events.append(fixtures.simple_event(20, "GameEnd", Result="Win"))
        h.client.data = fixtures.make_all_game_data(game_time=200)
        await h.poll(times=1, dt=2.0)

        self.assertEqual(h.plugin._narrator.queue_depth, 0, "窗口没被结算")
        new_texts = [p["parts"][0]["text"] for p in h.pushed[base:]]
        # 合并内容 + 赛后总结（"这局结束了"已并入总结，不再单独播）
        self.assertEqual(len(new_texts), 2, f"应当是合并内容 + 赛后总结：{new_texts}")
        self.assertIn("上路外塔", new_texts[0])
        self.assertIn("兵营", new_texts[0])
        self.assertIn("刚打完一局", new_texts[1], "GameEnd 当轮该播赛后总结")
        self.assertIn("赢了", new_texts[1], "总结里应当带结果")
        self.assertNotIn("这局结束了。", new_texts[1], "不该再有独立的结束播报")

    async def test_game_end_plays_post_game_immediately(self):
        """GameEnd 一到就直接播赛后总结 —— 两者合并成一条。

        早先是"这局结束了" + 5 秒后"赛后总结"两条。中间那段静默听感上
        像卡住，而且一句话信息量太低。
        """
        h = await self._harness({"window_seconds": 30, "game_over_grace_seconds": 20})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        # game_time 用 890：避开 900 秒那个定时小结点，否则会多一条小结
        h.client.events.append(fixtures.simple_event(10, "GameEnd", Result="Win"))
        h.client.data = fixtures.make_all_game_data(game_time=890)
        await h.poll(times=1, dt=2.0)

        new_texts = [p["parts"][0]["text"] for p in h.pushed[base:]]
        self.assertEqual(len(new_texts), 1, f"应当只发一条总结：{new_texts}")
        self.assertIn("刚打完一局", new_texts[0], "GameEnd 当轮没播总结")
        # 合并且不再有独立的"这局结束了"
        self.assertNotIn("这局结束了。", new_texts[0], new_texts[0])

    async def test_post_game_switch_mutes_the_only_end_broadcast(self):
        """合并之后赛后总结是唯一的结束播报，面板开关必须生效。"""
        h = await self._harness({
            "window_seconds": 30,
            "announce": {"post_game": False},
        })
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        h.client.events.append(fixtures.simple_event(10, "GameEnd", Result="Win"))
        h.client.data = fixtures.make_all_game_data(game_time=890)
        await h.poll(times=4, dt=2.0)

        new_texts = [p["parts"][0]["text"] for p in h.pushed[base:]]
        self.assertFalse(
            any("刚打完一局" in t for t in new_texts),
            f"关掉开关后仍播了总结：{new_texts}",
        )

    async def test_post_game_not_repeated_by_data_missing_path(self):
        """GameEnd 已经播过总结后，数据消失判定不能再播第二遍。"""
        h = await self._harness(
            {"window_seconds": 30, "game_over_grace_seconds": 4}
        )
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        h.client.events.append(fixtures.simple_event(10, "GameEnd", Result="Win"))
        h.client.data = fixtures.make_all_game_data(game_time=900)
        await h.poll(times=4, dt=2.0)          # 跨过 5 秒，总结已播

        h.client.in_game = False               # 接口消失，触发旧判定
        await h.poll(times=8, dt=2.0)

        count = sum(
            1 for p in h.pushed if "刚打完一局" in p["parts"][0]["text"]
        )
        self.assertEqual(count, 1, f"赛后总结播了 {count} 遍")

    async def test_post_game_fires_without_game_end(self):
        """没收到 GameEnd 时（中途退出），数据消失判定仍要兜住总结。"""
        h = await self._harness(
            {"window_seconds": 30, "game_over_grace_seconds": 4}
        )
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        h.client.in_game = False
        await h.poll(times=8, dt=2.0)
        self.assertIn("刚打完一局", h.texts, "没有 GameEnd 时总结丢了")

    async def test_no_double_period_in_any_address_mode(self):
        """三种称呼模式下都不能出现双标点。

        body 由 _normalize_body 统一补句号，模板再补一个就会拼出
        "影流之主。。"——实测出现过。
        """
        for mode in ("nickname", "name", "code"):
            h = await self._harness({"window_seconds": 10, "champion_address": mode})
            _start_fresh_game(h)
            await h.poll(times=1)
            await h.settle()
            h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
            h.client.data = fixtures.make_all_game_data(game_time=60)
            await h.poll(times=1, dt=2.0)
            await h.settle()
            texts = [p["parts"][0]["text"] for p in h.pushed]
            for text in texts:
                for bad in ("。。", "。，", "，。", "！。"):
                    self.assertNotIn(bad, text, f"{mode} 模式出现 {bad}：{text[:120]}")


    async def test_ambient_events_merge_into_one_message(self):
        """环境档事件（塔/兵营/先锋）攒在窗口里，合成一条。"""
        h = await self._harness({"window_seconds": 10})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle(12)
        base = len(h.pushed)

        # 环境档：推塔 / 破兵营 / 先锋
        h.client.events.append(
            fixtures.simple_event(10, "TurretKilled", KillerName="我",
                                  TurretKilled="Turret_TChaos_L2_P3_1_0")
        )
        h.client.data = fixtures.make_all_game_data(game_time=40)
        await h.poll(times=1, dt=2.0)
        h.client.events.append(
            fixtures.simple_event(11, "InhibKilled", KillerName="我",
                                  InhibKilled="Inhib_TChaos_L1_P1_1_0")
        )
        h.client.data = fixtures.make_all_game_data(game_time=42)
        await h.poll(times=1, dt=2.0)
        h.client.events.append(
            fixtures.simple_event(12, "HeraldKill", KillerName="我")
        )
        h.client.data = fixtures.make_all_game_data(game_time=44)
        await h.poll(times=1, dt=2.0)

        self.assertEqual(len(h.pushed), base, "窗口还没结束就播报了")
        await h.settle(12)
        self.assertEqual(len(h.pushed), base + 1, f"应当合并成 1 条：{h.texts}")
        last = h.pushed[-1]["parts"][0]["text"]
        for word in ("外塔", "兵营", "峡谷先锋"):
            self.assertIn(word, last, f"合并内容漏了 {word}：{last[:120]}")

    async def test_window_does_not_reset_on_new_items(self):
        """窗口从第一条开始计时，后续条目不能重置倒计时。"""
        h = await self._harness({"window_seconds": 10})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        h.client.events.append(
            fixtures.simple_event(1, "TurretKilled", KillerName="我",
                                  TurretKilled="Turret_TChaos_L2_P3_1_0")
        )
        h.client.data = fixtures.make_all_game_data(game_time=30.0)
        await h.poll(times=1, dt=2.0)      # 开窗
        for i in range(1, 4):               # 窗口内再加 3 条
            h.client.events.append(
                fixtures.simple_event(10 + i, "InhibKilled", KillerName="我",
                                      InhibKilled="Inhib_TChaos_L1_P1_1_0")
            )
            await h.poll(times=1, dt=2.0)

        await h.poll(times=3, dt=2.0)       # 从开窗算起已过 10 秒
        self.assertEqual(len(h.pushed), base + 1, "倒计时被后续条目重置了")

    async def test_muted_event_never_enters_window(self):
        h = await self._harness()
        await h.plugin.set_announce(event="champion_kill", enabled=False)
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        base = len(h.pushed)

        h.client.events.append(fixtures.kill_event(1, "我", "敌人A"))
        h.client.data = fixtures.make_all_game_data(game_time=40.0)
        await h.poll(times=1, dt=2.0)
        self.assertEqual(h.plugin._narrator.queue_depth, 0, "静音的事件进了窗口")
        await h.settle()
        self.assertEqual(len(h.pushed), base, "静音事件还是被播报了")

    async def test_post_game_forces_immediate_broadcast(self):
        """赛后总结不等窗口 —— 错过就永远没机会说了。"""
        h = await self._harness({"game_over_grace_seconds": 4, "window_seconds": 60})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertIn("刚打完一局", h.texts, f"赛后总结被窗口拖住了：{h.texts}")

    async def test_window_flushes_on_game_end(self):
        """对局结束时窗口里攒下的内容要立刻结算，不能丢。"""
        h = await self._harness({"game_over_grace_seconds": 4, "window_seconds": 60})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()

        h.client.events.append(
            fixtures.simple_event(1, "TurretKilled", KillerName="我",
                                  TurretKilled="Turret_TChaos_L2_P3_1_0")
        )
        h.client.data = fixtures.make_all_game_data(game_time=60.0)
        await h.poll(times=1, dt=2.0)
        self.assertNotIn("外塔", h.texts, "窗口没结束却提前播了")

        h.client.in_game = False
        await h.poll(times=6, dt=2.0)
        self.assertIn("外塔", h.texts, f"对局结束时窗口内容被丢了：{h.texts}")

    # ── 通道契约 ────────────────────────────────────────────────

    async def test_pushes_through_respond_channel(self):
        """必须走 ai_behavior=respond 才会真的说话。

        ``visibility`` 和 ``ai_behavior`` 是**独立**的两件事：前者只管
        "看不看得到"，后者才决定模型读不读、回不回。默认配置下提示文本
        不在聊天里显示（visibility=[]），但猫娘照样回应。
        """
        h = await self._harness()
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        self.assertTrue(h.pushed, "窗口结束后仍没有播报")
        payload = h.pushed[0]
        self.assertEqual(payload["ai_behavior"], "respond")
        self.assertEqual(payload["source"], "neko_lol")
        self.assertEqual(payload["parts"][0]["type"], "text")
        # 默认：提示文本不在聊天里出现
        self.assertEqual(payload["visibility"], [])

    async def test_prompt_visibility_can_be_enabled(self):
        """打开 show_prompt_in_chat 后提示文本才进聊天窗口。"""
        h = await self._harness({"show_prompt_in_chat": True})
        _start_fresh_game(h)
        await h.poll(times=1)
        await h.settle()
        self.assertTrue(h.pushed)
        self.assertEqual(h.pushed[0]["visibility"], ["chat"])

    async def test_dry_run_never_pushes(self):
        h = await self._harness({"dry_run": True})
        _start_fresh_game(h)
        h.client.events.append(fixtures.simple_event(1, "Ace", Acer="我"))
        await h.poll(times=30, dt=2.0)
        self.assertEqual(h.pushed, [])

    async def test_disabled_plugin_does_not_poll(self):
        h = await self._harness({"enabled": False})
        _start_fresh_game(h)
        await h.poll(times=10)
        self.assertEqual(h.pushed, [])
        self.assertFalse(h.plugin._in_game)

    async def test_not_in_game_from_the_start_is_silent(self):
        h = await self._harness()
        h.client.in_game = False
        await h.poll(times=10)
        self.assertEqual(h.pushed, [])
        self.assertFalse(h.plugin._in_game)

    async def test_push_failure_does_not_break_polling(self):
        """宿主不可用时插件必须继续活着，而不是每轮抛异常。"""
        h = await self._harness()

        def boom(**_kwargs):
            raise RuntimeError("宿主没了")

        h.plugin.push_message = boom
        _start_fresh_game(h)
        await h.poll(times=5)
        self.assertTrue(h.plugin._in_game)

    async def test_survives_malformed_payload(self):
        """接口返回半截 JSON 时不能把插件循环搞崩。"""
        h = await self._harness()
        h.client.data = {"allPlayers": "not-a-list", "gameData": None}
        h.client.events = [{"no_event_id": True}, "not-a-dict"]
        await h.poll(times=5)
        self.assertTrue(h.plugin._in_game)


def _roster_with_me_at(index: int) -> list[dict]:
    """造一份"主人排在第 index 位"的同队名单（0=上单 … 4=辅助）。"""
    lanes = ["上单哥", "打野哥", "中单哥", "ADC哥", "辅助哥"]
    players: list[dict] = []
    for i, name in enumerate(lanes):
        if i == index:
            players.append(fixtures.make_player("我", "Yasuo", "ORDER", kills=1, assists=3))
        else:
            players.append(fixtures.make_player(name, f"英雄{i}", "ORDER", assists=2))
    players.append(fixtures.make_player("敌人A", "Zed", "CHAOS"))
    return players


class TestSupportAssists(unittest.IsolatedAsyncioTestCase):
    """辅助位的助攻播报：只在峡谷征召 + 判定为辅助时开。"""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self._real_time = neko_lol.time
        neko_lol.time = self.clock  # type: ignore[assignment]

    def tearDown(self) -> None:
        neko_lol.time = self._real_time  # type: ignore[assignment]

    async def _assist_run(self, lane_index: int, raw=None, *, map_number: int = 11):
        h = Harness(self.clock, raw)
        await h.start()
        players = _roster_with_me_at(lane_index)
        h.client.in_game = True
        h.client.data = fixtures.make_all_game_data(
            game_time=5.0, players=players, map_number=map_number
        )
        h.client.events = [fixtures.simple_event(0, "GameStart")]
        await h.poll(times=1)
        # 队友击杀，主人在助攻名单里
        h.client.events.append(
            fixtures.kill_event(1, "中单哥", "敌人A", assisters=["我"])
        )
        h.client.data = fixtures.make_all_game_data(
            game_time=40.0, players=players, map_number=map_number
        )
        await h.poll(times=12, dt=2.0)
        return h

    async def test_support_lane_announces_assist(self):
        h = await self._assist_run(4)   # 辅助位
        self.assertIn("主人助攻", h.texts, h.texts)

    async def test_non_support_lane_stays_silent(self):
        h = await self._assist_run(2)   # 中单位
        self.assertNotIn("主人助攻", h.texts, h.texts)

    async def test_aram_never_announces_assist(self):
        """大乱斗没有分路可言，不该因为"第 5 位"就当成辅助。"""
        h = await self._assist_run(4, map_number=12)
        self.assertNotIn("主人助攻", h.texts, h.texts)

    async def test_lane_inference_off_means_no_assist(self):
        """关掉分路推断时认不出辅助，助攻也不该播。"""
        h = await self._assist_run(4, {"infer_lane_positions": False})
        self.assertNotIn("主人助攻", h.texts, h.texts)

    async def test_panel_switch_can_mute_assists(self):
        h = await self._assist_run(4, {"announce": {"my_assist": False}})
        self.assertNotIn("主人助攻", h.texts, h.texts)

    async def test_owner_kill_still_wins_over_assist(self):
        """主人自己击杀时，即使名单里也写了主人，仍按击杀播。"""
        h = Harness(self.clock)
        await h.start()
        players = _roster_with_me_at(4)
        h.client.in_game = True
        h.client.data = fixtures.make_all_game_data(game_time=5.0, players=players)
        h.client.events = [fixtures.simple_event(0, "GameStart")]
        await h.poll(times=1)
        h.client.events.append(
            fixtures.kill_event(1, "我", "敌人A", assisters=["我"])
        )
        h.client.data = fixtures.make_all_game_data(game_time=40.0, players=players)
        await h.poll(times=12, dt=2.0)
        self.assertIn("主人击杀了对面的", h.texts, h.texts)
        self.assertNotIn("主人助攻", h.texts, h.texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
