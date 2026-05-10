"""Location-id allocation for the YGO LotD-LE AP world.

ID space layout (BASE = 0):
  - 20001..20800   duel-completion locations. Two per named campaign duel:
                   "<duel name> Win"           -> base + 2*offset
                   "<duel name> First Clear Bonus" -> base + 2*offset + 1
                   183 named duels * 2 = 366 locations actually used.

Skipped slots (VRAINS 12, 18 — no name in dueldata) emit no locations.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from BaseClasses import Location

from .data.duel_table import DUELS, SERIES_NAMES, is_active_duel

if TYPE_CHECKING:
    from .world import YGOLotDWorld


BASE = 0
DUEL_LOC_ID_RANGE = (BASE + 20001, BASE + 20800)


# Build location maps. We also expose per-(series, slot) lookups so
# duel_watcher.py and item_applier.py can map between AP location ids and
# in-memory CampaignSaveData state slots.
LOCATION_NAME_TO_ID: dict[str, int] = {}

# (series, slot) -> {"win": loc_id, "bonus": loc_id, "name": str}
DUEL_TO_LOCATIONS: dict[tuple[int, int], dict] = {}

# loc_id -> (series, slot, kind)  where kind in {"win", "bonus"}
LOC_ID_TO_DUEL: dict[int, tuple[int, int, str]] = {}

_offset = 0
for _entry in DUELS:
    if not is_active_duel(_entry):
        continue
    _name = _entry["name"]
    _series = _entry["series"]
    _slot = _entry["slot"]
    _win_id = DUEL_LOC_ID_RANGE[0] + _offset * 2
    _bonus_id = _win_id + 1
    assert _bonus_id <= DUEL_LOC_ID_RANGE[1], (
        f"location id {_bonus_id} exceeds reserved range {DUEL_LOC_ID_RANGE}"
    )
    _win_loc = f"{_name} Win"
    _bonus_loc = f"{_name} First Clear Bonus"
    assert _win_loc not in LOCATION_NAME_TO_ID, f"location collision: {_win_loc!r}"
    assert _bonus_loc not in LOCATION_NAME_TO_ID, f"location collision: {_bonus_loc!r}"
    LOCATION_NAME_TO_ID[_win_loc] = _win_id
    LOCATION_NAME_TO_ID[_bonus_loc] = _bonus_id
    DUEL_TO_LOCATIONS[(_series, _slot)] = {
        "win": _win_id,
        "bonus": _bonus_id,
        "name": _name,
    }
    LOC_ID_TO_DUEL[_win_id] = (_series, _slot, "win")
    LOC_ID_TO_DUEL[_bonus_id] = (_series, _slot, "bonus")
    _offset += 1
del _offset, _entry, _name, _series, _slot, _win_id, _bonus_id, _win_loc, _bonus_loc


# Sanity checks.
_ids = list(LOCATION_NAME_TO_ID.values())
assert len(_ids) == len(set(_ids)), "duplicate location id"
assert len(LOCATION_NAME_TO_ID) == 2 * len(DUEL_TO_LOCATIONS), "win/bonus count mismatch"
del _ids


class YGOLotDLocation(Location):
    game = "YGO LotD-LE"


def create_all_locations(world: YGOLotDWorld) -> None:
    """Add every duel location to its series region."""
    region_by_series = {
        i: world.get_region(SERIES_NAMES[i]) for i in range(6)
    }
    for (series, slot), info in DUEL_TO_LOCATIONS.items():
        region = region_by_series[series]
        for kind in ("win", "bonus"):
            loc_id = info[kind]
            loc_name = (
                info["name"] + " Win" if kind == "win"
                else info["name"] + " First Clear Bonus"
            )
            region.locations.append(
                YGOLotDLocation(world.player, loc_name, loc_id, region)
            )
