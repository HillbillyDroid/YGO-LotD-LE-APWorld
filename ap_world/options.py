"""YAML option schema for the YGO LotD-LE AP world.

Mirrors the fields documented in PLAN.md §Options. The `goal_duel_count` upper
bound is the number of named campaign duels (183), so a player who picks
"clear all duels" goal gets a meaningful rule even if they crank the slider
to max."""

from __future__ import annotations

from dataclasses import dataclass

from Options import Choice, DefaultOnToggle, PerGameCommonOptions, Range, Toggle


class CampaignMode(Choice):
    """How duel-unlock items are placed in the seed.

    sequential: each duel's Unlock is locked to the previous duel's Win
                location. Inside each series the chain runs slot 1 -> 2 -> ...
                so the player advances naturally; slot 1 of every series is
                pre-collected.
    shuffled:   all 183 unlock items go into the AP pool and the fill
                algorithm scatters them. Slot-1 unlocks are still pre-collected
                so every series tab has at least one playable duel at start.
                (`random` is a reserved key in AP options — using `shuffled`.)"""

    display_name = "Campaign Mode"
    option_sequential = 0
    option_shuffled = 1
    default = 0


class GoalMode(Choice):
    """Win condition for the seed.

    any_series_finale: complete the highest-numbered duel of any one of the
                       6 series (YuGiOh #32, GX #32, 5Ds #32, ZEXAL #26,
                       ARC-V #33, or VRAINS #30).
    duel_count:        complete N duels in any combination, where N is set by
                       `goal_duel_count`."""

    display_name = "Goal"
    option_any_series_finale = 0
    option_duel_count = 1
    default = 0


class GoalDuelCount(Range):
    """Number of duels required to win when goal=duel_count. Capped at 175
    (the total named campaign duels in the game)."""

    display_name = "Goal Duel Count"
    range_start = 2
    range_end = 175
    default = 30


class HideDefaultCards(DefaultOnToggle):
    """If on, the client writes 0 to the default-structure-deck-cards static
    address at session start so the deck-edit trunk only reflects AP-granted
    cards. Otherwise the player keeps the game's starter cards on top of
    whatever AP grants. Runtime-only, reverts on game restart."""

    display_name = "Hide Default Cards"


class StapleFillerCount(Range):
    """Number of staple-tagged cards to add to the AP item pool as filler.

    Drawn at random (without replacement) from the ~200 staples in
    card_list.py. Set to 0 to disable staple filler entirely (DP filler will
    pad the remaining slots)."""

    display_name = "Staple Filler Count"
    range_start = 0
    range_end = 200
    default = 100


class DPFillerCount(Range):
    """Number of Duel Points filler items to add to the pool. Each slot picks
    randomly from {1000, 5000, 10000} DP — smaller amounts are pointless given
    that even budget shop packs cost a few hundred DP."""

    display_name = "DP Filler Count"
    range_start = 0
    range_end = 200
    default = 50


@dataclass
class YGOLotDOptions(PerGameCommonOptions):
    campaign_mode: CampaignMode
    goal: GoalMode
    goal_duel_count: GoalDuelCount
    hide_default_cards: HideDefaultCards
    staple_filler_count: StapleFillerCount
    dp_filler_count: DPFillerCount
