"""Polls campaign-duel state and emits AP location ids on first-clear.

Per PLAN, the canonical "duel completed" signal is a `Duel.State` rising
edge into `3` (Complete). Live testing 2026-05-05 showed no `2 -> 3`
event when winning a fresh duel — the state appears to jump straight from
`1` (Available) to `3`, skipping `2` (AvailableAttempted). The watcher
now fires on any rising edge into `3` (`prev != 3 and cur == 3`),
covering both `1 -> 3` and the source-documented `2 -> 3` path.

We rely on the AP server's `checked_locations` to dedup: a duel that's
already at `3` when the client connects records its baseline state on
the first tick (and emits nothing), so reconnecting mid-session won't
re-fire checks for already-cleared duels.

This module does NOT call into AP at all. It just returns the new location
ids from `tick`; the calling client decides what to do with them."""
from __future__ import annotations

from ..data.duel_table import DUELS, is_active_duel
from ..locations import DUEL_TO_LOCATIONS
from .memory import MemoryHandle


# (series, slot) pairs that the AP world tracks as locations. Excludes
# VRAINS slot-12/18 holes and tutorial slots (slot 1 of each series).
NAMED_DUELS: list[tuple[int, int]] = [
    (e["series"], e["slot"]) for e in DUELS if is_active_duel(e)
]


class DuelWatcher:
    def __init__(self, mem: MemoryHandle) -> None:
        self.mem = mem
        # last observed `Duel.State` value, keyed by (series, slot). None
        # before the first read.
        self.last_state: dict[tuple[int, int], int] = {}

    def reset(self) -> None:
        self.last_state.clear()

    def tick(self) -> list[int]:
        """Read every named duel's state once. Return AP location ids for
        any (series, slot) whose state had a rising edge into `3`
        (Complete) since the previous tick — i.e. `prev != 3 and cur == 3`.

        On the very first tick we just record the current state without
        emitting anything; this prevents the connect-time reconciliation
        from re-triggering checks for duels already at `Complete`. The
        AP server is the source of truth for which Win locs were already
        checked, and it sends those back on connect via `checked_locations`."""
        new_locations: list[int] = []
        first_pass = not self.last_state
        for key in NAMED_DUELS:
            series, slot = key
            duel = self.mem.read_campaign_duel(series, slot)
            cur = duel.state
            prev = self.last_state.get(key)
            if not first_pass and prev is not None and prev != 3 and cur == 3:
                info = DUEL_TO_LOCATIONS[key]
                new_locations.append(info["win"])
                new_locations.append(info["bonus"])
            self.last_state[key] = cur
        return new_locations
