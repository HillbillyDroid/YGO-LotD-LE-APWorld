"""Re-asserts AP-owned items against game memory every tick.

Per PLAN §Item-state sync, with two deviations:

  - Duel unlocks: AUTHORITATIVE — completion-preserving. The game itself
    auto-raises the next slot's `Duel.State` to 1 when you finish a duel,
    which (in random mode) would let the player play un-AP-granted duels.
    So every tick we raise owned duels to >=1 and lower un-owned duels to
    0 (Locked) UNLESS already at 3 (Complete) — keeping prior location
    checks intact. Placeholder slot 0 of every series is also held at
    >=1 (UI prerequisite for the series tab to be clickable).
  - Card grants: additive only. Raise card-byte counts to >=1 for owned
    card items; never lower (player-deck-bound copies persist).
  - Shop packs: AUTHORITATIVE OVERWRITE. The game's own campaign
    progression unlocks shop packs game-side (e.g. clearing certain duels
    pops a free Yugi pack into the shop), and we want the shop to reflect
    only AP-granted packs. So every tick we compute the exact target mask
    from owned pack items and write it verbatim — clearing any
    game-progression-set bit that wasn't AP-granted.
  - DP: skipped entirely. Spendable currency; the client handles credit
    via applied_dp_count + applied_dp_indices.

Process-gone errors (game exited, memory unreadable) propagate out of
`tick`; the caller (ygo_client) catches them and triggers a reattach.

Usage:

    enforcer = SyncEnforcer(mem, applier)
    enforcer.tick(item_names)   # idempotent; item_names = list of names
                                #   from items_received
"""
from __future__ import annotations

import pymem.exception

from ..data.duel_table import DUELS, is_active_duel
from .item_applier import DUEL_NAME_TO_KEY, ItemApplier
from .memory import MemoryHandle


# Errors that mean the game process is unreachable. These re-raise out of
# `tick` so the live loop can drop the pymem handle and reattach.
_PROCESS_GONE_ERRORS: tuple[type[BaseException], ...] = (
    pymem.exception.MemoryReadError,
    pymem.exception.MemoryWriteError,
    pymem.exception.WinAPIError,
    pymem.exception.ProcessError,
    OSError,
)


# All (series, slot) pairs we care about authoritatively syncing. Tutorials
# (slot 1) and VRAINS gaps are deliberately excluded — they have no AP item,
# and slot 1 stays Locked per the tutorial-skip design.
_NAMED_DUEL_KEYS: list[tuple[int, int]] = [
    (e["series"], e["slot"]) for e in DUELS if is_active_duel(e)
]


class SyncEnforcer:
    def __init__(self, mem: MemoryHandle, applier: ItemApplier) -> None:
        self.mem = mem
        self.applier = applier

    def tick(self, owned_item_names: list[str]) -> None:
        """Re-assert every owned item. Errors on a single item are caught
        so one bad name doesn't break enforcement for the rest of the set."""
        owned_set = set(owned_item_names)

        # 1) Compute exact target masks for the two pack bitfields from
        #    owned pack items. This OVERWRITES game state, intentionally.
        shop_target = 0
        dlc_target = 0
        for name, (field, bit) in self.applier.pack_item_to_bit.items():
            if name not in owned_set:
                continue
            mask = 1 << int(bit)
            if field == "shop":
                shop_target |= mask
            elif field == "dlc":
                dlc_target |= mask
        try:
            self.mem.set_unlocked_shop_packs(shop_target)
        except _PROCESS_GONE_ERRORS:
            raise
        except Exception:
            pass
        try:
            self.mem.set_unlocked_dlc_shop_packs(dlc_target)
        except _PROCESS_GONE_ERRORS:
            raise
        except Exception:
            pass

        # 2) Authoritative duel sync. Owned -> raise to >=1; un-owned -> lock
        #    unless already complete. Placeholder slot 0 always >=1 (UI).
        owned_duel_keys: set[tuple[int, int]] = set()
        for name in owned_set:
            key = DUEL_NAME_TO_KEY.get(name)
            if key is not None:
                owned_duel_keys.add(key)
        for series_idx in range(6):
            try:
                self.mem.raise_duel_state(series_idx, slot=0, min_state=1)
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception:
                pass
        for key in _NAMED_DUEL_KEYS:
            series, slot = key
            try:
                if key in owned_duel_keys:
                    self.mem.raise_duel_state(series, slot, min_state=1)
                else:
                    self.mem.lock_duel_unless_complete(series, slot)
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception:
                pass

        # 3) Additive re-apply for cards (and a no-op pass over packs/duels
        #    we already wrote authoritatively above; keeps the loop uniform).
        for name in owned_item_names:
            try:
                self.applier.apply(name, credit_dp=False)
            except KeyError:
                continue
