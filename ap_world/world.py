"""YGOLotDWorld: the AP World subclass."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from worlds.AutoWorld import World

from . import items, locations, regions, rules
from . import options as ygo_options
from .data.duel_table import DUELS, is_active_duel
from .items import (
    CARD_ITEM_TO_INDEX,
    DP_ITEM_AMOUNTS,
    PACK_ITEM_TO_BIT,
    YGOLotDItem,
    create_item as _create_item,
    get_filler_item_name as _get_filler_item_name,
)


class YGOLotDWorld(World):
    """Yu-Gi-Oh! Legacy of the Duelist Link Evolution randomizer.

    Locations are wins (and first-clear bonuses) on the 183 named campaign
    duels across 6 series. Items are duel unlocks (progression), shop pack
    unlocks, DP grants, individual cards (staple-tagged in v1), and a Victory
    token. The client attaches via pymem and reconciles AP state with the
    game's in-memory save data each tick."""

    game = "YGO LotD-LE"

    options_dataclass = ygo_options.YGOLotDOptions
    options: ygo_options.YGOLotDOptions

    item_name_to_id = items.ITEM_NAME_TO_ID
    location_name_to_id = locations.LOCATION_NAME_TO_ID

    origin_region_name = regions.MENU_REGION

    def create_regions(self) -> None:
        regions.create_and_connect_regions(self)
        locations.create_all_locations(self)

    def set_rules(self) -> None:
        rules.set_all_rules(self)
        rules.place_sequential_unlocks(self)

    def create_items(self) -> None:
        items.create_all_items(self)

    def create_item(self, name: str) -> YGOLotDItem:
        return _create_item(self, name)

    def get_filler_item_name(self) -> str:
        return _get_filler_item_name(self)

    def fill_slot_data(self) -> Mapping[str, Any]:
        duel_locations = []
        for entry in DUELS:
            if not is_active_duel(entry):
                continue
            info = locations.DUEL_TO_LOCATIONS[(entry["series"], entry["slot"])]
            duel_locations.append({
                "series": entry["series"],
                "slot": entry["slot"],
                "name": entry["name"],
                "win_loc": info["win"],
                "bonus_loc": info["bonus"],
                "opponent_deck_id": entry["opponent_deck_id"],
            })

        return {
            "campaign_mode": self.options.campaign_mode.current_key,
            "goal_mode": self.options.goal.current_key,
            "goal_duel_count": int(self.options.goal_duel_count.value),
            "hide_default_cards": bool(self.options.hide_default_cards.value),
            "duel_locations": duel_locations,
            "card_item_to_index": dict(CARD_ITEM_TO_INDEX),
            "pack_item_to_bit": {
                name: list(bit_info) for name, bit_info in PACK_ITEM_TO_BIT.items()
            },
            "dp_item_amounts": dict(DP_ITEM_AMOUNTS),
        }
