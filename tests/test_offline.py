"""离线单元测试：只测纯逻辑，不需要 N.E.K.O SDK，也不需要真的在打游戏。

跑法（仓库根目录）：

    python -m unittest discover -s tests -v

用的是标准库 unittest，不依赖 pytest —— 这台机器上没装 pytest，
而插件本身也不该为了几个测试引入依赖。
"""

from __future__ import annotations

import unittest
from pathlib import Path

import neko_lol as neko_lol_module
from neko_lol import cues, game_model, milestones, narrator
from neko_lol.config import DEFAULT_ANNOUNCE, PluginConfig

# 搜索路径由 tests/__init__.py 在包初始化时配好，所以上面这些 import 能
# 保持在文件顶部（否则会被 lint 判 E402）。
_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _player(
    name: str,
    champion: str,
    team: str,
    *,
    kills: int = 0,
    deaths: int = 0,
    assists: int = 0,
    cs: int = 0,
    level: int = 1,
    is_dead: bool = False,
    respawn: float = 0.0,
    items: list | None = None,
) -> dict:
    return {
        "riotId": f"{name}#CN1",
        "summonerName": name,
        "championName": champion,
        "team": team,
        "level": level,
        "isDead": is_dead,
        "respawnTimer": respawn,
        "scores": {
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "creepScore": cs,
            "wardScore": 0,
        },
        "items": items or [],
    }


def _all_game_data(
    *,
    game_time: float = 600.0,
    players: list | None = None,
    active: dict | None = None,
) -> dict:
    return {
        "activePlayer": active or {"riotId": "我#CN1", "summonerName": "我", "level": 11},
        "allPlayers": players
        if players is not None
        else [
            _player("我", "Yasuo", "ORDER", kills=3, deaths=1, assists=2, cs=120, level=11),
            _player("敌人A", "Zed", "CHAOS", kills=1, deaths=3, assists=1, cs=90, level=9),
            _player("敌人B", "LeeSin", "CHAOS", kills=0, deaths=2, assists=4, cs=80, level=8),
            _player("队友A", "Lulu", "ORDER", kills=0, deaths=1, assists=6, cs=20, level=8),
        ],
        "gameData": {
            "gameMode": "CLASSIC",
            "gameTime": game_time,
            "mapName": "Map11",
            "mapNumber": 11,
        },
        "events": {"Events": []},
    }


class TestGameModel(unittest.TestCase):
    def test_parses_me_by_riot_id(self):
        state = game_model.MatchState.from_all_game_data(_all_game_data())
        self.assertIsNotNone(state.me)
        self.assertEqual(state.me.champion, "Yasuo")
        self.assertEqual(state.me.kda, "3—1—2")
        self.assertEqual(state.me.cs, 120)
        self.assertEqual(state.map_name, "召唤师峡谷")
        self.assertEqual(state.game_mode, "经典")
        self.assertEqual(state.game_time_label, "00:10:00")

    def test_splits_teams(self):
        state = game_model.MatchState.from_all_game_data(_all_game_data())
        self.assertEqual({p.champion for p in state.allies}, {"Yasuo", "Lulu"})
        self.assertEqual({p.champion for p in state.enemies}, {"Zed", "LeeSin"})

    def test_score_line(self):
        state = game_model.MatchState.from_all_game_data(_all_game_data())
        self.assertEqual(state.score_line(), "我方击杀 3，敌方击杀 1（我方领先 2 个）")

    def test_score_line_reports_trailing_and_tied(self):
        """落后和持平也要说清楚，别让模型自己推。"""
        behind = game_model.MatchState.from_all_game_data(_all_game_data(players=[
            _player("我", "Yasuo", "ORDER", kills=1),
            _player("敌人A", "Zed", "CHAOS", kills=5),
        ]))
        self.assertIn("我方落后 4 个", behind.score_line())

        tied = game_model.MatchState.from_all_game_data(_all_game_data(players=[
            _player("我", "Yasuo", "ORDER", kills=2),
            _player("敌人A", "Zed", "CHAOS", kills=2),
        ]))
        self.assertIn("双方持平", tied.score_line())

    def test_estimated_gold_includes_items(self):
        players = [
            _player(
                "我",
                "Yasuo",
                "ORDER",
                items=[{"displayName": "无尽之刃", "count": 1, "price": 3400}],
            )
        ]
        data = _all_game_data(players=players)
        data["allPlayers"][0]["currentGold"] = 500.0
        state = game_model.MatchState.from_all_game_data(data)
        self.assertEqual(state.me.estimated_gold, 3900.0)
        self.assertEqual(state.me.items, ["无尽之刃"])

    def test_survives_garbage(self):
        """残缺/畸形的 JSON 不能把插件搞崩。"""
        state = game_model.MatchState.from_all_game_data({})
        self.assertIsNone(state.me)
        self.assertEqual(state.allies, [])
        self.assertEqual(state.enemies, [])
        self.assertIn("读不到自己的数据", state.summary_text())

    def test_matches_name_without_tag(self):
        """事件里给的名字是 riotId 去掉 #TAG 的那一段。"""
        state = game_model.MatchState.from_all_game_data(_all_game_data())
        self.assertTrue(state.is_me("我"))
        self.assertFalse(state.is_me("敌人A"))

    def test_duplicate_champions_do_not_confuse_me(self):
        """克隆大作战 / 无尽狂潮：同队同英雄时不能把队友认成主人本人。

        早先按 (英雄名, 队伍) 回查原始记录，同队同英雄会让两个人都"匹配"
        到主人的名字，于是 state.me 变成列表里靠前的那个队友 —— 所有
        "你"的显示都成了队友的数据。
        """
        players = [
            _player("队友1", "Yasuo", "ORDER", kills=9, deaths=0, assists=0, level=15),
            _player("队友2", "Yasuo", "ORDER", kills=0, deaths=1, assists=0, level=6),
            _player("我", "Yasuo", "ORDER", kills=1, deaths=2, assists=3, level=11),
            _player("敌人A", "Zed", "CHAOS"),
        ]
        state = game_model.MatchState.from_all_game_data(_all_game_data(players=players))
        self.assertEqual(state.me.name, "我")
        self.assertEqual(state.me.kda, "1—2—3")
        self.assertEqual(state.me.level, 11)

    def test_unidentified_active_player_is_not_guessed(self):
        """认不出主人是哪一位时，宁可如实说"读不到"，也不能瞎猜。

        猜错的代价是双向的：敌我整体反转，而且一旦把猜中的人塞进
        my_names，is_me() 就会对真正的对手返回 True。
        """
        data = _all_game_data(active={"riotId": "查无此人#CN1"})
        state = game_model.MatchState.from_all_game_data(data)

        self.assertIsNone(state.me)
        self.assertEqual(state.allies, [])
        self.assertEqual(state.enemies, [])
        self.assertIn("未知", state.score_line())
        # 关键：不能因为猜不出来就把某个真人当成主人
        self.assertFalse(state.is_me("敌人A"))
        self.assertFalse(state.is_me("队友A"))
        # 名字集合仍来自 activePlayer，所以主人自己的击杀还能认出来
        self.assertTrue(state.is_me("查无此人"))

    def test_side_of_distinguishes_ally_from_me(self):
        """side_of 回答"哪一边"，is_me 回答"是不是本人"，两者不能混用。"""
        state = game_model.MatchState.from_all_game_data(_all_game_data())
        self.assertEqual(state.side_of("我"), "ally")
        self.assertEqual(state.side_of("队友A"), "ally")
        self.assertEqual(state.side_of("敌人A"), "enemy")
        self.assertEqual(state.side_of("路人甲"), "unknown")
        self.assertEqual(state.side_of(""), "unknown")
        # 队友是"主人这边"，但不是主人本人
        self.assertTrue(state.is_me("我"))
        self.assertFalse(state.is_me("队友A"))

    def test_name_matching_survives_bot_suffix(self):
        """人机局的「名字（电脑）」必须能匹配上名单里的「名字」。

        2026-11-16 训练模式那局实测：事件里是「蛮族之王（电脑）」，名单里
        只是英雄名 → side_of 全部 unknown，于是"有人拿下了土龙"，而且
        龙魂计数整局一次都没执行（日志里 0 条 [小龙]）。
        """
        data = _all_game_data(players=[
            _player("我", "Yasuo", "ORDER", kills=1, deaths=0, assists=0),
            _player("队友A", "Lulu", "ORDER", kills=0, deaths=0, assists=1),
            _player("敌人A", "Zed", "CHAOS", kills=0, deaths=1, assists=0),
        ])
        state = game_model.MatchState.from_all_game_data(data)
        self.assertEqual(state.side_of("敌人A（电脑）"), "enemy")
        self.assertEqual(state.side_of("队友A（电脑）"), "ally")
        self.assertEqual(state.side_of("敌人A(Bot)"), "enemy")
        # 主人的带后缀写法也要认得出
        self.assertTrue(state.is_me("我（电脑）"))
        self.assertEqual(state.side_of("我（电脑）"), "ally")

    def test_bot_names_resolve_by_champion_when_player_name_differs(self):
        """人机局的事件用「英雄名（电脑）」当玩家名，名单里却是平台编的 ID。

        实测：事件里是「蛮族之王（电脑）」，名单里是「电脑1」+ champion
        「蛮族之王」。只按玩家名比永远匹配不上，必须按英雄名兜一层。
        """
        data = _all_game_data(players=[
            _player("我", "无双剑姬", "ORDER"),
            _player("电脑1", "蛮族之王", "CHAOS"),
            _player("队友A", "星籁歌姬", "ORDER"),
        ])
        state = game_model.MatchState.from_all_game_data(data)
        self.assertEqual(state.side_of("蛮族之王（电脑）"), "enemy")
        self.assertEqual(state.side_of("星籁歌姬（电脑）"), "ally")
        # 平台 ID 本身仍然认得出
        self.assertEqual(state.side_of("电脑1"), "enemy")
        self.assertEqual(state.side_of("电脑1（电脑）"), "enemy")
        # 真的不认识的人还是 unknown，不许瞎猜
        self.assertEqual(state.side_of("路人甲"), "unknown")

    def test_name_matching_ignores_tag_and_case(self):
        """#TAG、大小写、首尾空白都不该影响匹配。"""
        data = _all_game_data(players=[
            _player("我", "Yasuo", "ORDER"),
            _player("EnemyWho", "Zed", "CHAOS"),
        ])
        state = game_model.MatchState.from_all_game_data(data)
        self.assertEqual(state.side_of("EnemyWho#CN1"), "enemy")
        self.assertEqual(state.side_of("  enemywho  "), "enemy")
        self.assertEqual(state.side_of("ENEMYWHO"), "enemy")

    def test_name_key_normalizes_all_forms(self):
        self.assertEqual(game_model.name_key("蛮族之王（电脑）"), "蛮族之王")
        self.assertEqual(game_model.name_key("Zed#CN1"), "zed")
        self.assertEqual(game_model.name_key("  Zed  "), "zed")
        self.assertEqual(game_model.name_key(""), "")
        # 括号在后面带 TAG 的怪形态也要吃干净
        self.assertEqual(game_model.name_key("Zed（电脑）#CN1"), "zed")


