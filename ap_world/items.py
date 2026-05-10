"""Item-id allocation and pool builder for the YGO LotD-LE AP world.

ID space layout (BASE = 0 — offsets per PLAN.md):
  - 1..15000          individual cards (id = card index from card_list.py).
                      Item *name* equals card *name*. v1 only places staple-
                      tagged cards as filler; the rest of the range is reserved
                      for v2 to fill in without renumbering.
  - 15001..15300      duel unlock items, "<duel name> Unlock". 183 of the
                      300 ids are actually allocated (one per named campaign
                      duel). Remaining IDs left as headroom.
  - 16001..16100      shop pack unlocks. 29 from UnlockedShopPacks (bits 1..30,
                      bit 24 removed — Pendulum pack disabled in LE-v2)
                      + 4 from UnlockedDlcShopPacks (bits 2..5) = 33 used.
  - 17001..17050      DP filler items: "1000 DP", "5000 DP", "10000 DP".
  - 18001             "Victory" — world's win-condition item.

All assertions on uniqueness run at module import time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from BaseClasses import Item, ItemClassification

from .data.card_list import BY_INDEX
from .data.duel_table import DUELS, is_active_duel

if TYPE_CHECKING:
    from .world import YGOLotDWorld


BASE = 0

CARD_ID_RANGE = (BASE + 1, BASE + 15000)
DUEL_UNLOCK_ID_RANGE = (BASE + 15001, BASE + 15300)
SHOP_PACK_ID_RANGE = (BASE + 16001, BASE + 16100)
DP_FILLER_ID_RANGE = (BASE + 17001, BASE + 17050)
VICTORY_ID = BASE + 18001


# --- Shop pack catalog ---------------------------------------------------
# Name + which bitfield + which bit within that field. Source: CLAUDE.md
# `UnlockedShopPacks bits` table + DLC bit mapping verified 2026-05-02. Bit 31
# ("Unknown" placeholder in source) intentionally skipped.
SHOP_PACKS: list[tuple[str, str, int]] = [
    ("Grandpa Muto Pack",       "shop", 1),
    ("Mai Valentine Pack",      "shop", 2),
    ("Bakura Pack",             "shop", 3),
    ("Joey Wheeler Pack",       "shop", 4),
    ("Seto Kaiba Pack",         "shop", 5),
    ("Yugi Pack",               "shop", 6),
    ("Alexis Rhodes Pack",      "shop", 7),
    ("Bastion Misawa Pack",     "shop", 8),
    ("Chazz Princeton Pack",    "shop", 9),
    ("Syrus Truesdale Pack",    "shop", 10),
    ("Jesse Anderson Pack",     "shop", 11),
    ("Jaden Yuki Pack",         "shop", 12),
    ("Tetsu Trudge Pack",       "shop", 13),
    ("Leo & Luna Pack",         "shop", 14),
    ("Akiza Izinski Pack",      "shop", 15),
    ("Jack Atlas Pack",         "shop", 16),
    ("Crow Pack",               "shop", 17),
    ("Yusei Fudo Pack",         "shop", 18),
    ("Cathy Katherine Pack",    "shop", 19),
    ("Quinton Pack",            "shop", 20),
    ("Kite Tenjo Pack",         "shop", 21),
    ("Shark Pack",              "shop", 22),
    ("Yuma Tsukumo Pack",       "shop", 23),
    # bit 24 ("Pendulum Pack" in source enum) confirmed 2026-05-06 to no longer
    # produce a shop pack in LE-v2. Removed from the AP item pool.
    ("Gong Strong Pack",        "shop", 25),
    ("Zuzu Boyle Pack",         "shop", 26),
    ("Shay Pack",               "shop", 27),
    ("Declan Akaba Pack",       "shop", 28),
    ("Yuya Sakaki Pack",        "shop", 29),
    ("Playmaker Pack",          "shop", 30),
    # DLC-spillover packs in UnlockedDlcShopPacks; bits 0,1 unmapped.
    ("Blue Angel Pack",         "dlc",  2),
    ("Soulburner Pack",         "dlc",  3),
    ("Varis Pack",              "dlc",  4),
    ("Ai Pack",                 "dlc",  5),
]


# --- DP filler catalog ---------------------------------------------------
DP_FILLER: list[tuple[str, int]] = [
    ("1000 DP",  1000),
    ("5000 DP",  5000),
    ("10000 DP", 10000),
]


# --- Build ITEM_NAME_TO_ID -----------------------------------------------
ITEM_NAME_TO_ID: dict[str, int] = {}

# Duel unlocks. Slot is 1-indexed in duel_table; we map (series, slot) order to
# an offset so generating the same duel always produces the same id. Tutorial
# slots (slot 1 of each series) and VRAINS gaps still consume an offset so the
# remaining ids stay stable when the active-duel set changes.
DUEL_UNLOCK_NAMES: list[str] = []  # ordered: series 0..5, slot ascending
_duel_offset = 0
for _entry in DUELS:
    if not is_active_duel(_entry):
        # VRAINS gap or tutorial — reserve the id slot, emit no item.
        _duel_offset += 1
        continue
    _name = f"{_entry['name']} Unlock"
    assert _name not in ITEM_NAME_TO_ID, f"duel-unlock collision: {_name!r}"
    ITEM_NAME_TO_ID[_name] = DUEL_UNLOCK_ID_RANGE[0] + _duel_offset
    DUEL_UNLOCK_NAMES.append(_name)
    _duel_offset += 1
assert _duel_offset <= 300, f"more duel-table rows ({_duel_offset}) than reserved id space (300)"
del _duel_offset, _entry, _name


# Shop packs. Build a name->(field, bit) sidecar for the client.
PACK_ITEM_TO_BIT: dict[str, tuple[str, int]] = {}
for _i, (_pack_name, _field, _bit) in enumerate(SHOP_PACKS):
    assert _pack_name not in ITEM_NAME_TO_ID, f"shop-pack collision: {_pack_name!r}"
    ITEM_NAME_TO_ID[_pack_name] = SHOP_PACK_ID_RANGE[0] + _i
    PACK_ITEM_TO_BIT[_pack_name] = (_field, _bit)
del _i, _pack_name, _field, _bit


# DP filler.
DP_ITEM_AMOUNTS: dict[str, int] = {}
for _i, (_dp_name, _amount) in enumerate(DP_FILLER):
    assert _dp_name not in ITEM_NAME_TO_ID, f"dp-filler collision: {_dp_name!r}"
    ITEM_NAME_TO_ID[_dp_name] = DP_FILLER_ID_RANGE[0] + _i
    DP_ITEM_AMOUNTS[_dp_name] = _amount
del _i, _dp_name, _amount


# Victory.
ITEM_NAME_TO_ID["Victory"] = VICTORY_ID


# Staple cards. Item *name* = card *name*; id = BASE + card index. Multiple
# cards with the same name are skipped after the first.
CARD_ITEM_TO_INDEX: dict[str, int] = {}
STAPLE_ITEM_NAMES: list[str] = []
for _card in BY_INDEX.values():
    if not any(t.startswith("staple_") for t in _card["tags"]):
        continue
    _name = _card["name"]
    if _name in ITEM_NAME_TO_ID:
        continue
    _id = BASE + _card["index"]
    assert CARD_ID_RANGE[0] <= _id <= CARD_ID_RANGE[1], (
        f"card index {_card['index']} outside reserved id range {CARD_ID_RANGE}"
    )
    ITEM_NAME_TO_ID[_name] = _id
    CARD_ITEM_TO_INDEX[_name] = _card["index"]
    STAPLE_ITEM_NAMES.append(_name)
del _card, _name, _id


# --- Sanity asserts ------------------------------------------------------
_ids = list(ITEM_NAME_TO_ID.values())
assert len(_ids) == len(set(_ids)), "duplicate item id detected"
del _ids


# --- Item subclass -------------------------------------------------------
class YGOLotDItem(Item):
    game = "YGO LotD-LE"


# Items that gate progression (used by `set_rules` and the AP fill algorithm).
PROGRESSION_ITEM_NAMES: frozenset[str] = frozenset(
    list(DUEL_UNLOCK_NAMES) + ["Victory"]
)

# Items that give meaningful but non-progression value.
USEFUL_ITEM_NAMES: frozenset[str] = frozenset(PACK_ITEM_TO_BIT.keys())


def classify(name: str) -> ItemClassification:
    if name in PROGRESSION_ITEM_NAMES:
        return ItemClassification.progression
    if name in USEFUL_ITEM_NAMES:
        return ItemClassification.useful
    return ItemClassification.filler


def create_item(world: YGOLotDWorld, name: str) -> YGOLotDItem:
    return YGOLotDItem(name, classify(name), ITEM_NAME_TO_ID[name], world.player)


def get_filler_item_name(world: YGOLotDWorld) -> str:
    """AP calls this when it needs an extra filler. Prefer DP grants — staple
    cards are limited (one of each), DP can repeat freely."""
    return world.random.choice([name for name, _ in DP_FILLER])


def create_all_items(world: YGOLotDWorld) -> None:
    """Build the item pool.

    Roster:
      - 6 series-slot-2 unlocks pre-collected (slot 1 is the in-game tutorial,
        excluded from AP) so every series tab is immediately playable.
      - 1 random shop pack pre-collected as starter so deck-building works.
      - 6000 DP starter: 5000 + 1000 DP pre-collected.
      - Remaining duel-unlock items in the pool.
      - All shop packs not pre-collected (33 of 34).
      - N staple cards from `staple_filler_count`.
      - M DP items from `dp_filler_count`.
      - 1 Victory placed by `place_victory` per goal rule.
      - DP padding for any slack between pool size and location count.
    """
    starter_unlock_names: list[str] = []
    for series_idx in range(6):
        for entry in DUELS:
            if (
                entry["series"] == series_idx
                and entry["slot"] == 2
                and is_active_duel(entry)
            ):
                starter_unlock_names.append(f"{entry['name']} Unlock")
                break
    for name in starter_unlock_names:
        world.push_precollected(create_item(world, name))

    starter_pack = world.random.choice(list(PACK_ITEM_TO_BIT.keys()))
    world.push_precollected(create_item(world, starter_pack))

    world.push_precollected(create_item(world, "5000 DP"))
    world.push_precollected(create_item(world, "1000 DP"))

    place_victory(world)

    from .rules import sequentially_placed_unlock_names

    pool: list[YGOLotDItem] = []

    starter_unlock_set = set(starter_unlock_names)
    seq_placed = sequentially_placed_unlock_names(world)
    for name in DUEL_UNLOCK_NAMES:
        if name in starter_unlock_set:
            continue
        if name in seq_placed:
            # Locked onto the previous duel's Win location by place_sequential_unlocks.
            continue
        pool.append(create_item(world, name))

    for pack_name in PACK_ITEM_TO_BIT.keys():
        if pack_name == starter_pack:
            continue
        pool.append(create_item(world, pack_name))

    staple_count = int(world.options.staple_filler_count.value)
    staples = list(STAPLE_ITEM_NAMES)
    world.random.shuffle(staples)
    for name in staples[:staple_count]:
        pool.append(create_item(world, name))

    dp_count = int(world.options.dp_filler_count.value)
    dp_names = [name for name, _ in DP_FILLER]
    for _ in range(dp_count):
        pool.append(create_item(world, world.random.choice(dp_names)))

    unfilled = len(world.multiworld.get_unfilled_locations(world.player))
    deficit = unfilled - len(pool)
    while deficit > 0:
        pool.append(create_item(world, world.random.choice(dp_names)))
        deficit -= 1
    excess = len(pool) - unfilled
    if excess > 0:
        droppable_names = set(STAPLE_ITEM_NAMES) | {name for name, _ in DP_FILLER}
        i = len(pool) - 1
        while excess > 0 and i >= 0:
            if pool[i].name in droppable_names:
                del pool[i]
                excess -= 1
            i -= 1
        if excess > 0:
            raise RuntimeError(
                f"item pool overflow: {excess} items beyond unfilled locations after "
                f"trimming all filler/staples"
            )

    world.multiworld.itempool += pool


def place_victory(world: YGOLotDWorld) -> None:
    """Lock the Victory item to a location chosen by the goal rule."""
    from .rules import goal_victory_locations

    target = world.random.choice(goal_victory_locations(world))
    location = world.get_location(target)
    location.place_locked_item(create_item(world, "Victory"))
