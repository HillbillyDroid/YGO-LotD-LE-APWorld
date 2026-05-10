"""Applies a received AP item to the running game's memory.

Dispatch is keyed by `slot_data` lookups built in `world.fill_slot_data`:
  - `pack_item_to_bit`:  name -> ("shop"|"dlc", bit_index)
  - `card_item_to_index`: name -> internal card index
  - `dp_item_amounts`:    name -> int amount
Plus duel-unlock items, which are name-derived: any item ending in
" Unlock" maps to a (series, slot) lookup via `DUEL_NAME_TO_KEY` below.

All non-DP applies are idempotent and never lower existing state. DP is
NOT idempotent (it's a currency credit) — the caller guards it with a set
of already-applied `items_received` indices and persists the count in
slot-storage.
"""
from __future__ import annotations

from ..data.duel_table import DUELS, is_active_duel
from .memory import MemoryHandle


# Built once at import: "<duel name> Unlock" -> (series, slot). Tutorials
# and VRAINS gaps excluded — no AP item exists for them.
DUEL_NAME_TO_KEY: dict[str, tuple[int, int]] = {
    f"{e['name']} Unlock": (e["series"], e["slot"])
    for e in DUELS
    if is_active_duel(e)
}


class ItemApplier:
    def __init__(
        self,
        mem: MemoryHandle,
        pack_item_to_bit: dict[str, tuple[str, int]],
        card_item_to_index: dict[str, int],
        dp_item_amounts: dict[str, int],
    ) -> None:
        self.mem = mem
        self.pack_item_to_bit = pack_item_to_bit
        self.card_item_to_index = card_item_to_index
        self.dp_item_amounts = dp_item_amounts

    def apply(self, name: str, *, credit_dp: bool = True) -> None:
        """Apply the item with `name`. Idempotent for everything except DP;
        DP is only credited when `credit_dp` is True (the caller passes
        False to re-run history during reconcile without double-spending)."""
        if name == "Victory":
            return  # purely virtual; the world's completion_condition handles it.

        if name in self.pack_item_to_bit:
            field, bit = self.pack_item_to_bit[name]
            mask = 1 << int(bit)
            if field == "shop":
                self.mem.or_unlocked_shop_packs(mask)
            elif field == "dlc":
                self.mem.or_unlocked_dlc_shop_packs(mask)
            else:
                raise ValueError(f"unknown pack field {field!r} for item {name!r}")
            return

        if name in DUEL_NAME_TO_KEY:
            series, slot = DUEL_NAME_TO_KEY[name]
            self.mem.raise_duel_state(series, slot, min_state=1)
            return

        if name in self.dp_item_amounts:
            if credit_dp:
                self.mem.add_dp(int(self.dp_item_amounts[name]))
            return

        if name in self.card_item_to_index:
            self.mem.raise_card_count(int(self.card_item_to_index[name]), min_count=1)
            return

        # Unknown item — silently skip rather than crash the loop. The client
        # logs at the call site.
        raise KeyError(name)
