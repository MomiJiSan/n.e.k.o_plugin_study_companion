"""Server-owned action planning for public Knowledge Dungeon clients."""

from __future__ import annotations

from dataclasses import dataclass

from .bridge_contracts import BridgeContractError, PerformActionRequest, PublicAction
from .state import RunState

_NODE_LABELS = {
    "battle_1": "进入普通战斗",
    "trap_1": "进入易错陷阱",
    "rest_1": "前往休整",
    "boss_1": "挑战极限守卫",
}
_REWARD_LABELS = {
    "heal_6": "恢复 6 点生命",
    "next_damage_25": "下一场战斗伤害提高 25%",
    "reveal_map": "显露完整地图",
}


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """Private mapping from an opaque public action to one engine command."""

    public: PublicAction
    intent: str
    payload_items: tuple[tuple[str, str], ...]
    expected_state_version: int

    @property
    def payload(self) -> dict[str, str]:
        return dict(self.payload_items)


def _action_plan(
    state: RunState,
    *,
    action_type: str,
    label: str,
    intent: str,
    target_id: str | None = None,
    payload: dict[str, str] | None = None,
) -> ActionPlan:
    payload = payload or {}
    if action_type == "select_node":
        action_id = f"select_node:{target_id}"
    elif action_type == "enter_selected_node":
        action_id = "enter_selected_node"
    elif action_type == "play_card":
        action_id = f"play_card:{target_id}"
    elif action_type == "choose_reward":
        action_id = f"choose_reward:{target_id}"
    elif action_type in {"end_turn", "abandon_run", "finish_run"}:
        action_id = action_type
    else:
        raise ValueError(f"unsupported public action type: {action_type}")
    return ActionPlan(
        public=PublicAction(action_id, action_type, label, target_id),
        intent=intent,
        payload_items=tuple(sorted(payload.items())),
        expected_state_version=state.state_version,
    )


def build_available_actions(state: RunState) -> tuple[ActionPlan, ...]:
    """Return every command the authority currently permits a client to request."""

    plans: list[ActionPlan] = []
    if state.phase == "map":
        if state.status == "boss_defeated":
            plans.append(
                _action_plan(
                    state,
                    action_type="finish_run",
                    label="结束本轮探索",
                    intent="finish_run",
                )
            )
        else:
            for node_id in state.available_node_ids:
                plans.append(
                    _action_plan(
                        state,
                        action_type="select_node",
                        label=_NODE_LABELS.get(node_id, "选择节点"),
                        intent="select_node",
                        target_id=node_id,
                        payload={"node_id": node_id},
                    )
                )
            if state.selected_node_id is not None:
                plans.append(
                    _action_plan(
                        state,
                        action_type="enter_selected_node",
                        label=_NODE_LABELS.get(state.selected_node_id, "进入节点"),
                        intent="start_encounter",
                        target_id=state.selected_node_id,
                    )
                )
    elif state.phase == "encounter":
        for card_id in state.hand:
            card = state.cards.get(card_id)
            if card is None or card_id in state.dormant_card_ids:
                continue
            if card.energy_cost > state.energy:
                continue
            if card.starter and state.mercy_used_this_turn:
                continue
            plans.append(
                _action_plan(
                    state,
                    action_type="play_card",
                    label=f"打出「{card.name}」",
                    intent="play_card",
                    target_id=card_id,
                    payload={"card_id": card_id},
                )
            )
        plans.append(
            _action_plan(
                state,
                action_type="end_turn",
                label="结束回合",
                intent="end_turn",
            )
        )
        plans.append(
                _action_plan(
                    state,
                    action_type="abandon_run",
                    label="撤离本轮探索",
                    intent="leave_encounter",
            )
        )
    elif state.phase == "reward":
        for reward_id in state.pending_rewards:
            plans.append(
                _action_plan(
                    state,
                    action_type="choose_reward",
                    label=_REWARD_LABELS.get(reward_id, "选择本轮奖励"),
                    intent="choose_run_reward",
                    target_id=reward_id,
                    payload={"reward_id": reward_id},
                )
            )
    return tuple(plans)


def resolve_available_action(state: RunState, action_id: str) -> ActionPlan | None:
    return next((plan for plan in build_available_actions(state) if plan.public.action_id == action_id), None)


def command_for_action_id(action_id: str) -> tuple[str, dict[str, str]]:
    """Reconstruct an engine command after response loss or service restart."""

    # Reuse the strict public grammar instead of maintaining a second parser regex.
    try:
        PerformActionRequest(
            request_id="parser-validation",
            expected_state_version=1,
            action_id=action_id,
        )
    except BridgeContractError as exc:
        raise BridgeContractError("invalid_request", "action_id is invalid") from exc

    if action_id.startswith("select_node:"):
        return "select_node", {"node_id": action_id.partition(":")[2]}
    if action_id == "enter_selected_node":
        return "start_encounter", {}
    if action_id.startswith("play_card:"):
        return "play_card", {"card_id": action_id.partition(":")[2]}
    if action_id == "end_turn":
        return "end_turn", {}
    if action_id.startswith("choose_reward:"):
        return "choose_run_reward", {"reward_id": action_id.partition(":")[2]}
    if action_id == "abandon_run":
        return "leave_encounter", {}
    if action_id == "finish_run":
        return "finish_run", {}
    raise BridgeContractError("invalid_request", "action_id is invalid")
