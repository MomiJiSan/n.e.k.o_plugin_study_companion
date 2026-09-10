"""Versioned forest content and pure expedition rules; learning cards are untouched."""

from copy import deepcopy

FOREST_ID = "forest_v0_2"
REGIONS = [{"id": "edge", "name": "林缘"}, {"id": "mist", "name": "迷雾腹地"}, {"id": "ruins", "name": "守卫遗迹"}]


def _node(name, kind, region, x, y, next_ids, description, risk, reward):
    return dict(
        name=name,
        type=kind,
        region_id=region,
        x=x,
        y=y,
        next=next_ids,
        description=description,
        risk=risk,
        reward=reward,
    )


NODES = {
    "entrance": _node(
        "森林入口",
        "entrance",
        "edge",
        13,
        22,
        ["wisp_path", "old_camp"],
        "林道分成两支，远处传来旧哨塔的铃声。",
        "选择战斗或调查路线",
        "发现林缘",
    ),
    "wisp_path": _node(
        "微灵林道",
        "battle",
        "edge",
        26,
        30,
        ["edge_spring"],
        "公式微灵守着一批散落的木材。",
        "普通战斗",
        "3 份材料与战斗奖励",
    ),
    "old_camp": _node(
        "旧远征营地",
        "investigation",
        "edge",
        13,
        42,
        ["edge_spring"],
        "残破日志记着守卫的聚能规律。",
        "深入调查消耗 2 点生命",
        "永久线索与守卫情报，或 2 份材料",
    ),
    "edge_spring": _node(
        "林缘泉",
        "rest",
        "edge",
        19,
        67,
        ["convergence", "mist_patrol"],
        "泉水旁有可采集的青苔。",
        "只能选择一项收益",
        "恢复 6 点生命或采集 2 份材料",
    ),
    "convergence": _node(
        "趋近回廊",
        "mechanism",
        "mist",
        36,
        46,
        ["broken_creek"],
        "每一步跨过剩余路程的一半；不断重复无法真正抵达终点。",
        "强行穿越消耗 5 点生命",
        "稳定终点锚点：3 份材料；穿越：5 份",
    ),
    "mist_patrol": _node(
        "迷雾巡守",
        "battle",
        "mist",
        44,
        74,
        ["relic_grove"],
        "更强的巡守占据了一条藏有遗物的支路。",
        "敌人生命 16，攻击 4",
        "3 份材料与遗物路线",
    ),
    "broken_creek": _node(
        "断续溪谷",
        "investigation",
        "mist",
        50,
        50,
        ["memory_spring"],
        "漂来的远征标记指出汇合点的位置。",
        "搜寻消耗 2 点生命",
        "4 份材料，或安全通过",
    ),
    "relic_grove": _node(
        "余响树穴",
        "investigation",
        "mist",
        80,
        66,
        ["memory_spring"],
        "树根中埋着一枚远征者留下的护符。",
        "取出护符消耗 3 点生命",
        "苔光护符：本轮后续每场战斗伤害提高 10%",
    ),
    "memory_spring": _node(
        "记忆泉",
        "rest",
        "ruins",
        53,
        24,
        ["guardian_seal"],
        "遗迹前最后一处安全水源。",
        "治疗与准备只能选一项",
        "恢复 8 点生命或下一战伤害提高 25%",
    ),
    "guardian_seal": _node(
        "守卫碑文",
        "investigation",
        "ruins",
        74,
        36,
        ["guardian"],
        "碑文指向守卫聚能核心的裂隙。",
        "解析消耗 3 点生命",
        "守卫情报，或采集 2 份材料",
    ),
    "guardian": _node(
        "极限守卫",
        "boss",
        "ruins",
        87,
        27,
        [],
        "守卫在遗迹前聚能；远征调查能够破坏它的核心。",
        "生命 30 / 攻击 6；有情报时生命 22 / 攻击 4",
        "8 份材料与守望徽记",
    ),
}
CHOICES = {
    "old_camp": [
        ("read_journal", "研读远征日志", "消耗 2 点生命，永久保存线索并发现守卫弱点"),
        ("salvage", "回收散落物资", "获得 2 份材料"),
    ],
    "edge_spring": [("heal", "饮用泉水", "恢复 6 点生命"), ("gather", "采集青苔", "获得 2 份材料")],
    "convergence": [
        ("anchor", "固定终点锚点", "停止无限折半，获得 3 份材料"),
        ("force", "强行穿越", "消耗 5 点生命，获得 5 份材料"),
    ],
    "broken_creek": [
        ("search", "搜寻远征物资", "消耗 2 点生命，获得 4 份材料"),
        ("cross", "沿岸安全通过", "不消耗生命"),
    ],
    "relic_grove": [
        ("take_relic", "取出苔光护符", "消耗 3 点生命，后续战斗伤害提高 10%"),
        ("leave", "留下护符", "安全通过"),
    ],
    "memory_spring": [("heal", "恢复生命", "恢复 8 点生命"), ("prepare", "调整战斗准备", "下一场战斗伤害提高 25%")],
    "guardian_seal": [
        ("decipher", "解析核心裂隙", "消耗 3 点生命，发现守卫弱点"),
        ("gather", "收集遗迹材料", "获得 2 份材料"),
    ],
}


