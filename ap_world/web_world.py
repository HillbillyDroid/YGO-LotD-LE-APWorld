"""WebWorld config — how the apworld appears on the AP website."""

from __future__ import annotations

from BaseClasses import Tutorial
from worlds.AutoWorld import WebWorld


class YGOLotDWebWorld(WebWorld):
    game = "YGO LotD-LE"

    theme = "dirt"

    setup_en = Tutorial(
        "Multiworld Setup Guide",
        "A guide to setting up Yu-Gi-Oh! Legacy of the Duelist: Link Evolution for MultiWorld.",
        "English",
        "setup_en.md",
        "setup/en",
        ["SolomonW"],
    )

    tutorials = [setup_en]
