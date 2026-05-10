"""Access rules, completion condition, and sequential-mode placement."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .data.duel_table import DUELS, SERIES_NAMES, SERIES_SLOT_COUNTS, is_active_duel
from .items import DUEL_UNLOCK_NAMES, create_item

if TYPE_CHECKING:
    from .world import YGOLotDWorld


def _duel_name(series: int, slot: int) -> str | None:
    for entry in DUELS:
        if entry["series"] == series and entry["slot"] == slot:
            return entry["name"]
    return None


def _series_first_slot(series: int) -> int:
    """First active (non-tutorial, non-gap) slot in a series. Always 2 in
    practice (slot 1 is the tutorial) but defensive."""
    for entry in DUELS:
        if entry["series"] == series and is_active_duel(entry):
            return entry["slot"]
    raise RuntimeError(f"series {series} has no active duels")


def _series_finale_name(series: int) -> str:
    """Highest-numbered active duel in this series. Used as the goal target."""
    last = None
    for entry in DUELS:
        if entry["series"] == series and is_active_duel(entry):
            last = entry["name"]
    if last is None:
        raise RuntimeError(f"no finale found for series {series}")
    return last


def set_all_rules(world: YGOLotDWorld) -> None:
    set_all_location_rules(world)
    set_completion_condition(world)


def set_all_location_rules(world: YGOLotDWorld) -> None:
    for entry in DUELS:
        if not is_active_duel(entry):
            continue
        unlock = f"{entry['name']} Unlock"
        for kind in (" Win", " First Clear Bonus"):
            location = world.get_location(entry["name"] + kind)
            world.set_rule(
                location,
                lambda state, u=unlock, p=world.player: state.has(u, p),
            )


def set_completion_condition(world: YGOLotDWorld) -> None:
    world.multiworld.completion_condition[world.player] = (
        lambda state, p=world.player: state.has("Victory", p)
    )


def goal_victory_locations(world: YGOLotDWorld) -> list[str]:
    """Candidate Win-locations onto which `Victory` may be locked.

    In sequential campaign mode, every non-finale Win loc is already
    consumed by `place_sequential_unlocks` (it locks the next slot's Unlock
    onto the previous slot's Win). The only Win locs left available are
    the per-series finales — same set as `any_series_finale`."""
    mode = world.options.goal.current_key
    sequential = world.options.campaign_mode.current_key == "sequential"
    if mode == "any_series_finale" or sequential:
        return [f"{_series_finale_name(s)} Win" for s in range(6)]
    # duel_count + random campaign: any active Win loc except the precollected
    # starters (first active slot of each series, i.e. slot 2 post-tutorial-skip).
    starter_names = {
        _duel_name(s, _series_first_slot(s)) for s in range(6)
    }
    return [
        f"{e['name']} Win"
        for e in DUELS
        if is_active_duel(e) and e["name"] not in starter_names
    ]


def sequentially_placed_unlock_names(world: YGOLotDWorld) -> set[str]:
    """Names of unlock items consumed by sequential placement (so the item-pool
    builder skips them). Returns empty set in random mode."""
    mode = world.options.campaign_mode.current_key
    if mode != "sequential":
        return set()
    placed: set[str] = set()
    for series in range(6):
        slots_with_names = sorted(
            e["slot"] for e in DUELS if e["series"] == series and is_active_duel(e)
        )
        # All slots after the first active one get pre-placed (the first active
        # slot — slot 2 post-tutorial-skip — is precollected).
        for slot in slots_with_names[1:]:
            placed.add(f"{_duel_name(series, slot)} Unlock")
    return placed


def place_sequential_unlocks(world: YGOLotDWorld) -> None:
    """For sequential mode, lock each duel's Unlock onto the previous duel's
    Win location. Slot 1 of every series is precollected (handled in items)."""
    if world.options.campaign_mode.current_key != "sequential":
        return
    for series in range(6):
        slots_with_names = sorted(
            (e["slot"], e["name"]) for e in DUELS
            if e["series"] == series and is_active_duel(e)
        )
        for i in range(1, len(slots_with_names)):
            prev_name = slots_with_names[i - 1][1]
            cur_name = slots_with_names[i][1]
            location = world.get_location(f"{prev_name} Win")
            location.place_locked_item(create_item(world, f"{cur_name} Unlock"))