def empty_camp():
    return dict(materials=0, relics=[], clues=[], watchtower_repaired=False)


def new_expedition():
    return dict(
        content_version=FOREST_ID,
        carried_materials=0,
        carried_relics=[],
        clues=[],
        boss_intel=False,
        event=None,
        settlement=None,
        camp=empty_camp(),
    )


def public_expedition(expedition):
    return {
        **deepcopy(expedition),
        "regions": deepcopy(REGIONS),
        "nodes": [dict(id=key, **deepcopy(value)) for key, value in NODES.items()],
    }


def reveal_next(state):
    state.available_node_ids = list(NODES[state.current_node_id]["next"])
    state.revealed_node_ids = list(dict.fromkeys(state.revealed_node_ids + state.available_node_ids))


def enter_event(state):
    node_id = state.selected_node_id
    node = NODES[node_id]
    state.current_node_id = node_id
    state.phase = "event"
    state.available_node_ids = []
    state.expedition["event"] = dict(
        node_id=node_id,
        title=node["name"],
        description=node["description"],
        choices=[dict(id=choice[0], label=choice[1], description=choice[2]) for choice in CHOICES[node_id]],
    )
    return [{"type": "event_entered", "node_id": node_id}]


def choose_event(state, choice_id):
    exp = state.expedition
    event = exp["event"]
    if state.phase != "event" or not event or choice_id not in [c["id"] for c in event["choices"]]:
        raise ValueError("event choice unavailable")
    node_id = event["node_id"]
    damage = 0
    materials = 0
    if choice_id == "read_journal":
        damage = 2
        exp["boss_intel"] = True
        exp["clues"] = list(dict.fromkeys(exp["clues"] + ["old_expedition_journal"]))
        exp["camp"]["clues"] = list(dict.fromkeys(exp["camp"]["clues"] + exp["clues"]))
    elif choice_id == "decipher":
        damage = 3
        exp["boss_intel"] = True
    elif choice_id in {"salvage", "gather"}:
        materials = 2
    elif choice_id == "heal":
        state.player_hp = min(state.player_max_hp, state.player_hp + (8 if node_id == "memory_spring" else 6))
    elif choice_id == "prepare":
        state.next_encounter_damage_bps = 12500
    elif choice_id == "anchor":
        materials = 3
    elif choice_id == "force":
        damage, materials = 5, 5
    elif choice_id == "search":
        damage, materials = 2, 4
    elif choice_id == "take_relic":
        damage = 3
        exp["carried_relics"].append("moss_charm")
    state.player_hp = max(0, state.player_hp - damage)
    exp["carried_materials"] += materials
    exp["event"] = None
    state.selected_node_id = None
    state.completed_node_ids.append(node_id)
    state.phase = "map"
    reveal_next(state)
    if state.player_hp == 0:
        state.status = state.phase = "failed"
    return [{"type": "event_resolved", "node_id": node_id, "choice_id": choice_id}]


def settle(state):
    exp = state.expedition
    if exp["settlement"] is not None or state.status not in {"failed", "completed", "abandoned"}:
        return []
    materials = exp["carried_materials"]
    kept = materials // 2 if state.status == "failed" else materials
    relics = [] if state.status == "failed" else list(exp["carried_relics"])
    exp["settlement"] = dict(
        outcome=state.status, materials_kept=kept, materials_lost=materials - kept, relics_kept=relics
    )
    exp["camp"]["materials"] += kept
    exp["camp"]["relics"] = list(dict.fromkeys(exp["camp"]["relics"] + relics))
    exp["carried_materials"] = 0
    exp["carried_relics"] = []
    exp["event"] = None
    state.available_node_ids = []
    state.pending_rewards = []
    state.energy = 0
    return [
        {
            "type": "expedition_settled",
            "outcome": state.status,
            "materials_kept": kept,
            "materials_lost": materials - kept,
        }
    ]


def repair_camp(state):
    camp = state.expedition["camp"]
    if state.phase not in {"complete", "failed"} or camp["watchtower_repaired"] or camp["materials"] < 6:
        raise ValueError("camp repair unavailable")
    camp["materials"] -= 6
    camp["watchtower_repaired"] = True
    return [{"type": "camp_repaired", "repair_id": "watchtower"}]