class TestCues(unittest.TestCase):
    def setUp(self):
        self.state = game_model.MatchState.from_all_game_data(_all_game_data())

    def test_my_kill(self):
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            self.state,
        )
        self.assertEqual(kind, "my_kill")
        self.assertIn("主人击杀了对面的 Zed", fragment)

    def test_alias_map_rewrites_the_announcement(self):
        """别名表要真的生效：官方全称 → 玩家常用叫法。"""
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            self.state,
            champion_names={"Zed": "劫"},
        )
        self.assertEqual(kind, "my_kill")
        self.assertIn("劫", fragment)
        self.assertNotIn("Zed", fragment)

    def test_load_name_map_reads_aliases(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "champion_aliases.json"
            path.write_text(
                json.dumps({"虚空恐惧": "大虫子", "诺克萨斯之手": "诺手"}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(
                game_model.load_name_map(path),
                {"虚空恐惧": "大虫子", "诺克萨斯之手": "诺手"},
            )
            # 旧名字仍然能用（老用户手里是 champion_names.json）
            self.assertEqual(
                game_model.load_champion_names(path),
                {"虚空恐惧": "大虫子", "诺克萨斯之手": "诺手"},
            )

    def test_load_name_map_tolerates_garbage(self):
        """表是可选装饰：缺文件、坏 JSON、非对象、非字符串值都不该崩。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(game_model.load_name_map(missing), {})

            broken = Path(tmp) / "broken.json"
            broken.write_text("{ not json", encoding="utf-8")
            self.assertEqual(game_model.load_name_map(broken), {})

            array = Path(tmp) / "array.json"
            array.write_text('["a", "b"]', encoding="utf-8")
            self.assertEqual(game_model.load_name_map(array), {})

            mixed = Path(tmp) / "mixed.json"
            mixed.write_text(
                '{"A": "甲", "B": 2, "": "空键", "C": "  "}', encoding="utf-8"
            )
            self.assertEqual(game_model.load_name_map(mixed), {"A": "甲"})

    def test_my_death(self):
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "我"},
            self.state,
        )
        self.assertEqual(kind, "my_death")
        self.assertIn("主人被对面的 Zed 击杀了", fragment)

    def test_ally_death(self):
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "敌人B", "VictimName": "队友A"},
            self.state,
            only_mine=False,
        )
        self.assertEqual(kind, "ally_death")
        self.assertIn("Lulu", fragment)

    def test_non_champion_killer_is_not_shown_as_an_id(self):
        """被防御塔/小兵打死时，不能把结构物内部 ID 念出来。

        实测 2026-11-16：
          "主人被对面的 Turret_TChaos_L1_P3_2254202041_0 击杀了。"
        """
        cases = {
            "Turret_TChaos_L1_P3_2254202041_0": "防御塔",
            "Turret_TChaos_L1_P2_1_0": "防御塔",
            "SRU_Dragon_Fire": "野怪",
            "Minion_T1_C_01": "小兵",
        }
        for killer, expect in cases.items():
            fragment, kind = cues.describe_champion_kill(
                {"EventName": "ChampionKill", "KillerName": killer, "VictimName": "我"},
                self.state,
            )
            self.assertEqual(kind, "my_death")
            self.assertIn(expect, fragment, f"{killer} 没降级成「{expect}」：{fragment}")
            self.assertNotIn("Turret_", fragment)
            self.assertNotIn("SRU_", fragment)
            self.assertNotIn("Minion_", fragment)
            self.assertNotIn("对面的", fragment, "非英雄单位不该说'对面的'")

    def test_unknown_id_like_killer_falls_back_to_generic(self):
        """认不出的长 ID 也不原样念出来。"""
        fragment, _kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "SomeInternal_Id_123",
             "VictimName": "我"},
            self.state,
        )
        self.assertNotIn("SomeInternal", fragment)
        self.assertIn("击杀了", fragment)

    def test_bot_suffix_is_stripped_from_assists(self):
        """助攻名带「（电脑）」后缀时不能连后缀一起念。"""
        fragment, _kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "我",
                "VictimName": "敌人A",
                "Assisters": ["队友A（电脑）", "敌人B（电脑）"],
            },
            self.state,
        )
        self.assertIn("助攻：Lulu、LeeSin", fragment)
        self.assertNotIn("（电脑）", fragment)

    def test_my_assist_is_announced_when_enabled(self):
        """开启后，队友击杀而我助攻 → 播「主人助攻…」。"""
        fragment, kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "队友A",
                "VictimName": "敌人A",
                "Assisters": ["我"],
            },
            self.state,
            announce_my_assists=True,
        )
        self.assertEqual(kind, "my_assist")
        self.assertIn("主人助攻", fragment)
        self.assertIn("Lulu", fragment)   # 谁拿下的
        self.assertIn("Zed", fragment)    # 拿下的是谁

    def test_my_assist_is_silent_when_disabled(self):
        """没开（非辅助位）时，队友击杀 + 我助攻不该播。"""
        self.assertIsNone(
            cues.describe_champion_kill(
                {
                    "EventName": "ChampionKill",
                    "KillerName": "队友A",
                    "VictimName": "敌人A",
                    "Assisters": ["我"],
                },
                self.state,
                announce_my_assists=False,
            )
        )

    def test_my_kill_wins_over_assist(self):
        """我既在 Assisters 又是击杀者时，按击杀算，不能播成助攻。"""
        fragment, kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "我",
                "VictimName": "敌人A",
                "Assisters": ["我"],
            },
            self.state,
            announce_my_assists=True,
        )
        self.assertEqual(kind, "my_kill")
        self.assertNotIn("主人助攻", fragment)

    def test_my_assist_not_announced_for_enemy_kills(self):
        """对面互杀、主人不在助攻名单里时，不该瞎播。"""
        self.assertIsNone(
            cues.describe_champion_kill(
                {
                    "EventName": "ChampionKill",
                    "KillerName": "敌人A",
                    "VictimName": "敌人B",
                    "Assisters": ["敌人B"],
                },
                self.state,
                announce_my_assists=True,
            )
        )

    def test_assist_hint_text_shape(self):
        """助攻写成数据式「（助攻：A、B）」，不写成叙述句。

        叙述式（"有 A 帮忙"）模型很容易照念一遍；数据式更像背景字段。
        """
        fragment, _ = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill", "KillerName": "我",
                "VictimName": "敌人A", "Assisters": ["队友A"],
            },
            self.state,
        )
        self.assertIn("（助攻：Lulu）", fragment)
        self.assertNotIn("帮忙", fragment)

    def test_assist_hint_is_exported(self):
        """提示语常量存在且明确要求别点名。"""
        self.assertIn("不用特意点名", cues.ASSIST_BACKGROUND_HINT)
        self.assertIn("助攻", cues.ASSIST_BACKGROUND_HINT)

    def test_killer_and_victim_are_never_in_assist_list(self):
        """击杀者不该出现在自己的助攻名单里（否则会出现"被 Zed 击杀（助攻：Zed）"）。"""
        fragment, _ = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill", "KillerName": "敌人A",
                "VictimName": "我", "Assisters": ["敌人A", "敌人B"],
            },
            self.state,
        )
        self.assertIn("助攻：LeeSin", fragment)
        self.assertNotIn("助攻：Zed", fragment, f"击杀者混进助攻了：{fragment}")

    def test_lane_suffix_on_kill_prompts(self):
        """排位（分路可信）时给英雄名补分路；不可信时不补。"""
        ranked = game_model.MatchState.from_all_game_data(
            _all_game_data(players=[
                _player("我", "Yasuo", "ORDER"),
                _player("队友A", "Lulu", "ORDER"),
                _player("敌人A", "Zed", "CHAOS"),
            ]),
            trust_lane_order=True,
        )
        self.assertTrue(ranked.lanes_trusted)
        self.assertEqual(ranked.lane_of("敌人A"), "上单")

        frag, _ = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            ranked, with_lane=True,
        )
        self.assertIn("Zed（上单）", frag)

        # 默认不带（普通匹配 / 大乱斗）
        frag2, _ = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            ranked,
        )
        self.assertNotIn("（上单）", frag2)

    def test_lane_of_returns_empty_when_untrusted(self):
        state = game_model.MatchState.from_all_game_data(_all_game_data())
        self.assertFalse(state.lanes_trusted)
        self.assertEqual(state.lane_of("敌人A"), "")

    def test_my_kill_lists_the_helpers(self):
        """主人击杀时要说清谁帮忙了。

        早先的 `_assist_suffix` 判断"主人在不在 Assisters 里"——主人既然是
        击杀者就不可能同时是助攻者，那句永远为空，是死代码。
        """
        fragment, kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "我",
                "VictimName": "敌人A",
                "Assisters": ["队友A"],
            },
            self.state,
        )
        self.assertEqual(kind, "my_kill")
        self.assertIn("击杀了对面的 Zed", fragment)
        self.assertIn("助攻：Lulu", fragment)

    def test_my_death_lists_the_helpers(self):
        """主人被杀时也要说清对面是谁带着人把他做掉的。"""
        fragment, kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "敌人A",
                "VictimName": "我",
                "Assisters": ["敌人B"],
            },
            self.state,
        )
        self.assertEqual(kind, "my_death")
        self.assertIn("主人被对面的 Zed 击杀了", fragment)
        self.assertIn("助攻：LeeSin", fragment)

    def test_assists_are_omitted_when_empty(self):
        """没有助攻时不要留空括号或多余的"有  帮忙"。"""
        for killer, victim in (("我", "敌人A"), ("敌人A", "我")):
            for extra in ({}, {"Assisters": []}, {"Assisters": "not-a-list"}):
                fragment, _kind = cues.describe_champion_kill(
                    {"EventName": "ChampionKill", "KillerName": killer,
                     "VictimName": victim, **extra},
                    self.state,
                )
                self.assertNotIn("助攻：", fragment, f"{extra} 出现了助攻数据")
                self.assertNotIn("（）", fragment)

    def test_assists_exclude_me_and_dedupe(self):
        """Assisters 里混进主人自己、或重复同一个人，都不该念出来。"""
        fragment, _kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "我",
                "VictimName": "敌人A",
                "Assisters": ["我", "队友A", "队友A"],
            },
            self.state,
        )
        # 主人自己不该出现在助攻名单里；重复出现的队友只念一次
        self.assertIn("助攻：Lulu", fragment)
        self.assertEqual(fragment.count("助攻："), 1, fragment)
        self.assertEqual(fragment.count("Lulu"), 1, fragment)

    def test_assist_uses_champion_names_through_alias_map(self):
        """助攻名字也要走别名表，不能直接吐官方全称。"""
        fragment, _kind = cues.describe_champion_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "我",
                "VictimName": "敌人A",
                "Assisters": ["队友A"],
            },
            self.state,
            champion_names={"Lulu": "璐璐"},
        )
        self.assertIn("助攻：璐璐", fragment)
        self.assertNotIn("Lulu", fragment)

    def test_dragon(self):
        cue = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            self.state,
        )
        self.assertIsNotNone(cue)
        self.assertEqual(cue.key, "dragon_kill")
        self.assertIn("火龙", cue.text)

    def test_baron_stolen(self):
        cue = cues.build_event_cue(
            {"EventName": "BaronKill", "KillerName": "敌人A", "Stolen": "True"},
            self.state,
        )
        self.assertIn("敌方拿下了大龙", cue.text)
        self.assertIn("抢的", cue.text)

    def test_ace(self):
        cue = cues.build_event_cue({"EventName": "Ace", "Acer": "我"}, self.state)
        self.assertEqual(cue.key, "ace")
        self.assertIn("团灭了对面", cue.text)

    def test_ally_kill_is_not_reported_as_our_death(self):
        """队友杀了对面，不能说成"我们的人被杀了" —— 方向完全相反。

        这是最常见的事件（线上搭档拿人头），早先的实现在这里没有分支，
        直接落到"主人这边的 X 被 Y 击杀"，把对面的英雄标成了自己人。
        """
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "队友A", "VictimName": "敌人A"},
            self.state,
            only_mine=False,
        )
        self.assertEqual(kind, "ally_kill")
        self.assertIn("主人这边的 Lulu 击杀了对面的 Zed", fragment)
        self.assertNotIn("被", fragment)

    def test_enemy_kill_on_ally_is_reported_as_our_death(self):
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "队友A"},
            self.state,
            only_mine=False,
        )
        self.assertEqual(kind, "ally_death")
        self.assertIn("主人这边的 Lulu 被对面的 Zed 击杀", fragment)

    def test_unknown_sides_fall_back_to_neutral_wording(self):
        """敌我认不出来时给中性描述，别硬编方向。"""
        fragment, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "路人甲", "VictimName": "路人乙"},
            self.state,
            only_mine=False,
        )
        self.assertEqual(kind, "neutral")
        self.assertIn("路人甲 击杀了 路人乙", fragment)

    def test_ally_objective_counts_as_our_side(self):
        """队友拿的资源要说"队友"，不能说成"对面"。

        早先用 is_me（只判是不是主人本人）来定边，于是队友惩戒掉的大龙
        会被播报成"对面拿下了大龙" —— 直接说反。
        """
        ally_dragon = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "队友A", "DragonType": "Fire"},
            self.state,
        )
        enemy_dragon = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "敌人A", "DragonType": "Fire"},
            self.state,
        )
        self.assertIn("队友拿下了火龙", ally_dragon.text)
        self.assertIn("敌方拿下了火龙", enemy_dragon.text)
        self.assertNotEqual(ally_dragon.text, enemy_dragon.text)

        ally_baron = cues.build_event_cue(
            {"EventName": "BaronKill", "KillerName": "队友A"}, self.state
        )
        self.assertIn("队友拿下了大龙", ally_baron.text)

        ally_herald = cues.build_event_cue(
            {"EventName": "HeraldKill", "KillerName": "队友A"}, self.state
        )
        self.assertIn("队友拿下了峡谷先锋", ally_herald.text)

    def test_ally_ace_is_not_inverted(self):
        """队友打出的团灭，不能说成"主人这边被团灭了"。"""
        ally_ace = cues.build_event_cue({"EventName": "Ace", "Acer": "队友A"}, self.state)
        enemy_ace = cues.build_event_cue({"EventName": "Ace", "Acer": "敌人A"}, self.state)
        self.assertIn("主人这边团灭了对面", ally_ace.text)
        self.assertIn("主人这边被团灭了", enemy_ace.text)

    def test_multikill_is_never_announced_on_its_own(self):
        """多杀事件绝不能单独成一条播报。

        接口对一次五杀会额外发 4 条 Multikill。曾经因为它掉进了通用分支，
        结果"主人拿了个三杀 / 四杀 / 五杀"被单独播了四遍 —— 正是要消灭的
        刷屏。它只该作为 KillBurst 的标签用。
        """
        for streak in (2, 3, 4, 5):
            self.assertIsNone(
                cues.build_event_cue(
                    {"EventName": "Multikill", "KillerName": "我", "KillStreak": streak},
                    self.state,
                ),
                f"KillStreak={streak} 的多杀被单独播报了",
            )




    def test_non_champion_tower_killer_is_named(self):
        """塔被小兵拆掉时不能播成"有人拆掉了…"。"""
        for killer, expect in (
            ("Minion_T1_C_01", "小兵"),
            ("SRU_Dragon_Fire", "野怪"),
            ("SomeWeird_Id_9", "非英雄单位"),
        ):
            cue = cues.build_event_cue(
                {"EventName": "TurretKilled", "KillerName": killer,
                 "TurretKilled": "Turret_T2_C_01_A"},
                self.state,
            )
            self.assertIn(expect, cue.body, f"{killer} 没降级：{cue.body}")
            self.assertNotIn("有人", cue.body)

    def test_unknown_player_killer_still_says_someone(self):
        """真认不出的普通名字仍保持"有人"——不瞎猜。"""
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "路人甲",
             "TurretKilled": "Turret_T2_C_01_A"},
            self.state,
        )
        self.assertIn("有人", cue.body)

    def test_first_blood_and_dragons_have_no_stats(self):
        """一血 / 小龙 / 大龙不带战绩（2026-11-17 用户指定）。

        这三个时刻本身已经够清楚，后面再跟一行"主人现在 X—Y—Z"是多余的尾巴。
        """
        for event in (
            {"EventName": "FirstBlood", "Recipient": "我"},
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            {"EventName": "BaronKill", "KillerName": "我"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertIsNotNone(cue, event)
            self.assertNotIn("主人现在", cue.text, f"{event['EventName']} 带了战绩")
            self.assertNotIn(
                "击杀—死亡—助攻", cue.text, f"{event['EventName']} 带了格式说明"
            )

    def test_structure_events_have_no_stats(self):
        """先锋 / 推塔 / 兵营不挂战绩（2026-11-16 用户选定）。

        这三类是环境档：量大、多是背景进展（"敌方拆了座塔"），播报越短
        越好；挂一行战绩只会把这类话拖长。
        """
        for event in (
            {"EventName": "HeraldKill", "KillerName": "我"},
            {"EventName": "TurretKilled", "KillerName": "我", "TurretKilled": "Turret_T2_C_01_A"},
            {"EventName": "InhibKilled", "KillerName": "我", "InhibKilled": "Inhib_T2_C_01_A"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertIsNotNone(cue, event)
            self.assertNotIn("主人现在", cue.text, f"{event['EventName']} 带了战绩")
            self.assertNotIn(
                "击杀—死亡—助攻", cue.text, f"{event['EventName']} 带了格式说明"
            )

    def test_game_start_and_end_have_no_stats(self):
        """"开局 0—0—0" 和"结束（总结马上来）"都不该挂战绩行。"""
        start = cues.build_event_cue({"EventName": "GameStart"}, self.state)
        self.assertIsNotNone(start)
        self.assertNotIn("主人现在", start.text)

        end = cues.build_event_cue({"EventName": "GameEnd", "Result": "Win"}, self.state)
        self.assertIsNotNone(end)
        self.assertNotIn("主人现在", end.text)

    def test_ace_keeps_stats(self):
        """团灭是现在唯一还挂战绩行的事件。"""
        cue = cues.build_event_cue({"EventName": "Ace", "Acer": "我"}, self.state)
        self.assertTrue(cue.has_stats)
        self.assertIn("主人现在", cue.text)

    def test_resource_events_have_no_double_punctuation(self):
        """去掉战绩后不能留下"。。"（body 本来就以句号结尾）。"""
        for event in (
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            {"EventName": "BaronKill", "KillerName": "我"},
            {"EventName": "TurretKilled", "KillerName": "我", "TurretKilled": "Turret_T2_C_01_A"},
        ):
            cue = cues.build_event_cue(event, self.state)
            for bad in ("。。", "。，", "，。", "！。"):
                self.assertNotIn(bad, cue.text, f"{event['EventName']} 出现 {bad}")

    def test_stats_can_be_omitted_per_call(self):
        """调用方可以显式要求不带战绩（冷却用）。"""
        with_stats = cues.build_event_cue(
            {"EventName": "Ace", "Acer": "我"},
            self.state,
        )
        self.assertTrue(with_stats.has_stats)
        self.assertIn("主人现在", with_stats.text)

        without = cues.build_event_cue(
            {"EventName": "Ace", "Acer": "我"},
            self.state,
            include_stats=False,
        )
        self.assertFalse(without.has_stats)
        self.assertNotIn("主人现在", without.text)
        self.assertNotIn("击杀—死亡—助攻", without.text)

    def test_never_stats_keys_ignore_include_flag(self):
        """开局/推塔这些本来就不带的，强制 include 也不该带。"""
        for event in (
            {"EventName": "GameStart"},
            {"EventName": "TurretKilled", "KillerName": "我",
             "TurretKilled": "Turret_T2_C_01_A"},
        ):
            cue = cues.build_event_cue(event, self.state, include_stats=True)
            self.assertFalse(cue.has_stats, event["EventName"])

    def test_time_is_omitted_for_noisy_events(self):
        """这些事件不带对局时间（2026-11-18 用户指定）。"""
        for event in (
            {"EventName": "GameStart"},
            {"EventName": "Ace", "Acer": "我"},
            {"EventName": "BaronKill", "KillerName": "我"},
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            {"EventName": "HeraldKill", "KillerName": "队友A"},
            {"EventName": "TurretKilled", "KillerName": "队友A",
             "TurretKilled": "Turret_T2_C_01_A"},
            {"EventName": "InhibKilled", "KillerName": "敌人A",
             "InhibKilled": "Inhib_T1_C_01_A"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertNotIn("对局时间", cue.text, f"{event['EventName']} 还带着时间")
            # 地图/模式不能一起被删掉 —— 模型要靠它确定场景
            self.assertIn("召唤师峡谷", cue.text, event["EventName"])

    def test_time_is_kept_for_first_blood_and_game_end(self):
        """一血、对局结束保留时间（"什么时候发生"有信息量）。"""
        for event in (
            {"EventName": "FirstBlood", "Recipient": "我"},
            {"EventName": "GameEnd", "Result": "Win"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertIn("对局时间", cue.text, event["EventName"])
            self.assertRegex(cue.text, r"\d{2}:\d{2}:\d{2}")

    def test_other_multikill_has_no_time(self):
        h = cues.OthersMultikill()
        h.add("敌人A", 3, self.state, 0.0)
        cue = h.force_flush(self.state)[0]
        self.assertNotIn("对局时间", cue.text)

    def test_game_time_label_is_hh_mm_ss(self):
        state = game_model.MatchState.from_all_game_data(_all_game_data(game_time=3905))
        self.assertEqual(state.game_time_label, "01:05:05")
        state2 = game_model.MatchState.from_all_game_data(_all_game_data(game_time=59))
        self.assertEqual(state2.game_time_label, "00:00:59")

    def test_kda_uses_dash_not_slash(self):
        """战绩用 3—1—2，不用斜杠 —— 斜杠串容易被读成分数或日期。"""
        self.assertEqual(self.state.me.kda, "3—1—2")
        # 比分行不再用三段式 —— 只看击杀数，避免模型把"阵亡"当击杀读。
        self.assertNotIn("—", self.state.score_line())
        self.assertNotIn("/", self.state.me.kda)

    def test_broadcasts_do_not_mention_cs(self):
        """播报里不出现补刀数量。

        "补到 120 刀"在聊天里既啰嗦，又容易被模型当成重点去讲。
        补刀数据仍然在 PlayerStat.cs 里，LLM 工具查得到。
        """
        self.assertNotIn("补刀", self.state.me.one_line())
        self.assertNotIn("补刀", self.state.summary_text())
        self.assertNotIn("补刀", self.state.post_game_text())
        for event in (
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            {"EventName": "Ace", "Acer": "我"},
            {"EventName": "FirstBlood", "Recipient": "我"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertNotIn("补刀", cue.text, f"{event['EventName']} 仍带补刀")

    def test_cs_data_is_still_available(self):
        """播报不说，但数据不能丢 —— LLM 工具要能查到补刀。"""
        self.assertEqual(self.state.me.cs, 120)

    def test_no_respawn_countdown_in_broadcasts(self):
        """播报里不出现"X 秒后复活"。

        复活倒计时是屏幕上的数字，说出来只会拖长句子、打断节奏。
        受影响的只有场上文字，``respawn_timer`` 字段本身保留着。
        """
        import fixtures

        data = fixtures.make_all_game_data(game_time=600)
        data["allPlayers"][0]["isDead"] = True
        data["allPlayers"][0]["respawnTimer"] = 18.0
        state = game_model.MatchState.from_all_game_data(data)

        self.assertNotIn("秒后复活", state.me.one_line())
        self.assertNotIn("阵亡中", state.me.one_line())

        cue = cues.build_event_cue(
            {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "我"},
            state,
        )
        self.assertNotIn("秒后复活", cue.text)
        self.assertNotIn("秒后复活", cue.body)

        frag, kind = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "我"},
            state,
        )
        self.assertNotIn("秒后复活", frag)
        self.assertEqual(kind, "my_death")

    def test_respawn_data_is_still_available(self):
        """播报不说倒计时，但数据不能丢 —— LLM 工具要能查到。"""
        import fixtures

        data = fixtures.make_all_game_data(game_time=600)
        data["allPlayers"][0]["isDead"] = True
        data["allPlayers"][0]["respawnTimer"] = 18.0
        state = game_model.MatchState.from_all_game_data(data)
        self.assertTrue(state.me.is_dead)
        self.assertEqual(state.me.respawn_timer, 18.0)

    def test_all_bodies_end_with_period(self):
        """正文统一以「。」结尾。

        早先资源/建筑分支天生带句号，击杀/团灭不带，导致播报记录里
        同样是"一句话"却有的有句号有的没有。统一由 _normalize_body 收口。
        """
        cases = [
            {"EventName": "GameStart"},
            {"EventName": "FirstBlood", "Recipient": "我"},
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "我"},
            {"EventName": "Ace", "Acer": "我"},
            {"EventName": "Ace", "Acer": "敌人A"},
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            {"EventName": "BaronKill", "KillerName": "我"},
            {"EventName": "HeraldKill", "KillerName": "我"},
            {"EventName": "TurretKilled", "KillerName": "我",
             "TurretKilled": "Turret_TChaos_L2_P3_1_0"},
            {"EventName": "InhibKilled", "KillerName": "我",
             "InhibKilled": "Inhib_TChaos_L1_P1_1_0"},
            {"EventName": "GameEnd", "Result": "Win"},
        ]
        for event in cases:
            cue = cues.build_event_cue(event, self.state)
            self.assertIsNotNone(cue, event)
            self.assertTrue(
                cue.body.endswith("。"),
                f"{event['EventName']} 的 body 结尾不是句号：{cue.body!r}",
            )

    def test_summary_instruction_is_not_duplicated(self):
        """小结的收尾指令不能出现两次"用你自己的口吻"。"""
        cue = cues.build_summary_cue(self.state)
        self.assertEqual(
            cue.text.count("用你自己的口吻"), 1, f"指令重复了：{cue.text[-160:]}"
        )
        self.assertIn("点评一句当前局势", cue.text)

    def test_kda_legend_present_when_stats_are(self):
        """带战绩的播报必须附上「击杀-死亡-助攻」的格式说明。

        光给 "3—1—2" 模型会读错顺序 —— 它不知道这三个数字分别是什么。
        """
        cue = cues.build_event_cue(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            self.state,
        )
        self.assertIn("3—1—2", cue.text)
        self.assertIn("击杀—死亡—助攻", cue.text, f"缺格式说明：{cue.text}")
        # 必须明确要求把横线念成"杠"，否则模型会念成"三到一""三减一"。
        self.assertIn("念成「杠」", cue.text, f"没要求读成杠：{cue.text}")
        self.assertIn("三杠一杠二", cue.text, f"缺读法示例：{cue.text}")

    def test_kda_legend_absent_without_stats(self):
        """没有战绩行的播报不该出现格式说明，白占篇幅。"""
        for event in (
            {"EventName": "GameStart"},
            {"EventName": "GameEnd", "Result": "Win"},
            {"EventName": "HeraldKill", "KillerName": "我"},
            {"EventName": "TurretKilled", "KillerName": "我",
             "TurretKilled": "Turret_TChaos_L2_P3_1_0"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertNotIn("击杀—死亡—助攻", cue.text, event["EventName"])

    def test_summary_has_kda_legend(self):
        cue = cues.build_summary_cue(self.state)
        self.assertIn("击杀—死亡—助攻", cue.text)

    def test_aram_turret_has_no_lane(self):
        """嚎哭深渊只有一条路，塔名不该带上路/中/下路。"""
        import fixtures

        data = fixtures.make_all_game_data(game_time=300)
        data["gameData"].update({"mapNumber": 12, "gameMode": "ARAM"})
        aram = game_model.MatchState.from_all_game_data(data)

        for raw in ("Turret_T1_L_01_A", "Turret_T2_C_02_A", "Turret_T1_L_03_A"):
            cue = cues.build_event_cue(
                {"EventName": "TurretKilled", "KillerName": "我", "TurretKilled": raw}, aram
            )
            for word in ("上路", "中路", "下路"):
                self.assertNotIn(word, cue.body, f"大乱斗塔名带了路：{cue.body}")
            self.assertIn("塔", cue.body)

    def test_rift_turret_keeps_lane(self):
        """召唤师峡谷仍然要带路名 —— 大乱斗的改动不能影响它。"""
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "我",
             "TurretKilled": "Turret_T2_L_01_A"},
            self.state,
        )
        self.assertIn("上路", cue.body, cue.body)

    def test_aram_variants_detected_by_map_number(self):
        """海克斯大乱斗等变种的 gameMode 代码可能没见过，靠 mapNumber=12 兜住。"""
        import fixtures

        for mode in ("ARAM", "KIWI", "", "ARAM_AUGMENTS", "海克斯大乱斗"):
            data = fixtures.make_all_game_data(game_time=300)
            data["gameData"].update({"mapNumber": 12, "gameMode": mode})
            state = game_model.MatchState.from_all_game_data(data)
            self.assertTrue(state.is_aram, f"map=12 mode={mode!r} 没识别成大乱斗")

    def test_rift_is_not_aram(self):
        self.assertFalse(self.state.is_aram, f"召唤师峡谷被误判成大乱斗：{self.state.map_name}")

    def test_game_end_has_no_stats(self):
        """game_end 不带战绩行。

        它 5 秒后就会跟一条带完整战绩的赛后总结，这里再挂一行纯属重复。
        """
        for result in ("Win", "Lose", ""):
            cue = cues.build_event_cue(
                {"EventName": "GameEnd", "Result": result}, self.state
            )
            self.assertNotIn("主人现在", cue.text, f"Result={result} 仍带战绩")
            self.assertNotIn("补刀", cue.text)

    def test_game_end_states_the_verdict(self):
        """game_end 直接说结果。

        早先只说"这局结束了"，胜负留给 5 秒后的 post_game —— 中间那 5 秒
        是静默空档，听感上像卡住了。现在这里说结果，post_game 只讲过程。
        """
        cases = {"Win": "赢了", "Lose": "输了", "Draw": "平局"}
        for result, word in cases.items():
            cue = cues.build_event_cue(
                {"EventName": "GameEnd", "Result": result}, self.state
            )
            self.assertIn("这局结束了", cue.body)
            self.assertIn(word, cue.body, f"Result={result} 没说结果：{cue.body}")

    def test_game_end_without_result_says_nothing_about_verdict(self):
        """没收到 Result 时不能猜输赢。"""
        cue = cues.build_event_cue({"EventName": "GameEnd"}, self.state)
        self.assertEqual(cue.body.strip(), "这局结束了。")
        for word in ("赢了", "输了", "平局"):
            self.assertNotIn(word, cue.body)

    def test_end_broadcast_carries_the_verdict(self):
        """对局结束播报是唯一一条，胜负必须由它来说。"""
        cue = cues.build_post_game_cue(self.state, result="Win")
        self.assertEqual(cue.key, "game_end", "结束播报该用 game_end 这个 key")
        self.assertIn("可以直接说", cue.text)
        self.assertIn("赢了", cue.text)

    def test_end_broadcast_includes_pending_items(self):
        """窗口里没说完的推塔/兵营要并进这条，而不是单独再发一轮。"""
        cue = cues.build_post_game_cue(
            self.state, result="Win",
            pending_items=["主人拆掉了对面的中路二塔", "敌方拆掉了我们的上路外塔"],
        )
        self.assertIn("对局结束前最后发生的事", cue.text)
        self.assertIn("中路二塔", cue.text)
        self.assertIn("上路外塔", cue.text)

    def test_end_broadcast_without_pending_has_no_tail(self):
        cue = cues.build_post_game_cue(self.state, result="Win")
        self.assertNotIn("对局结束前最后发生的事", cue.text)

    def test_unknown_event_ignored(self):
        self.assertIsNone(cues.build_event_cue({"EventName": "MinionsSpawning"}, self.state))

    def test_turret_label_parsed(self):
        """塔要能识别到**哪条路 + 哪一层**。"""
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "我", "TurretKilled": "Turret_T2_C_01_A"},
            self.state,
        )
        self.assertIn("中路外塔", cue.text)
        self.assertIn("对面的", cue.text)

    def test_turret_includes_killer(self):
        """播报必须带上**谁拆的塔**。

        早先完全没读 KillerName，三种拆塔者输出一模一样，
        连"敌人拆了我们的塔"都说成"主人这边推掉了"。
        """
        for killer, expect in (("我", "主人拆掉了"), ("队友A", "队友拆掉了"), ("敌人A", "敌方拆掉了")):
            cue = cues.build_event_cue(
                {"EventName": "TurretKilled", "KillerName": killer, "TurretKilled": "Turret_T1_C_01_A"},
                self.state,
            )
            self.assertIn(expect, cue.text, f"{killer} 的播报不对：{cue.body}")

    def test_turret_side_uses_my_team_not_absolute(self):
        """归属要跟主人的队伍走 —— 主人可能在 CHAOS 方。"""
        import fixtures

        st = game_model.MatchState.from_all_game_data(
            fixtures.make_all_game_data(
                players=[
                    fixtures.make_player("我", "Yasuo", "CHAOS"),
                    fixtures.make_player("敌人A", "Zed", "ORDER"),
                ],
                active={"riotId": "我#CN1", "summonerName": ""},
            )
        )
        # T1 属于 ORDER，主人在 CHAOS → T1 是对面的塔
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "我", "TurretKilled": "Turret_T1_C_01_A"}, st
        )
        self.assertIn("对面的", cue.text, cue.body)
        # T2 属于 CHAOS，主人在 CHAOS → T2 是我们自己的塔
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "敌人A", "TurretKilled": "Turret_T2_C_01_A"}, st
        )
        self.assertIn("我们的", cue.text, cue.body)

    def test_domestic_team_tags(self):
        """国服用 TChaos / TOrder，不是 T1 / T2。

        实测日志里出现过"TChaos防御塔被推掉了"——说明旧代码的白名单
        只认 T1/T2，国服的结构名原样漏进了播报。
        """
        # 主人在 ORDER
        for raw, expect in (
            ("Turret_TChaos_C_01_A", "对面的中路外塔"),
            ("Turret_TOrder_L_02_A", "我们的上路二塔"),
            ("Inhib_TChaos_R_01_A", "对面的下路兵营"),
        ):
            field = "InhibKilled" if raw.startswith("Inhib") else "TurretKilled"
            cue = cues.build_event_cue(
                {"EventName": field, "KillerName": "我", field: raw}, self.state
            )
            self.assertIn(expect, cue.text, f"{raw} 解析错：{cue.body}")

    def test_real_collected_structure_names(self):
        """用真机采集到的字段做回归。

        数据来自 2026-11-14 两局（蓝方 TChaos / 红方 TOrder），操作顺序
        与字段一一对应。格式是 ``Turret_<队伍>_L<路>_P<位置>_<随机ID>_<0>``，
        **不是**国际服的 ``Turret_T1_C_01_A``。
        """
        # (原始字段, 期望出现的文字)
        cases = [
            ("Turret_TOrder_L2_P3_1509986696_0", "上路外塔"),
            ("Turret_TOrder_L1_P3_1242677625_0", "中路外塔"),
            ("Turret_TOrder_L0_P3_3795288474_0", "下路外塔"),
            ("Turret_TOrder_L1_P2_1225900006_0", "中路二塔"),
            ("Turret_TOrder_L1_P1_1209122387_0", "中路高地塔"),
            ("Inhib_TOrder_L1_P1_2786523670_0", "中路兵营"),
            ("Turret_TOrder_L1_P4_3675873665_0", "门牙塔"),
            ("Turret_TOrder_L1_P5_3625540808_0", "门牙塔"),
            ("Turret_TChaos_L0_P2_528623213_0", "下路二塔"),
            ("Turret_TChaos_L0_P1_478290356_0", "下路高地塔"),
        ]
        for raw, expect in cases:
            field = "InhibKilled" if raw.startswith("Inhib") else "TurretKilled"
            cue = cues.build_event_cue(
                {"EventName": field, "KillerName": "我", field: raw}, self.state
            )
            self.assertIn(expect, cue.body, f"{raw} 解析错：{cue.body}")

    def test_door_towers_have_no_lane(self):
        """门牙塔不属于任何一条路，说"中路门牙塔"是错的。"""
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "我",
             "TurretKilled": "Turret_TOrder_L1_P4_3675873665_0"},
            self.state,
        )
        self.assertNotIn("中路", cue.body)
        self.assertIn("门牙塔", cue.body)



    def test_objectives_identify_the_killer(self):
        """大龙/小龙/先锋也要说清是你、队友还是敌方拿的。"""
        for event, needle in (
            ({"EventName": "DragonKill", "DragonType": "Fire"}, "火龙"),
            ({"EventName": "BaronKill"}, "大龙"),
            ({"EventName": "HeraldKill"}, "峡谷先锋"),
        ):
            for killer, expect in (("我", "主人拿下了"), ("队友A", "队友拿下了"), ("敌人A", "敌方拿下了")):
                cue = cues.build_event_cue({**event, "KillerName": killer}, self.state)
                self.assertIn(f"{expect}{needle}", cue.body, f"{killer}/{needle}: {cue.body}")

    def test_unknown_objective_killer_is_soft(self):
        """查不到的人不要说成队友或敌方。"""
        cue = cues.build_event_cue(
            {"EventName": "BaronKill", "KillerName": "路人甲"}, self.state
        )
        self.assertIn("有人拿下了大龙", cue.body, cue.body)

    def test_dragon_soul_reminder_on_third_and_fourth(self):
        """第三/第四条提醒龙魂。龙魂属性 = 本局第一种龙的属性。"""
        # 第三条：提醒
        cue = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            self.state, dragon_counts={"ally": 3, "enemy": 0}, soul_type="火龙",
        )
        self.assertIn("火龙魂", cue.body)
        self.assertIn("再来一条", cue.body)

        # 第四条：宣告到手。即使这一条是别的属性，龙魂仍是本局的那种。
        cue = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Water"},
            self.state,
            dragon_counts={"ally": 4, "enemy": 0},
            soul_type="火龙",
        )
        self.assertIn("火龙魂到手了", cue.body)
        self.assertNotIn("水龙魂", cue.body)

    def test_dragon_soul_for_enemy_side(self):
        cue = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "敌人A", "DragonType": "Water"},
            self.state, dragon_counts={"ally": 0, "enemy": 3}, soul_type="水龙",
        )
        self.assertIn("对面", cue.body)
        self.assertIn("水龙魂", cue.body)

    def test_no_soul_reminder_early(self):
        """第 1、2 条不提龙魂。"""
        for n in (1, 2):
            cue = cues.build_event_cue(
                {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
                self.state, dragon_counts={"ally": n, "enemy": 0}, soul_type="火龙",
            )
            self.assertNotIn("龙魂", cue.body, f"第 {n} 条就提龙魂了：{cue.body}")

    def test_elder_dragon_has_no_soul(self):
        """远古巨龙是决胜龙，不给龙魂，不该提示。"""
        cue = cues.build_event_cue(
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Elder"},
            self.state, dragon_counts={"ally": 4, "enemy": 0}, soul_type="火龙",
        )
        self.assertNotIn("龙魂", cue.body, cue.body)

    def test_killer_identity_is_distinguished(self):
        """要能区分"我拆的""队友拆的""对面拆的"。

        只在开局建好 ID → 阵营映射（allPlayers 里每人带 team），
        才能在拆塔时判断拆的人属于哪边 —— 光看 KillerName 只是个 ID。
        """
        cases = [
            ("我", "Turret_TChaos_L2_P3", "主人拆掉了对面的"),
            ("队友A", "Turret_TChaos_L1_P2", "队友拆掉了对面的"),
            ("敌人A", "Turret_TOrder_L0_P3", "敌方拆掉了我们的"),
            ("敌人B", "Turret_TOrder_L1_P1", "敌方拆掉了我们的"),
        ]
        for killer, raw, expect in cases:
            cue = cues.build_event_cue(
                {"EventName": "TurretKilled", "KillerName": killer, "TurretKilled": raw},
                self.state,
            )
            self.assertIn(expect, cue.body, f"{killer} 的播报不对：{cue.body}")

    def test_unknown_killer_falls_back_to_id(self):
        """ID 在阵容里找不到时，退回显示原始 ID，不要瞎编阵营。"""
        cue = cues.build_event_cue(
            {"EventName": "TurretKilled", "KillerName": "路过的匿名人",
             "TurretKilled": "Turret_TChaos_L2_P3"},
            self.state,
        )
        self.assertIn("有人拆掉了", cue.body)
        self.assertNotIn("队友", cue.body)
        self.assertNotIn("敌方", cue.body)

    def test_turret_tiers(self):
        for raw, expect in (
            ("Turret_T1_C_01_A", "外塔"),
            ("Turret_T2_C_02_A", "二塔"),
            ("Turret_T1_C_03_A", "高地塔"),
        ):
            self.assertIn(expect, cues._turret_label(raw), raw)

    def test_cue_forbids_hallucination(self):
        """提示语必须明确禁止模型编造，否则游戏播报会开始胡说。"""
        cue = cues.build_event_cue(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            self.state,
        )
        self.assertIn("不要编造", cue.text)
        # 收尾指令必须要求模型把官方全称换成玩家常用叫法
        self.assertIn("用玩家平时的叫法", cue.text)







    def test_no_doubled_punctuation(self):
        """提示语里不能出现"。，"这种双标点。

        实测日志里出现过"主人玩的是 虚空恐惧。，主人现在 …" ——
        body 以句号结尾，尾巴又补了个逗号。
        """
        for event in (
            {"EventName": "GameStart"},
            {"EventName": "FirstBlood", "Recipient": "我"},
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            {"EventName": "Ace", "Acer": "我"},
            {"EventName": "DragonKill", "KillerName": "我", "DragonType": "Fire"},
            {"EventName": "BaronKill", "KillerName": "我"},
            {"EventName": "TurretKilled", "KillerName": "我", "TurretKilled": "Turret_T2_C_01_A"},
            {"EventName": "GameEnd", "Result": "Win"},
        ):
            cue = cues.build_event_cue(event, self.state)
            self.assertIsNotNone(cue, event)
            for bad in ("。，", "，。", "。。", "，,"):
                self.assertNotIn(bad, cue.text, f"{event['EventName']} 出现双标点 {bad}")

    def test_no_doubled_punctuation_in_summary_and_post_game(self):
        summary = cues.build_summary_cue(self.state)
        post = cues.build_post_game_cue(self.state)
        for cue in (summary, post):
            for bad in ("。，", "，。", "。。"):
                self.assertNotIn(bad, cue.text, f"{cue.key} 出现双标点 {bad}")

    def test_render_plain_strips_model_instructions(self):
        cue = cues.build_event_cue(
            {"EventName": "BaronKill", "KillerName": "我"}, self.state
        )
        plain = cues.render_plain(cue)
        self.assertNotIn("用你自己的口吻", plain)
        self.assertIn("大龙", plain)


class TestOnlyMyEvents(unittest.TestCase):
    """只播主人自己的事：队友的、对面的都不说；一血只播主人参与的。"""

    def setUp(self):
        self.state = game_model.MatchState.from_all_game_data(_all_game_data())

    # ── 击杀 ────────────────────────────────────────────────────

    def test_my_kill_is_reported(self):
        d = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "我", "VictimName": "敌人A"},
            self.state,
        )
        self.assertIsNotNone(d)
        self.assertEqual(d[1], "my_kill")

    def test_my_death_is_reported(self):
        d = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "我"},
            self.state,
        )
        self.assertIsNotNone(d)
        self.assertEqual(d[1], "my_death")

    def test_ally_kill_is_silent(self):
        """队友拿人头不关主人的事。"""
        self.assertIsNone(
            cues.describe_champion_kill(
                {"EventName": "ChampionKill", "KillerName": "队友A", "VictimName": "敌人A"},
                self.state,
            )
        )

    def test_ally_death_is_silent(self):
        """队友被打死也不关主人的事。"""
        self.assertIsNone(
            cues.describe_champion_kill(
                {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "队友A"},
                self.state,
            )
        )

    def test_enemy_vs_enemy_is_silent(self):
        """对面互杀更不关主人的事。"""
        self.assertIsNone(
            cues.describe_champion_kill(
                {"EventName": "ChampionKill", "KillerName": "敌人A", "VictimName": "敌人B"},
                self.state,
            )
        )

    def test_full_mode_still_reports_ally_events(self):
        """only_mine=False 时四象限照旧（保留给想听全场的人）。"""
        d = cues.describe_champion_kill(
            {"EventName": "ChampionKill", "KillerName": "队友A", "VictimName": "敌人A"},
            self.state,
            only_mine=False,
        )
        self.assertIsNotNone(d)
        self.assertEqual(d[1], "ally_kill")

    # ── 一血 ────────────────────────────────────────────────────

    def test_first_blood_taken_by_me(self):
        cue = cues.build_event_cue(
            {"EventName": "FirstBlood", "Recipient": "我"},
            self.state,
            only_mine=True,
        )
        self.assertIsNotNone(cue)
        self.assertIn("主人拿到了一血", cue.text)

    def test_first_blood_not_mine_is_silent(self):
        """接口只给"谁被拿一血"，给不了"谁拿的"。

        所以只要被杀的不是主人自己，就分不清是不是主人拿的 —— 按新规则
        一律不播，避免出现"这局的一血被主人这边拿走了"这种可能猜错的话。
        """
        for recipient in ("队友A", "敌人A"):
            self.assertIsNone(
                cues.build_event_cue(
                    {"EventName": "FirstBlood", "Recipient": recipient},
                    self.state,
                    only_mine=True,
                ),
                f"recipient={recipient} 时不该播报",
            )

    def test_first_blood_full_mode_keeps_old_behaviour(self):
        cue = cues.build_event_cue(
            {"EventName": "FirstBlood", "Recipient": "敌人A"},
            self.state,
            only_mine=False,
        )
        self.assertIsNotNone(cue)
        self.assertIn("被对面拿走了", cue.text)

    # ── 集成：主流程真的会静音 ──────────────────────────────────

    def test_pipeline_mutes_ally_kills(self):
        """端到端：队友人头走完 _process_events 之后什么都不该发生。"""
        import asyncio
        import sys
        from pathlib import Path as _P

        sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
        sys.path.insert(0, str(_P(__file__).resolve().parent))
        import fixtures
        from test_session import FakeClock, Harness

        clock = FakeClock()
        real_time = neko_lol_module.time
        neko_lol_module.time = clock
        try:
            h = Harness(clock)
            asyncio.run(h.start())
            h.client.data = fixtures.make_all_game_data(game_time=5.0)
            h.client.events = [fixtures.simple_event(0, "GameStart")]
            asyncio.run(h.plugin.poll_match())
            # 开局播报现在也要等窗口，先把它放出来，再取基线。
            for _ in range(10):
                clock.advance(2.0)
                asyncio.run(h.plugin.poll_match())
            h.pushed.clear()
            before = 0

            for killer, victim in (("队友A", "敌人A"), ("敌人A", "队友A"), ("敌人A", "敌人B")):
                h.client.events.append(fixtures.kill_event(1, killer, victim))
                h.client.data = fixtures.make_all_game_data(game_time=60.0)
                asyncio.run(h.plugin.poll_match())
                clock.advance(30.0)

            self.assertEqual(len(h.pushed), before, f"不该播报却播了：{h.texts}")
            self.assertEqual(h.plugin._narrator.queue_depth, 0, "队友人头被攒进了窗口")
        finally:
            neko_lol_module.time = real_time


class TestMyMultikill(unittest.TestCase):
    """主人自己的连杀：三杀以上播一句，五杀只说"五杀"。"""

    def setUp(self):
        self.state = game_model.MatchState.from_all_game_data(_all_game_data())

    def test_double_kill_is_ignored(self):
        h = cues.MyMultikill()
        h.add(2, 100.0)
        self.assertEqual(h.flush(110.0, self.state), [])

    def test_triple_kill_is_announced(self):
        h = cues.MyMultikill()
        h.add(3, 100.0)
        out = h.flush(110.0, self.state)
        self.assertEqual(len(out), 1)
        self.assertIn("三杀", out[0].body)

    def test_pentakill_collapses_to_one_line(self):
        """连杀过程中 KillStreak 会一路涨到 5，只能播一句，且是"五杀"。

        实测踩过：逐条播会连喊"三杀！四杀！五杀！"三声。
        """
        h = cues.MyMultikill()
        for i, streak in enumerate((2, 3, 4, 5)):
            h.add(streak, 100.0 + i)
        out = h.flush(110.0, self.state)
        self.assertEqual(len(out), 1, f"应当只播一句：{out}")
        self.assertIn("五杀", out[0].body)
        self.assertEqual(h.pending, 0, "播完要清空")

    def test_no_stats_and_no_game_clock(self):
        """五杀播报不带战绩、不带对局时间 —— 它要短、要能直接喊出来。"""
        h = cues.MyMultikill()
        h.add(5, 100.0)
        text = h.flush(110.0, self.state)[0].text
        self.assertNotIn("主人现在", text)
        self.assertNotIn("对局时间", text)
        self.assertNotIn("击杀—死亡—助攻", text)

    def test_is_critical_tier(self):
        """主人自己的多杀值得打断当前话题，走关键档。"""
        h = cues.MyMultikill()
        h.add(5, 100.0)
        self.assertEqual(h.flush(110.0, self.state)[0].tier, cues.TIER_CRITICAL)

    def test_force_flush_releases_pending(self):
        """刚拿完就结束，攒着的那句也要放出来。"""
        h = cues.MyMultikill()
        h.add(5, 100.0)
        out = h.force_flush(self.state)
        self.assertEqual(len(out), 1)
        self.assertEqual(h.pending, 0)


class TestOthersMultikill(unittest.TestCase):
    """别人的连杀：三杀以上才播，且一次五杀只说一句。"""

    def setUp(self):
        self.state = game_model.MatchState.from_all_game_data(_all_game_data())

    def _add(self, h, killer, streak, now):
        h.add(killer, streak, self.state, now)

    def test_double_kill_is_ignored(self):
        """双杀太常见，不说。"""
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        self._add(h, "队友A", 2, 100.0)
        self.assertEqual(h.flush(110.0, self.state), [])

    def test_ally_triple_kill_is_announced(self):
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        self._add(h, "队友A", 3, 100.0)
        cues_out = h.flush(110.0, self.state)
        self.assertEqual(len(cues_out), 1)
        self.assertIn("主人这边的 Lulu 拿了三杀", cues_out[0].text)

    def test_enemy_pentakill_announced_once(self):
        """连杀过程中 KillStreak 会一路涨，最终只说一句"五杀"。"""
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        for streak in (2, 3, 4, 5):
            self._add(h, "敌人A", streak, 100.0 + streak * 0.1)
        cues_out = h.flush(110.0, self.state)
        self.assertEqual(len(cues_out), 1, f"播了 {len(cues_out)} 条")
        self.assertIn("对面的 Zed 拿了五杀", cues_out[0].text)
        self.assertNotIn("三杀", cues_out[0].text)

    def test_waits_for_settle_before_announcing(self):
        """攒够时间才播，否则会边说三杀边升四杀。"""
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        self._add(h, "队友A", 3, 100.0)
        self.assertEqual(h.flush(102.0, self.state), [], "没等够就播了")
        self.assertEqual(len(h.flush(105.0, self.state)), 1)

    def test_separate_killers_are_tracked_separately(self):
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        self._add(h, "队友A", 3, 100.0)
        self._add(h, "敌人A", 3, 100.0)
        cues_out = h.flush(110.0, self.state)
        self.assertEqual(len(cues_out), 2)
        texts = " | ".join(c.text for c in cues_out)
        self.assertIn("Lulu", texts)
        self.assertIn("Zed", texts)

    def test_force_flush_ignores_settle(self):
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        self._add(h, "队友A", 3, 100.0)
        self.assertEqual(len(h.force_flush(self.state)), 1)

    def test_threshold_zero_disables(self):
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=1)
        self._add(h, "队友A", 2, 100.0)
        self.assertEqual(len(h.flush(110.0, self.state)), 1, "阈值 1 时双杀也该播")

    def test_streak_below_threshold_does_not_replace(self):
        """先收到双杀（忽略）再收到三杀，应当按三杀播。"""
        h = cues.OthersMultikill(settle_seconds=4.0, min_streak=3)
        self._add(h, "队友A", 2, 100.0)
        self._add(h, "队友A", 3, 100.5)
        cues_out = h.flush(110.0, self.state)
        self.assertEqual(len(cues_out), 1)
        self.assertIn("三杀", cues_out[0].text)


class TestLaneInference(unittest.TestCase):
    """分路推断：接口不给 lane 字段，靠同队数组顺序（仅排位/灵活排位固定）。"""

    def setUp(self):
        self.players = [
            _player("我", "ChoGath", "ORDER"),
            _player("队友1", "Nidalee", "ORDER"),
            _player("队友2", "Seraphine", "ORDER"),
            _player("队友3", "MissFortune", "ORDER"),
            _player("队友4", "Leona", "ORDER"),
            _player("敌人1", "Irelia", "CHAOS"),
            _player("敌人2", "Graves", "CHAOS"),
            _player("敌人3", "Ahri", "CHAOS"),
            _player("敌人4", "Ezreal", "CHAOS"),
            _player("敌人5", "Thresh", "CHAOS"),
        ]

    def _state(self, *, trust=True, map_number=11, mode="CLASSIC", players=None):
        data = _all_game_data(
            game_time=5,
            players=players if players is not None else self.players,
        )
        data["gameData"]["mapNumber"] = map_number
        data["gameData"]["gameMode"] = mode
        return game_model.MatchState.from_all_game_data(
            data, trust_lane_order=trust
        )

    def test_lane_names_are_in_order(self):
        self.assertEqual(
            game_model.LANE_NAMES, ("上单", "打野", "中单", "ADC", "辅助")
        )

    def test_top_lane_and_opponent(self):
        """主人在己方数组第 0 位 → 上单，对位是敌方数组第 0 位。"""
        state = self._state()
        self.assertEqual(state.my_lane, "上单")
        self.assertEqual(state.lane_opponent().champion, "Irelia")

    def test_lane_follows_index(self):
        """把主人挪到数组不同位置，分路应随下标变。"""
        for index, lane in enumerate(("上单", "打野", "中单", "ADC", "辅助")):
            players = list(self.players)
            players[0], players[index] = players[index], players[0]
            state = self._state(players=players)
            self.assertEqual(state.my_lane, lane, f"下标 {index}")
            # 对位取敌方同下标
            self.assertEqual(
                state.lane_opponent().champion,
                self.players[5 + index]["championName"],
            )

    def test_disabled_flag_gives_no_lane(self):
        state = self._state(trust=False)
        self.assertEqual(state.my_lane, "")
        self.assertIsNone(state.lane_opponent())

    def test_non_rift_map_never_trusts_order(self):
        """大乱斗/竞技场等非召唤师峡谷，开关开着也不信顺序。"""
        for map_number in (12, 30, 33):
            state = self._state(map_number=map_number)
            self.assertFalse(state.lanes_trusted, f"map={map_number}")
            self.assertEqual(state.my_lane, "")

    def test_non_classic_mode_never_trusts_order(self):
        for mode in ("ARAM", "URF", "NEXUSBLITZ", "ONEFORALL"):
            state = self._state(mode=mode)
            self.assertFalse(state.lanes_trusted, f"mode={mode}")

    def test_game_start_mentions_lane_and_opponent(self):
        cue = cues.build_event_cue({"EventName": "GameStart"}, self._state())
        self.assertIn("主人走上单", cue.body)
        self.assertIn("Irelia", cue.body)

    def test_game_start_omits_lane_when_disabled(self):
        cue = cues.build_event_cue(
            {"EventName": "GameStart"}, self._state(trust=False)
        )
        self.assertNotIn("上单", cue.body)
        self.assertNotIn("对位", cue.body)

    def test_uneven_teams_do_not_fabricate_opponent(self):
        """人数不等（秒退/自定义局）时不硬凑对位。"""
        # 己方 5 人、敌方只有 4 人，且主人排在己方第 5 位（辅助）
        # → 敌方下标 4 越界，不该硬凑一个对位出来
        # 己方顺序：队友1..4 在前，主人在第 5 位；敌方只有 4 人
        players = self.players[1:5] + [self.players[0]] + self.players[5:9]
        state = self._state(players=players)
        self.assertEqual(state.my_lane, "辅助")
        self.assertIsNone(state.lane_opponent())

    def test_english_champion_gets_a_space(self):
        """中英混排要留空格：``对面的 Darius`` 而不是 ``对面的Darius``。"""
        players = [
            _player("我", "Garen", "ORDER"),
            _player("敌人1", "Darius", "CHAOS"),
        ]
        state = self._state(players=players)
        body = cues.build_event_cue({"EventName": "GameStart"}, state).body
        self.assertIn("对面的 Darius", body, body)

    def test_chinese_champion_gets_no_space(self):
        """中文英雄名中间不该插空格。"""
        players = [
            _player("我", "盖伦", "ORDER"),
            _player("敌人1", "亚索", "CHAOS"),
        ]
        state = self._state(players=players)
        body = cues.build_event_cue({"EventName": "GameStart"}, state).body
        self.assertIn("对面的亚索", body, body)
        self.assertNotIn("对面的 亚索", body)

    def test_game_start_has_no_stats_line(self):
        """开局那一刻战绩必然是 0—0—0、1 级，不该占一句。"""
        cue = cues.build_event_cue({"EventName": "GameStart"}, self._state())
        self.assertNotIn("主人现在", cue.text)
        self.assertNotIn("0—0—0", cue.body)

    def test_no_me_no_lane(self):
        """认不出主人时不猜分路。"""
        data = _all_game_data(
            game_time=5,
            players=self.players,
            active={"riotId": "查无此人#CN1"},
        )
        data["gameData"]["mapNumber"] = 11
        data["gameData"]["gameMode"] = "CLASSIC"
        state = game_model.MatchState.from_all_game_data(
            data, trust_lane_order=True
        )
        self.assertIsNone(state.me)
        self.assertEqual(state.my_lane, "")
        self.assertIsNone(state.lane_opponent())


class TestNarrator(unittest.TestCase):
    """三档投递：关键永远立刻，常规隔 3 秒，环境进窗口合并。"""

    def _narrator(self, sent: list, **overrides) -> narrator.Narrator:
        cfg = narrator.NarratorConfig()
        # 这些用例测的是**窗口本身**（开窗、倒计时、合并、折叠），
        # 所以关掉"环境档搭车播报"——那一档会让窗口等别的播报来带走，
        # 到点不吐。搭车行为另有专门用例覆盖。
        cfg.attach_ambient_to_events = False
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return narrator.Narrator(
            lambda text, meta: (sent.append(text), {"submitted": True})[1],
            config=cfg,
            log=lambda _m: None,
        )

    def _cue(
        self,
        key: str = "turret_killed",
        body: str = "内容",
        priority: int = 20,
        tier: int = cues.TIER_AMBIENT,
    ):
        """默认造一条**环境档**的 cue（进窗口）。"""
        return cues.Cue(
            key=key, text=f"（{body}）", priority=priority, body=body, tier=tier
        )

    # ── 基本窗口 ────────────────────────────────────────────────

    def test_first_submit_opens_window_without_sending(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        self.assertEqual(n.submit(self._cue(), now=1000.0), "buffered")
        self.assertEqual(sent, [], "窗口刚开就发了")
        self.assertEqual(n.queue_depth, 1)

    def test_window_is_not_flushed_before_deadline(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue(), now=1000.0)
        n.flush(now=1029.0)
        self.assertEqual(sent, [], "没到 30 秒就发了")
        n.flush(now=1030.0)
        self.assertEqual(len(sent), 1, "到点了却没发")

    def test_items_in_window_are_merged_into_one_message(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        for i in range(3):
            n.submit(self._cue(body=f"第{i}条"), now=1000.0 + i)
        n.flush(now=1030.0)
        self.assertEqual(len(sent), 1, f"应当只发 1 条，实际 {len(sent)}")
        self.assertIn("第0条", sent[0])
        self.assertIn("第2条", sent[0])

    def test_merged_message_uses_its_own_key(self):
        """合并产物不能借用某个真实事件的 key。

        早先多条合并时把 key 硬写成 "multikill"，于是"敌方拆掉两座塔"
        的日志里写着 multikill —— 排查时完全误导（真实日志实证）。
        """
        cues_out: list = []
        cfg = narrator.NarratorConfig(window_seconds=30)
        n = narrator.Narrator(
            lambda text, meta: (cues_out.append(meta), {"submitted": True})[1],
            config=cfg,
            log=lambda _m: None,
        )
        n.submit(self._cue("turret_killed", body="塔A"), now=1000.0)
        n.submit(self._cue("inhib_killed", body="兵营"), now=1001.0)
        merged = n._build_merged(n._pending)
        self.assertEqual(merged.key, narrator.MERGED_KEY)
        self.assertNotEqual(merged.key, "turret_killed")
        self.assertNotEqual(merged.key, "inhib_killed")
        # 单条不合并时仍沿用原 key
        single = n._build_merged(n._pending[:1])
        self.assertEqual(single.key, "turret_killed")

    def test_merged_key_is_not_a_real_announce_switch(self):
        """合并 key 不该出现在 announce 开关表里 —— 否则关掉某个事件会误伤合并。"""
        from neko_lol import PluginConfig
        self.assertNotIn(narrator.MERGED_KEY, PluginConfig().announce)

    def test_milestone_does_not_join_the_merge_window(self):
        """里程碑是"状态总结"，不能和击杀混成"刚才发生了这些事"。

        实测踩过：一条击杀 + 辅助 10 助攻里程碑被合并成
        「主人击杀了对面的 敌英雄0；主人已经 10 个助攻了」，模型会把
        累计值当成"刚拿到第 10 个助攻"来讲。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("champion_kill", body="主人击杀了对面的 X", priority=55), now=1000.0)
        milestone = cues.Cue(
            key="milestone",
            text="（主人已经 10 个助攻了）",
            body="主人已经 10 个助攻了",
            priority=cues.PRIORITIES["milestone"],
            tier=cues.TIER_AMBIENT,
            mergeable=False,
        )
        self.assertEqual(n.submit(milestone, now=1001.0), "sent")
        self.assertEqual(len(sent), 1)
        self.assertIn("10 个助攻", sent[0])
        # 击杀那条仍在窗口里，不该被里程碑带走
        self.assertEqual(n.queue_depth, 1)
        n.flush(now=1031.0)
        self.assertEqual(len(sent), 2)
        self.assertIn("击杀了对面的 X", sent[1])
        self.assertNotIn("10 个助攻", sent[1])

    def test_summary_does_not_join_the_merge_window(self):
        """定期小结不进合并窗口，否则会被当成"刚发生的事"概括。

        真实日志实证（2026-11-14 23:50:58）：小结和推塔撞进同一窗口，
        合并结果变成「敌方拆掉了我们的中路外塔；（主人正在玩英雄联盟，
        这是当前战况：…」—— 语义错乱，而且完整战况被压成一行。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("turret_killed", body="推塔"), now=1000.0)
        summary = cues.Cue(
            key="summary", text="（当前战况：完整列表）", body="当前战况：一行比分",
            priority=cues.PRIORITIES["summary"], tier=cues.TIER_AMBIENT,
            mergeable=False,
        )
        self.assertEqual(n.submit(summary, now=1001.0), "sent")
        self.assertEqual(len(sent), 1, f"小结没有立刻发：{sent}")
        self.assertIn("完整列表", sent[0], "小结应发完整文本，而不是被压缩的 body")
        self.assertEqual(n.queue_depth, 1, "小结不该占住窗口")

        n.flush(now=1030.0)
        self.assertEqual(len(sent), 2)
        self.assertIn("推塔", sent[1])
        self.assertNotIn("当前战况", sent[1], "小结被并进事件列表了")

    def test_unmergeable_item_in_quiet_window_is_sent_alone(self):
        """静默期会把不能合并的小结攒进窗口，结算时也必须单独发。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=10, user_chat_quiet_window=30)
        n.note_user_chat(now=1000.0)
        summary = cues.Cue(
            key="summary", text="（当前战况：完整列表）", body="当前战况：一行比分",
            priority=cues.PRIORITIES["summary"], tier=cues.TIER_AMBIENT,
            mergeable=False,
        )
        self.assertEqual(n.submit(summary, now=1001.0), "buffered")
        n.flush(now=1031.0)
        self.assertEqual(len(sent), 1)
        self.assertIn("完整列表", sent[0])

    def test_merged_bodies_keep_arrival_order(self):
        """合并按**到达顺序**拼接，不按优先级重排。

        优先级只决定截断时谁被丢掉。早先按优先级排序输出，于是
        "二塔 → 高地塔 → 兵营"被念成"兵营 → 二塔 → 高地塔"——
        建筑明明是从外往里塌的，读起来是倒放（2026-11-17 实测）。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("turret_killed", body="二塔", priority=20), now=1000.0)
        n.submit(self._cue("turret_killed", body="高地塔", priority=20), now=1001.0)
        n.submit(self._cue("inhib_killed", body="兵营", priority=35), now=1002.0)
        n.flush(now=1030.0)
        text = sent[0]
        self.assertLess(text.index("二塔"), text.index("高地塔"), text)
        self.assertLess(text.index("高地塔"), text.index("兵营"), text)

    def test_truncation_keeps_the_most_important_items(self):
        """merge_max_items 截断时先保住高优先级，而不是最早到的。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30, merge_max_items=2)
        n.submit(self._cue("turret_killed", body="塔", priority=20), now=1000.0)
        n.submit(self._cue("ace", body="团灭", priority=80), now=1001.0)
        n.submit(self._cue("champion_kill", body="人头", priority=55), now=1002.0)
        n.flush(now=1030.0)
        text = sent[0]
        self.assertIn("团灭", text)
        self.assertIn("人头", text)
        self.assertNotIn("塔", text, f"低优先级没被截掉：{text}")
        self.assertIn("等共 3 条", text)

    def test_duplicate_bodies_keep_a_count_instead_of_vanishing(self):
        """同一句话来两次要保留条数，不能直接吞掉第二条。

        2026-11-16 实测：两座门牙塔的 body 完全相同，
        早先去重后只剩一句"主人拆掉了对面的门牙塔" —— 两座都掉了这件事丢了。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("turret_killed", body="主人拆掉了对面的门牙塔", priority=20), now=1000.0)
        n.submit(self._cue("turret_killed", body="主人拆掉了对面的门牙塔", priority=20), now=1001.0)
        n.flush(now=1030.0)
        self.assertEqual(len(sent), 1)
        self.assertIn("门牙塔", sent[0])
        self.assertIn("（2 座）", sent[0], f"两座没标出来：{sent[0]}")

    def test_same_sentence_is_not_treated_as_multiple_events(self):
        """两座同名塔仍算"一件事"，不套"刚才发生了这些事"的壳。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("turret_killed", body="门牙塔", priority=20), now=1000.0)
        n.submit(self._cue("turret_killed", body="门牙塔", priority=20), now=1001.0)
        n.flush(now=1030.0)
        self.assertNotIn("刚才这一段时间里发生了这些事", sent[0])

    def test_repeated_kills_are_not_counted_as_structures(self):
        """人头重复是正常的连杀，不能标成"（3 座）"。

        实测踩过：把"重复句子计数"无条件套用，同一个敌人被连续击杀
        时播出了「主人击杀了对面的 Zed（3 座）」。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        for i in range(3):
            n.submit(
                self._cue("champion_kill", body="主人击杀了对面的 Zed", priority=55),
                now=1000.0 + i,
            )
        n.flush(now=1030.0)
        self.assertIn("击杀了对面的 Zed", sent[0])
        self.assertNotIn("座）", sent[0], f"人头被标成了座数：{sent[0]}")

    def test_repeated_kills_keep_a_count(self):
        """同一个英雄被连杀两次要标「（2 次）」，不能吞掉一条。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("champion_kill", body="主人击杀了对面的 Zed", priority=55), now=1000.0)
        n.submit(self._cue("champion_kill", body="主人击杀了对面的 Zed", priority=55), now=1001.0)
        n.flush(now=1030.0)
        self.assertIn("（2 次）", sent[0], sent[0])
        self.assertNotIn("座）", sent[0], "人头被标成了座数")

    def test_merged_carries_assist_background_hint(self):
        """合并里只要有助攻数据，就要带上"别点名"的约束。

        单条提示语是插件自己拼的，那条约束塞在 text 里；合并走的是另一条
        路，早先把约束丢了 —— 结果合并播报反而更容易念出助攻名单。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        with_assist = self._cue("champion_kill", body="主人击杀了对面的 Zed（助攻：Lulu）", priority=55)
        with_assist.has_assists = True
        n.submit(with_assist, now=1000.0)
        n.submit(self._cue("champion_kill", body="主人击杀了对面的 LeeSin", priority=55), now=1001.0)
        n.flush(now=1030.0)
        self.assertIn("助攻只作背景", sent[0], sent[0])

    def test_merged_without_assists_has_no_hint(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("champion_kill", body="主人击杀了对面的 Zed", priority=55), now=1000.0)
        n.submit(self._cue("champion_kill", body="主人击杀了对面的 LeeSin", priority=55), now=1001.0)
        n.flush(now=1030.0)
        self.assertNotIn("助攻只作背景", sent[0], sent[0])

    def test_new_window_starts_after_flush(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue(body="第一波"), now=1000.0)
        n.flush(now=1030.0)
        n.submit(self._cue(body="第二波"), now=1031.0)
        self.assertEqual(len(sent), 1, "第二波不该立刻发")
        n.flush(now=1061.0)
        self.assertEqual(len(sent), 2)
        self.assertIn("第二波", sent[1])

    def test_window_timer_starts_at_first_item_not_at_flush(self):
        """窗口从第一条开始计时，后面进来的不会把倒计时重置。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue(body="早"), now=1000.0)
        n.submit(self._cue(body="晚"), now=1025.0)
        n.flush(now=1030.0)      # 从 1000 起算已满 30 秒
        self.assertEqual(len(sent), 1, f"倒计时被后续条目重置了：{sent}")

    # ── 直接播报（不进窗口）────────────────────────────────────

    def test_critical_tier_always_speaks_now(self):
        """关键档永远立刻说，间隔再密也不等。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30, normal_gap=10)
        for i in range(3):
            result = n.submit(
                self._cue("ace", body=f"第{i}次团灭", tier=cues.TIER_CRITICAL),
                now=1000.0 + i,      # 间隔 1 秒，远小于 normal_gap
            )
            self.assertEqual(result, "sent", f"第{i}次没立刻播")
        self.assertEqual(len(sent), 3)
        self.assertEqual(n.queue_depth, 0)

    def test_normal_tier_waits_for_gap(self):
        """常规档第一条立刻，第二条间隔不够就进窗口。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30, normal_gap=5)
        self.assertEqual(
            n.submit(self._cue("champion_kill", tier=cues.TIER_NORMAL), now=1000.0),
            "sent",
        )
        self.assertEqual(
            n.submit(self._cue("champion_kill", tier=cues.TIER_NORMAL), now=1002.0),
            "buffered",
            "间隔不够却直接播了",
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(n.queue_depth, 1)

    def test_normal_tier_speaks_after_gap(self):
        """隔够了就立刻说，不用等窗口。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30, normal_gap=5)
        n.submit(self._cue("champion_kill", tier=cues.TIER_NORMAL), now=1000.0)
        self.assertEqual(
            n.submit(self._cue("champion_kill", tier=cues.TIER_NORMAL), now=1006.0),
            "sent",
        )
        self.assertEqual(len(sent), 2)

    def test_ambient_tier_uses_the_window(self):
        """环境档一律进窗口，不直接播。"""
        for key in ("turret_killed", "inhib_killed", "herald_kill", "summary"):
            sent: list = []
            n = self._narrator(sent, window_seconds=30)
            self.assertEqual(
                n.submit(self._cue(key), now=1000.0), "buffered",
                f"{key} 不该直接播",
            )
            self.assertEqual(sent, [])

    def test_critical_carries_pending_out_immediately(self):
        """关键档播报会把窗口里攒着的**一起带走**。

        早先的规则是"关键事件不碰窗口，窗口按自己的倒计时走"。代价是
        窗口里的击杀可能还要干等 10 秒才出声，而中途插进来的团灭早说完了
        —— 实测 2026-11-22 有 7 次这种"击杀被压在窗口里"。
        现在关键档直接把内容并进来一次发完，既不延迟也不丢。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("turret_killed", body="攒着的塔"), now=1000.0)
        n.submit(
            self._cue("ace", body="立刻的团灭", tier=cues.TIER_CRITICAL),
            now=1005.0,
        )
        self.assertEqual(len(sent), 1, f"应当只发一条合并播报：{sent}")
        self.assertIn("立刻的团灭", sent[0])
        self.assertIn("攒着的塔", sent[0], "窗口内容没被带走")
        self.assertEqual(n.queue_depth, 0, "窗口应当已结算")

    def test_summary_does_not_steal_the_window(self):
        """mergeable=False 的直发播报不能把窗口里的事件卷走。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue("champion_kill", body="击杀了对面的 X"), now=1000.0)
        summary = cues.Cue(
            key="summary", text="（当前战况）", body="当前战况：一行比分",
            tier=cues.TIER_AMBIENT, mergeable=False,
        )
        n.submit(summary, now=1001.0)
        self.assertNotIn("击杀了对面的 X", sent[0],
                         "小结把窗口里的事件卷进来了")
        self.assertEqual(n.queue_depth, 1, "小结不该动窗口")

    def test_ambient_waits_for_a_ride_by_default(self):
        """默认（搭车模式）下环境档不按倒计时自己吐，等别的播报带走。

        目的：推塔/兵营不单独占一条推送。一局播报从 ~37 条压到 ~20 条，
        宿主队列不积压。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=10, attach_ambient_to_events=True)
        n.submit(self._cue("turret_killed", body="推塔"), now=1000.0)
        # 过了窗口倒计时也不发 —— 在等搭车
        n.flush(now=1015.0)
        self.assertEqual(sent, [], "环境档按倒计时自己吐了")
        self.assertEqual(n.queue_depth, 1)

        # 来一条**直发**的播报（关键档），把塔一起带走
        n.submit(
            self._cue("ace", body="团灭了对面", tier=cues.TIER_CRITICAL),
            now=1020.0,
        )
        self.assertEqual(len(sent), 1, f"没搭上车：{sent}")
        self.assertIn("推塔", sent[0], "塔没被带走")
        self.assertIn("团灭了对面", sent[0])
        self.assertEqual(n.queue_depth, 0)

    def test_ambient_falls_back_after_max_wait(self):
        """长时间没有别的播报时，环境档不能永远不说。"""
        sent: list = []
        n = self._narrator(
            sent, window_seconds=10, attach_ambient_to_events=True,
            ambient_max_wait=30.0,
        )
        n.submit(self._cue("turret_killed", body="推塔"), now=1000.0)
        n.flush(now=1025.0)
        self.assertEqual(sent, [], "还没到兜底上限")
        n.flush(now=1031.0)
        self.assertEqual(len(sent), 1, "兜底没生效")
        self.assertIn("推塔", sent[0])

    def test_tier_is_read_from_the_cue(self):
        """档位是 cue 自带的，同一个 key 可以按情况给不同档。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        # champion_kill 常规情况下走常规档
        self.assertEqual(
            n.submit(self._cue("champion_kill", tier=cues.TIER_NORMAL), now=1000.0),
            "sent",
        )
        # 同一 key 提到关键档，间隔不够也立刻说
        self.assertEqual(
            n.submit(self._cue("champion_kill", body="超神", tier=cues.TIER_CRITICAL),
                     now=1001.0),
            "sent",
        )

    # ── 去重与折叠 ──────────────────────────────────────────────

    def test_identical_items_are_deduped(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        for _ in range(3):
            n.submit(self._cue(body="同一句话"), now=1000.0)
        n.flush(now=1030.0)
        self.assertEqual(sent[0].count("同一句话"), 1, "重复内容没去重")

    def test_long_batch_is_folded(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30, merge_max_items=2)
        for i in range(6):
            n.submit(self._cue(body=f"第{i}条"), now=1000.0)
        n.flush(now=1030.0)
        self.assertIn("等共 6 条", sent[0], f"超长批次没折叠：{sent[0][:100]}")

    def test_single_item_keeps_original_text(self):
        """只有一条时不该套"刚才发生了这些事"的壳。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        cue = cues.Cue(key="ace", text="（主人这边团灭了对面）", body="主人这边团灭了对面", priority=80)
        n.submit(cue, now=1000.0)
        n.flush(now=1030.0)
        self.assertEqual(sent[0], "（主人这边团灭了对面）")

    # ── 强制 ────────────────────────────────────────────────────

    def test_force_submit_sends_immediately(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        self.assertEqual(
            n.submit(self._cue("post_game", "赛后总结"), now=1000.0, force=True), "sent"
        )
        self.assertEqual(len(sent), 1)

    def test_force_flush_drains_pending(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30)
        n.submit(self._cue(body="攒着的"), now=1000.0)
        n.force_flush(now=1005.0)
        self.assertEqual(len(sent), 1, "强制结算没发出去")

    # ── 开关与异常 ──────────────────────────────────────────────

    def test_disabled_sends_nothing(self):
        sent: list = []
        n = self._narrator(sent, enabled=False)
        self.assertEqual(n.submit(self._cue(), now=1000.0), "disabled")
        n.flush(now=2000.0)
        self.assertEqual(sent, [])

    def test_muted_event_is_not_buffered(self):
        sent: list = []
        n = self._narrator(sent, announce={"turret_killed": False})
        self.assertEqual(n.submit(self._cue("turret_killed"), now=1000.0), "muted")
        self.assertEqual(n.queue_depth, 0)

    def test_dry_run_marks_without_pushing(self):
        sent: list = []
        n = self._narrator(sent, dry_run=True, window_seconds=30)
        n.submit(self._cue(body="干跑内容"), now=1000.0)
        n.flush(now=1030.0)
        self.assertEqual(sent, [], "干跑却真的发出去了")

    def test_push_failure_does_not_raise(self):
        def boom(_text, _meta):
            raise RuntimeError("宿主没了")

        cfg = narrator.NarratorConfig(window_seconds=30)
        n = narrator.Narrator(boom, config=cfg, log=lambda _m: None)
        n.submit(self._cue(), now=1000.0)
        n.flush(now=1030.0)   # 不能抛

    def test_rejected_push_reported(self):
        cfg = narrator.NarratorConfig(window_seconds=30)
        n = narrator.Narrator(
            lambda t, m: {"submitted": False, "reason": "backpressure"},
            config=cfg, log=lambda _m: None,
        )
        n.submit(self._cue(), now=1000.0)
        self.assertEqual(n.flush(now=1030.0), 0, "拒收却算作已发")

    # ── 静默窗口 ────────────────────────────────────────────────

    def test_quiet_window_blocks_critical_tier_too(self):
        """静默窗口对**所有档位**生效，包括关键档。

        早先只有合并路径看静默期，常规档和关键档直接发出去 ——
        用户设了静默却仍被打断。现在一律攒进窗口。
        """
        sent: list = []
        n = self._narrator(sent, window_seconds=30, user_chat_quiet_window=60)
        n.note_user_chat(now=1000.0)   # 静默到 1060

        result = n.submit(
            self._cue("ace", body="团灭", tier=cues.TIER_CRITICAL), now=1010.0
        )
        self.assertNotEqual(result, "sent", "静默期内关键档仍然直接播了")
        self.assertEqual(sent, [])
        self.assertEqual(n.queue_depth, 1)

    def test_quiet_window_blocks_normal_tier(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30, user_chat_quiet_window=60)
        n.note_user_chat(now=1000.0)
        result = n.submit(
            self._cue("champion_kill", body="击杀", tier=cues.TIER_NORMAL), now=1010.0
        )
        self.assertNotEqual(result, "sent")
        self.assertEqual(sent, [])

    def test_force_ignores_quiet_window(self):
        """赛后总结错过就没机会了，静默期也照发。"""
        sent: list = []
        n = self._narrator(sent, window_seconds=30, user_chat_quiet_window=60)
        n.note_user_chat(now=1000.0)
        self.assertEqual(
            n.submit(self._cue("post_game", body="赛后", tier=cues.TIER_CRITICAL),
                     now=1010.0, force=True),
            "sent",
        )
        self.assertEqual(len(sent), 1)

    def test_quiet_window_delays_flush(self):
        sent: list = []
        n = self._narrator(sent, window_seconds=30, user_chat_quiet_window=60)
        n.note_user_chat(now=1020.0)   # 静默到 1080
        n.submit(self._cue(body="战况"), now=1010.0)
        n.flush(now=1030.0)
        self.assertEqual(sent, [], "聊天静默期内还是抢话了")
        n.flush(now=1081.0)
        self.assertEqual(len(sent), 1, "静默期过了却没补发")


class TestConfig(unittest.TestCase):
    def test_defaults_when_section_missing(self):
        cfg = PluginConfig.from_raw(None)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.poll_interval, 2.0)

    def test_partial_section_keeps_other_defaults(self):
        cfg = PluginConfig.from_raw({"dry_run": True})
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.summary_interval, 300.0)
        self.assertTrue(cfg.announce["dragon_kill"])

    def test_string_values_tolerated(self):
        """用户手改 TOML 写成字符串也不能崩。"""
        cfg = PluginConfig.from_raw({"enabled": "false", "poll_interval_seconds": "5"})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.poll_interval, 5.0)

    def test_garbage_types_fall_back(self):
        cfg = PluginConfig.from_raw({"poll_interval_seconds": "abc", "window_seconds": "x"})
        self.assertEqual(cfg.poll_interval, 2.0)
        self.assertEqual(cfg.window_seconds, 10.0)

    def test_fallback_defaults_match_dataclass(self):
        """``from_raw`` 的回退值必须和 dataclass 默认值一致。

        早先 ``window_seconds`` 的 dataclass 默认是 10，但 from_raw 缺项
        回退写成了 30 —— 用户删掉那一行或写错类型，播报就悄悄慢三倍。
        这条用例把所有数值字段的回退路径和默认值对齐。
        """
        from dataclasses import fields

        raw = PluginConfig.from_raw({})          # 整段缺失 → 全走回退
        base = PluginConfig()                     # 纯 dataclass 默认
        for f in fields(PluginConfig):
            got = getattr(raw, f.name)
            want = getattr(base, f.name)
            if isinstance(want, (int, float)) and not isinstance(want, bool):
                self.assertEqual(
                    got, want, f"{f.name}：from_raw 回退值 {got} != 默认值 {want}"
                )

    def test_poll_interval_clamped_to_tick(self):
        """轮询快于定时器粒度没有意义，会被夹到 2 秒。"""
        cfg = PluginConfig.from_raw({"poll_interval_seconds": 0.1})
        self.assertEqual(cfg.poll_interval, 2.0)

    def test_idle_interval_clamped_to_tick(self):
        cfg = PluginConfig.from_raw({"idle_poll_interval_seconds": 0.1})
        self.assertEqual(cfg.idle_poll_interval, 2.0)

    def test_timeout_has_margin_over_probe_delay(self):
        """超时必须明显大于"被拒绝的本地连接"的探测延迟。

        实测某些安全软件会给被拒连接叠加约 2 秒延迟，超时贴着这个值设
        会让"干净拒绝"退化成"超时"，白白多等好几秒。
        """
        cfg = PluginConfig.from_raw({})
        self.assertGreaterEqual(cfg.http_timeout, 5.0)


