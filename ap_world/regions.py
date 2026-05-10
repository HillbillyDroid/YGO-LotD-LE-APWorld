"""Region graph for YGO LotD-LE AP world.

One region per campaign series + a Menu region. The Menu connects
unconditionally to all 6 series; per-duel access is enforced at the location
level (see rules.py), not at the region level.

Series regions are named after `SERIES_NAMES` from `duel_table.py`:
YuGiOh, GX, 5Ds, ZEXAL, ARC-V, VRAINS.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from BaseClasses import Region

from .data.duel_table import SERIES_NAMES

if TYPE_CHECKING:
    from .world import YGOLotDWorld


MENU_REGION = "Menu"


def create_and_connect_regions(world: YGOLotDWorld) -> None:
    menu = Region(MENU_REGION, world.player, world.multiworld)
    series_regions = [Region(name, world.player, world.multiworld) for name in SERIES_NAMES]
    world.multiworld.regions += [menu, *series_regions]

    for sr in series_regions:
        menu.connect(sr, f"{MENU_REGION} -> {sr.name}")