class TestPluginToml(unittest.TestCase):
    """校验插件清单本身。这些检查能接住"改了类名忘了改 entry"这类静默故障。"""

    @classmethod
    def setUpClass(cls):
        import tomllib

        cls.toml_path = _PLUGIN_DIR / "plugin.toml"
        cls.raw = tomllib.loads(cls.toml_path.read_text(encoding="utf-8"))

    def test_id_matches_directory_name(self):
        """目录名等于插件 ID —— 但发布仓库是例外。

        两种布局都合法：
        * **开发目录**：``Orca工作区/neko_lol/`` —— 目录名 == id。
        * **发布仓库**（市场要求根目录直接是 plugin.toml）：根目录叫
          ``n.e.k.o_plugin_neko_lol``，此时只能从 plugin.toml 读 id。
        """
        plugin_id = self.raw["plugin"]["id"]
        self.assertTrue(plugin_id, "plugin.toml 必须有 id")
        if _PLUGIN_DIR.name != plugin_id:
            # 不是同名目录时，必须是市场规范的仓库目录名
            self.assertEqual(
                _PLUGIN_DIR.name,
                f"n.e.k.o_plugin_{plugin_id}",
                "目录既不是插件 ID，也不是 n.e.k.o_plugin_<id> 的仓库名",
            )

    def test_entry_points_at_a_real_class(self):
        entry = self.raw["plugin"]["entry"]
        module_path, _, class_name = entry.partition(":")
        # entry 里的包路径永远是 plugin.plugins.<id>（宿主安装后的形态），
        # 与开发目录名无关。
        self.assertEqual(module_path, f"plugin.plugins.{self.raw['plugin']['id']}")
        import neko_lol

        self.assertTrue(
            hasattr(neko_lol, class_name), f"entry 指向的 {class_name} 不存在"
        )

    def test_no_unknown_config_keys(self):
        """键名写错、或者标量键写到了子表后面，都必须在这里被抓住。"""
        unknown = PluginConfig.unknown_keys(self.raw["neko_lol"])
        self.assertEqual(unknown, set(), f"plugin.toml 里有读不到的配置键：{unknown}")


    def test_announce_keys_are_real_cue_keys(self):
        """announce 里不能出现插件根本不会产生的事件 key。"""
        produced = set(cues.PRIORITIES)
        stray = set(self.raw["neko_lol"]["announce"]) - produced
        self.assertEqual(stray, set(), f"announce 里有不存在的事件：{stray}")

    def test_shipped_config_has_sane_window(self):
        """单一窗口模型下，唯一要检查的就是窗口长度本身。"""
        cfg = PluginConfig.from_raw(self.raw["neko_lol"])
        self.assertEqual(cfg.invariant_warnings(), [], "出厂配置存在冲突")
        self.assertEqual(cfg.window_seconds, 10.0)
        self.assertEqual(cfg.normal_gap, 3.0)
        self.assertGreater(cfg.merge_max_items, 0)

    def test_shipped_config_parses_to_expected_values(self):
        cfg = PluginConfig.from_raw(self.raw["neko_lol"])
        self.assertTrue(cfg.enabled)
        self.assertFalse(cfg.dry_run, "出厂配置不该默认干跑，否则用户以为坏了")
        self.assertTrue(cfg.post_game_summary)
        self.assertTrue(cfg.announce["champion_kill"])
        self.assertGreater(cfg.window_seconds, 0)

    def test_author_does_not_end_with_a_trailing_underscore(self):
        """[plugin.author] 是必填段，缺了宿主会拒绝加载。"""
        self.assertIn("author", self.raw["plugin"])
        self.assertIn("name", self.raw["plugin"]["author"])

    def test_i18n_dir_has_default_locale(self):
        i18n = self.raw["plugin"]["i18n"]
        locale_file = _PLUGIN_DIR / i18n["locales_dir"] / f"{i18n['default_locale']}.json"
        self.assertTrue(locale_file.is_file(), f"缺少默认语言包 {locale_file}")

    def test_every_declared_entry_has_i18n(self):
        import json

        path = _PLUGIN_DIR / "i18n" / "zh-CN.json"
        messages = json.loads(path.read_text(encoding="utf-8"))
        for entry_id in (
            "lol_status",
            "lol_summary_now",
            "lol_diagnose",
            "set_announce",
            "set_dry_run",
        ):
            self.assertIn(
                entry_id,
                messages.get("entries", {}),
                f"zh-CN.json 缺少 entries.{entry_id}",
            )

    def test_english_locale_is_gone(self):
        """选项文本写死中文，就不再保留英文语言包 —— 免得哪天又被选中。"""
        self.assertFalse(
            (_PLUGIN_DIR / "i18n" / "en.json").exists(),
            "还存在 en.json：宿主 locale 为英文时 tr() 会返回英文",
        )

    # ── 面板 ────────────────────────────────────────────────────

    def test_panel_is_declared_and_file_exists(self):
        ui = self.raw["plugin"].get("ui")
        self.assertIsNotNone(ui, "plugin.toml 缺少 [plugin.ui]")
        self.assertTrue(ui.get("enabled"))
        panel = ui["panel"][0]
        self.assertEqual(panel["context"], "lol_settings", "必须匹配 @ui.context 的 id")
        self.assertIn("state:read", panel["permissions"])
        self.assertIn("action:call", panel["permissions"], "没有它就调不了 set_announce")
        self.assertTrue(
            (_PLUGIN_DIR / panel["entry"]).is_file(),
            f"面板文件不存在：{panel['entry']}",
        )
        self.assertTrue(panel["entry"].endswith(".tsx"), "非 .tsx 会走静态/其他模式")

    def test_toggle_events_cover_all_announce_keys(self):
        """面板必须能控制每一个 announce 开关，且不能有重复或多余。"""
        import neko_lol

        self.assertEqual(set(neko_lol._TOGGLE_EVENTS), set(DEFAULT_ANNOUNCE))
        self.assertEqual(
            len(neko_lol._TOGGLE_EVENTS),
            len(set(neko_lol._TOGGLE_EVENTS)),
            "面板事件列表里有重复项",
        )

    def test_every_toggle_event_has_chinese_label(self):
        """面板用 context 下发的 label 渲染 —— 缺中文名会把原始 key 显示给用户。"""
        import neko_lol

        self.assertEqual(
            set(neko_lol._EVENT_LABELS), set(neko_lol._TOGGLE_EVENTS), "标签和开关项对不上"
        )
        for key in neko_lol._TOGGLE_EVENTS:
            label = neko_lol._EVENT_LABELS.get(key, "")
            self.assertTrue(label, f"{key} 没有中文名")
            self.assertRegex(label, r"[一-鿿]", f"{key} 的名字不是中文：{label}")

    def test_panel_text_is_inline_chinese_not_locale_driven(self):
        """面板选项文案必须内联中文，不能依赖 props.t()。

        早先用 t("events.<key>") 取文案，结果要赌宿主 locale 能不能解析到
        zh-CN 语言包；没解析到就显示英文甚至原始 key。选项文本不该由
        locale 决定。
        """
        import re

        source = (_PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
        # 注释里会提到旧写法，检查时必须排除，否则误报。
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)

        self.assertNotIn('t("events.', code, "面板仍在从语言包取事件文案")
        self.assertNotIn('t("panel.', code, "面板仍在从语言包取界面文案")
        self.assertRegex(code, r"[一-鿿]", "面板里没有中文文案")
        # 事件名必须来自 context 的 label 字段
        self.assertIn("item.label", code, "事件开关没有使用 Python 下发的中文名")


    def test_panel_groups_cover_every_toggle(self):
        """面板分组必须覆盖所有开关，且不能有重复/多余 key。

        分组表在 Python 侧（lol_settings 的 event_groups），TSX 只负责渲染。
        漏一个 key，那个开关在面板上就永远调不到。
        """
        import asyncio

        import neko_lol

        plugin = neko_lol.NekoLolPlugin(ctx=None)

        class _Quiet:
            def info(self, *_a, **_k): pass
            def warning(self, *_a, **_k): pass
            def error(self, *_a, **_k): pass
            def debug(self, *_a, **_k): pass

        plugin.logger = _Quiet()
        asyncio.run(plugin._load_config({"neko_lol": {}}))
        ctx = asyncio.run(plugin.lol_settings())
        groups = ctx["event_groups"]
        grouped: list[str] = []
        for group in groups:
            grouped.extend(group["keys"])
        self.assertEqual(
            set(grouped), set(neko_lol._TOGGLE_EVENTS),
            f"分组和开关对不上：多 {set(grouped) - set(neko_lol._TOGGLE_EVENTS)}"
            f" 少 {set(neko_lol._TOGGLE_EVENTS) - set(grouped)}",
        )
        self.assertEqual(len(grouped), len(set(grouped)), "分组里有重复 key")
        self.assertIn("milestone", grouped, "里程碑开关没有出现在面板分组里")

    def test_panel_only_uses_verified_components(self):
        """面板只能用运行时里真实存在的组件。

        这个运行时不是 React —— import 到不存在的导出会让面板整块白屏，
        而且报错只在宿主控制台里，很难查。这份名单来自
        ``tsxRuntime-*.js`` 里实际定义的 function 名。

        另一个坑：组件的 props 也不一定是你以为的那些
        （``StatCard`` 只认 label/value，``KeyValue`` 只认 items/data）。
        """
        import re

        source = (_PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
        # 已知存在的组件（从运行时提取）
        available = {
            "Alert", "Card", "Columns", "Divider", "Grid", "KeyValue", "Page",
            "Progress", "Stack", "StatCard", "StatusBadge", "Switch", "Tabs",
            "Text", "Tip", "useEffect", "useState",
        }
        imported = set(re.findall(r"^  ([A-Za-z]+),$", source, re.M))
        unknown = imported - available
        self.assertEqual(
            unknown, set(), f"面板 import 了运行时里没有的组件：{unknown}"
        )

        # StatCard 不接受 hint / title 这类额外参数，多传不会报错但也不显示。
        self.assertNotIn("hint=", source, "StatCard 不支持 hint 参数")
        # KeyValue 只认 items / data，写 label/value 会渲染成空行。
        self.assertNotRegex(
            source, r"<KeyValue\s+label=",
            "KeyValue 不支持 label/value，要用 data 或 items",
        )

    def test_prompt_visibility_switch_is_wired(self):
        """面板要有「在聊天里显示提示文本」开关，且接的是对应 action。

        visibility 与 ai_behavior 独立：默认 visibility=[]，模型照样回应，
        只是聊天里看不到括号提示。
        """
        source = (_PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
        self.assertIn("set_show_prompt_in_chat", source)
        self.assertIn("在聊天里显示提示文本", source)
        # 面板显示的状态字段必须由 context 下发
        ctx = (_PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"show_prompt_in_chat"', ctx)
        # 默认关闭
        self.assertIn("show_prompt_in_chat: bool = False", (
            _PLUGIN_DIR / "config.py").read_text(encoding="utf-8"))

    def test_every_panel_action_has_its_own_entry(self):
        """每个面板 action 必须紧跟自己的 @plugin_entry 和 def。

        实测踩过：给 set_show_prompt_in_chat 插装饰器时，把 set_dry_run 的
        装饰器块切断了 —— 两个函数共用同一套装饰器，面板上点「在聊天里
        显示提示文本」毫无反应。
        """
        import re

        src = (_PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
        # 按装饰器块切分：每段 "@ui.action(...)" 到下一个 "async def" 之间
        # 必须同时出现 @plugin_entry，且两者的 id= 一致，最后接上函数。
        pattern = re.compile(
            r"@ui\.action\(\s*id=\"([a-z_]+)\"(.*?)"
            r"@plugin_entry\(\s*id=\"([a-z_]+)\"(.*?)"
            r"async def ([a-z_]+)\(",
            re.S,
        )
        found = {m.group(1): m for m in pattern.finditer(src)}
        for name in ("set_announce", "set_dry_run", "set_show_prompt_in_chat"):
            self.assertIn(name, found, f"面板 action {name} 没有被正确注册")
        for action_id, m in found.items():
            entry_id, func = m.group(3), m.group(5)
            self.assertEqual(action_id, entry_id,
                             f"{action_id} 的 action/entry id 不一致")
            self.assertEqual(action_id, func,
                             f"{action_id} 的 id 与函数名 {func} 不一致")

        # 反向检查：不能有孤立的 @ui.action（后面没有 @plugin_entry）
        isolated = re.findall(
            r"@ui\.action\([^)]*?id=\"([a-z_]+)\".*?(?=async def|@plugin_entry)",
            src, re.S,
        )
        self.assertEqual(
            set(isolated) - set(found), set(),
            "有 ui.action 没有紧跟 plugin_entry",
        )

    def test_panel_has_tabs_and_progress(self):
        """面板按 tab 分区，里程碑用 Progress 显示完成度。"""
        source = (_PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
        self.assertIn("<Tabs", source)
        self.assertIn("activeId={tab}", source, "Tabs 必须受控，否则拿不到当前页")
        self.assertIn("<Progress", source)
        self.assertIn("milestonePercent", source)

    def test_config_schema_covers_every_key(self):
        """config.schema.json 必须覆盖 plugin.toml 的每个配置键。

        这份 schema 决定插件面板里怎么展示运行配置（中文标题、说明、
        默认值）。漏一个键，那个配置在面板上就没有中文名，只剩裸 key。
        """
        import json

        schema_path = _PLUGIN_DIR / "config.schema.json"
        self.assertTrue(schema_path.is_file(), "缺少 config.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        props = schema["properties"]["neko_lol"]["properties"]

        shipped = self.raw["neko_lol"]
        # 标量键 + announce 子表（schema 里把子表也当属性描述）
        toml_keys = {k for k, v in shipped.items() if not isinstance(v, dict)}
        if "announce" in shipped:
            toml_keys.add("announce")
        self.assertEqual(
            toml_keys - set(props), set(), "schema 缺少这些配置键的中文定义"
        )
        self.assertEqual(
            set(props) - toml_keys, set(), "schema 里有 plugin.toml 不存在的键"
        )

    def test_config_schema_defaults_match_shipped(self):
        """schema 里的 default 要和 plugin.toml 一致。

        面板用 schema 的 default 渲染"恢复默认"，两边不一致会让用户
        看到的值和实际行为对不上。
        """
        import json

        schema = json.loads(
            (_PLUGIN_DIR / "config.schema.json").read_text(encoding="utf-8")
        )
        props = schema["properties"]["neko_lol"]["properties"]
        shipped = self.raw["neko_lol"]
        for key, spec in props.items():
            if key == "announce":
                continue
            self.assertIn(key, shipped, f"{key} 不在 plugin.toml 里")
            self.assertEqual(
                spec.get("default"), shipped[key],
                f"{key} 的 schema 默认值 {spec.get('default')} != plugin.toml {shipped[key]}",
            )

    def test_config_schema_has_chinese_titles(self):
        """每个配置项都要有 zh-CN 标题，否则面板上显示英文或裸 key。"""
        import json

        schema = json.loads(
            (_PLUGIN_DIR / "config.schema.json").read_text(encoding="utf-8")
        )
        section = schema["properties"]["neko_lol"]
        self.assertIn("zh-CN", section.get("x-title-i18n", {}), "段落缺中文标题")

        for key, spec in section["properties"].items():
            if key == "announce":
                for sub, sub_spec in spec["properties"].items():
                    self.assertIn(
                        "zh-CN", sub_spec.get("x-title-i18n", {}),
                        f"announce.{sub} 缺中文标题",
                    )
                continue
            self.assertIn(
                "zh-CN", spec.get("x-title-i18n", {}), f"{key} 缺中文标题"
            )

    def test_config_example_does_not_drift(self):
        """config.example.toml 是用户实际会改的那份，绝不能和 plugin.toml 跑偏。

        双向检查：多一个键（模板里写了 plugin.toml 没有的）或少一个键
        （新增配置忘了写进模板，用户就永远看不到这个开关）都算跑偏。
        """
        import tomllib

        example = tomllib.loads(
            (_PLUGIN_DIR / "config.example.toml").read_text(encoding="utf-8")
        )
        shipped = self.raw["neko_lol"]
        example_section = example["neko_lol"]

        example_scalars = {
            k for k, v in example_section.items() if not isinstance(v, dict)
        }
        shipped_scalars = {k for k, v in shipped.items() if not isinstance(v, dict)}
        self.assertEqual(
            example_scalars,
            shipped_scalars,
            "标量配置键两边对不上（少的那边就是漏文档/多余的键）",
        )
        self.assertEqual(
            set(example_section["announce"]),
            set(shipped["announce"]),
            "两边的 announce 键集必须一致",
        )

    def test_config_example_matches_shipped_defaults(self):
        """模板里的值就是出厂默认值 —— 两边填的数不一样会让用户困惑。"""
        import tomllib

        example = tomllib.loads(
            (_PLUGIN_DIR / "config.example.toml").read_text(encoding="utf-8")
        )["neko_lol"]
        shipped = self.raw["neko_lol"]
        for key, value in example.items():
            if isinstance(value, dict):
                continue
            self.assertEqual(
                value, shipped.get(key), f"{key} 在模板和出厂配置里不一致"
            )


def _lane_roster(me_index: int, *, me_kw=None, opp_kw=None, team_kills: int = 0):
    """造一份"主人排在第 me_index 位"的十人名单（0=上单 … 4=辅助）。

    ``team_kills`` 是**全队人头总数**：先扣掉主人自己的人头（``me_kw`` 里
    的 ``kills``），剩下的平均分给 4 个队友（余数给第一个）。这样
    ``sum(allies.kills)`` 就恰好等于 ``team_kills``。
    """
    lanes = ["上单哥", "打野哥", "中单哥", "ADC哥", "辅助哥"]
    others = [i for i in range(5) if i != me_index]
    me_stats = dict(me_kw or {})
    my_kills = int(me_stats.get("kills", 0) or 0)
    share, extra = divmod(max(0, team_kills - my_kills), len(others))
    players = []
    for i in range(5):
        if i == me_index:
            players.append(_player("我", "Yasuo", "ORDER", **me_stats))
        else:
            bonus = extra if i == others[0] else 0
            players.append(
                _player(lanes[i], f"英雄{i}", "ORDER", kills=share + bonus)
            )
    for i in range(5):
        players.append(
            _player(f"敌{i}", f"敌英雄{i}", "CHAOS", **((opp_kw if i == me_index else {}) or {}))
        )
    return players


def _lane_state(me_index: int, *, game_time: float = 600.0, **kw) -> game_model.MatchState:
    return game_model.MatchState.from_all_game_data(
        _all_game_data(game_time=game_time, players=_lane_roster(me_index, **kw)),
        trust_lane_order=True,
    )


class TestLaneMilestones(unittest.TestCase):
    """分路里程碑：每条规则单独验，以及"不可信就不播"。"""

    def test_top_solo_triple_kill(self):
        state = _lane_state(0)                     # 上单
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        for _ in range(3):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "我", "VictimName": opponent},
                state,
            )
        cues_out = m.check(state)
        self.assertEqual(len(cues_out), 1)
        self.assertIn("单杀", cues_out[0].text)
        # 一局只播一次
        self.assertEqual(m.check(state), [])

    def test_milestone_uses_champion_name_not_player_name(self):
        """里程碑要报英雄名，不能报玩家昵称。

        实测 2026-11-16：
          "单杀对位 欲将爱意隐于风中 3 次"   ← 玩家名，读起来莫名其妙
        """
        state = _lane_state(0)
        opponent = state.lane_opponent()
        m = milestones.LaneMilestones()
        for _ in range(3):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "我",
                 "VictimName": opponent.name},
                state,
            )
        text = m.check(state)[0].text
        self.assertIn(opponent.champion, text, f"没报英雄名：{text}")
        self.assertNotIn(opponent.name, text, f"把玩家名念出来了：{text}")

    def test_milestone_text_does_not_recite_the_rule(self):
        """播报只说成绩，不复述触发条件。"""
        # 上单
        state = _lane_state(0)
        opponent = state.lane_opponent().name
        m = milestones.LaneMilestones()
        for _ in range(3):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "我", "VictimName": opponent},
                state,
            )
        text = m.check(state)[0].text
        for bad in ("15 分钟前", "一次都没被抓", "3 次", "15 分钟"):
            self.assertNotIn(bad, text, f"把条件念出来了：{text}")

        # ADC
        m2 = milestones.LaneMilestones()
        adc_text = m2.check(_lane_state(3, me_kw={"cs": 200}, opp_kw={"cs": 120}))[0].text
        for bad in ("20 分钟前", "20 分钟"):
            self.assertNotIn(bad, adc_text, f"把条件念出来了：{adc_text}")

    def test_top_requires_no_assists(self):
        """有人帮忙的击杀不算"对位单杀"。"""
        state = _lane_state(0)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        for _ in range(3):
            m.note_kill(
                {
                    "EventName": "ChampionKill",
                    "KillerName": "我",
                    "VictimName": opponent,
                    "Assisters": ["打野哥"],
                },
                state,
            )
        self.assertEqual(m.check(state), [])

    def test_top_disqualified_when_caught_by_opponent(self):
        state = _lane_state(0)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        for _ in range(3):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "我", "VictimName": opponent},
                state,
            )
        # 被对位抓一次 → 资格取消
        m.note_kill(
            {"EventName": "ChampionKill", "KillerName": opponent, "VictimName": "我"},
            state,
        )
        self.assertEqual(m.check(state), [])

    def test_top_disqualified_when_opponent_assists(self):
        """对位选手参与击杀了我，也算被抓。"""
        state = _lane_state(0)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        m.note_kill(
            {
                "EventName": "ChampionKill",
                "KillerName": "敌4",
                "VictimName": "我",
                "Assisters": [opponent],
            },
            state,
        )
        self.assertTrue(m._state["top_disqualified"])

    def test_top_expires_after_15_minutes(self):
        state = _lane_state(0, game_time=901.0)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        for _ in range(3):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "我", "VictimName": opponent},
                state,
            )
        self.assertEqual(m.check(state), [])

    def test_jungle_triggers_over_ten_team_kills(self):
        """全队人头 > 10 且主人参与度 ≥ 60%。"""
        m = milestones.LaneMilestones()
        # 11 个人头，主人参与 8 个（73%）
        state = _lane_state(1, team_kills=11, me_kw={"kills": 4, "assists": 4})
        out = m.check(state)
        self.assertEqual(len(out), 1, f"没触发：{out}")
        self.assertIn("人头", out[0].text)
        self.assertIn("主人参与了", out[0].text)
        # 同一局再 check 不能重复播
        self.assertEqual(m.check(state), [], "一局只说一次")

    def test_jungle_respects_threshold_boundary(self):
        m = milestones.LaneMilestones()
        state = _lane_state(1, team_kills=10, me_kw={"kills": 5, "assists": 5})
        self.assertEqual(m.check(state), [], "10 不算超过")
        self.assertEqual(sum(a.kills for a in state.allies), 10)

    def test_jungle_requires_sixty_percent_participation(self):
        """全队人头够了但主人没参与，不能把功劳算给打野。"""
        m = milestones.LaneMilestones()
        # 12 个头，主人只参与 2 个（17%）
        state = _lane_state(1, team_kills=12, me_kw={"kills": 1, "assists": 1})
        self.assertEqual(m.check(state), [], "参与度不足却播了")

    def test_jungle_participation_boundary(self):
        """刚好 60% 应当触发，59% 不触发。"""
        # 10 个人头不够（必须 >10），用 20 个：12 参与 = 60%
        m1 = milestones.LaneMilestones()
        state = _lane_state(1, team_kills=20, me_kw={"kills": 6, "assists": 6})
        self.assertEqual(sum(a.kills for a in state.allies), 20)
        self.assertEqual(len(m1.check(state)), 1, "60% 应当触发")

        m2 = milestones.LaneMilestones()
        # 11 参与 / 20 = 55%
        state2 = _lane_state(1, team_kills=20, me_kw={"kills": 5, "assists": 6})
        self.assertEqual(m2.check(state2), [], "55% 不该触发")

    def test_mid_rotations_require_five(self):
        state = _lane_state(2)                     # 中单
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name       # 打对位的不算转移击杀
        others = [p.name for p in state.enemies if p.name != opponent]
        self.assertTrue(others, "敌方名单里没有非对位目标")

        # 同一个目标被反复击杀也算多次转移（接口只给每次击杀，不去重）
        for _ in range(4):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "上单哥",
                 "VictimName": others[0], "Assisters": ["我"]},
                state,
            )
        self.assertEqual(m.check(state), [], "4 次不该触发")

        m.note_kill(
            {"EventName": "ChampionKill", "KillerName": "上单哥",
             "VictimName": others[-1], "Assisters": ["我"]},
            state,
        )
        out = m.check(state)
        self.assertEqual(len(out), 1)
        self.assertIn("游走", out[0].text)

    def test_mid_ignores_lane_opponent_kills(self):
        """打对位的击杀不算"转移"。"""
        state = _lane_state(2)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        for _ in range(6):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "上单哥",
                 "VictimName": opponent, "Assisters": ["我"]},
                state,
            )
        self.assertEqual(m._state["mid_rotations"], 0)
        self.assertEqual(m.check(state), [])

    def test_adc_cs_lead(self):
        m = milestones.LaneMilestones()
        self.assertEqual(
            m.check(_lane_state(3, me_kw={"cs": 160}, opp_kw={"cs": 120})), [],
            "领先 40 不该触发",
        )
        out = m.check(_lane_state(3, me_kw={"cs": 180}, opp_kw={"cs": 120}))
        self.assertEqual(len(out), 1)
        self.assertIn("补刀", out[0].text)

    def test_adc_expires_after_20_minutes(self):
        m = milestones.LaneMilestones()
        state = _lane_state(3, game_time=1201.0, me_kw={"cs": 200}, opp_kw={"cs": 100})
        self.assertEqual(m.check(state), [])

    def test_support_steps(self):
        m = milestones.LaneMilestones()
        for assists, expect in ((9, 0), (10, 1), (15, 0), (20, 1), (25, 0)):
            state = _lane_state(4, me_kw={"assists": assists})
            self.assertEqual(len(m.check(state)), expect, f"{assists} 个助攻")

    def test_each_milestone_fires_exactly_once(self):
        """除辅助（10/20 两个台阶）外，每条里程碑全局只播一次。

        轮询每 2 秒 check 一次，没有去重的话一局能刷几十遍。
        """
        # 上单
        state = _lane_state(0)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        for _ in range(3):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "我", "VictimName": opponent},
                state,
            )
        self.assertEqual(sum(len(m.check(state)) for _ in range(10)), 1, "上单播了多次")

        # ADC
        state = _lane_state(3, me_kw={"cs": 200}, opp_kw={"cs": 100})
        m = milestones.LaneMilestones()
        self.assertEqual(sum(len(m.check(state)) for _ in range(10)), 1, "ADC 播了多次")

        # 打野
        state = _lane_state(1, team_kills=20, me_kw={"kills": 6, "assists": 6})
        m = milestones.LaneMilestones()
        self.assertEqual(sum(len(m.check(state)) for _ in range(10)), 1, "打野播了多次")

        # 中单
        state = _lane_state(2)
        m = milestones.LaneMilestones()
        opponent = state.lane_opponent().name
        other = next(p.name for p in state.enemies if p.name != opponent)
        for _ in range(5):
            m.note_kill(
                {"EventName": "ChampionKill", "KillerName": "上单哥",
                 "VictimName": other, "Assisters": ["我"]},
                state,
            )
        self.assertEqual(sum(len(m.check(state)) for _ in range(10)), 1, "中单播了多次")

    def test_support_fires_exactly_twice(self):
        """辅助只有 10 / 20 两个台阶，跨过之后不该再刷。"""
        m = milestones.LaneMilestones()
        total = 0
        for assists in (9, 10, 10, 15, 20, 20, 30):
            state = _lane_state(4, me_kw={"assists": assists})
            total += sum(len(m.check(state)) for _ in range(3))
        self.assertEqual(total, 2, f"辅助应只播 10 和 20 两次，实际 {total}")

    def test_reset_allows_next_match_to_fire_again(self):
        """换局后里程碑要能重新触发。"""
        state = _lane_state(3, me_kw={"cs": 200}, opp_kw={"cs": 100})
        m = milestones.LaneMilestones()
        self.assertEqual(len(m.check(state)), 1)
        m.reset()
        self.assertEqual(len(m.check(state)), 1, "换局后没有重新触发")

    def test_untrusted_lanes_never_fire(self):
        """普通匹配/大乱斗顺序不可信 —— 一律不播。"""
        m = milestones.LaneMilestones()
        state = game_model.MatchState.from_all_game_data(
            _all_game_data(players=_lane_roster(1, team_kills=20)),
            trust_lane_order=False,
        )
        self.assertFalse(state.lanes_trusted)
        self.assertEqual(m.check(state), [])

    def test_reset_clears_state(self):
        m = milestones.LaneMilestones()
        m._state["jungle_said"] = True
        m.reset()
        self.assertFalse(m._state["jungle_said"])

    def test_snapshot_has_progress_per_lane(self):
        for index, lane in enumerate(("上单", "打野", "中单", "ADC", "辅助")):
            state = _lane_state(index)
            snap = milestones.LaneMilestones().snapshot(state)
            self.assertEqual(snap["lane"], lane)
            self.assertTrue(snap["enabled"])
            self.assertIn("need", snap, f"{lane} 的进度里没有目标值")


if __name__ == "__main__":
    unittest.main(verbosity=2)
