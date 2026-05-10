"""Pymem wrapper for the YGO LotD-LE AP client.

All static addresses + offsets ported from `debug_tools/unlock_shop_pack.py`,
`debug_tools/campaign_probe.py`, and `debug_tools/card_grant.py`. See the
"Lotd LE v2 — Memory layout" section of `CLAUDE.md` for the source
references.

Typical lifecycle:

    handle = MemoryHandle()
    if not handle.attach():           # (1) find Lotd process
        ...
    if not handle.wait_for_save_data():# (2) walk the save-data pointer chain
        ...                            #     until 4 readiness conditions hold
    handle.write_unlocked_content_all()# (3..) per-tick reads/writes

`try_resolve_save_data()` is idempotent and cheap; the AP client calls it
both during `wait_for_save_data` and again before any read/write because
the underlying pointers move when the player switches saves.

Save-data pointer-chain resolution checks four readiness conditions:
  1. `saveDataAddress1` deref returns non-null.
  2. The whole chain walk completes without nulls.
  3. The 16-byte miscData header reads identically across two ticks
     (so we don't catch a half-loaded state).
  4. The active-slot id at saveDataAddress1+176 is >= 0.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass

import pymem


# --- Static addresses (from CLAUDE.md / pixeltris MemTools.Addresses.LotdLE_v2) ---
SAVE_DATA_ADDRESS_1 = 0x142924010
SAVE_DATA_MEM_OFFSET = 80
SAVE_DATA_ADDRESS_OFFSET = 10208
MISC_DATA_OFFSET_LE2 = 4056
NUM_DECK_DATA_SLOTS_LE2 = 700
CAMPAIGN_DATA_OFFSET_LE2 = 7024
CARD_LIST_OFFSET_LE2 = 24008
CARD_LIST_SIZE_LE2 = 20000
DUEL_POINTS_OFFSET_IN_MISC = 16
DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2 = 0x140A6C818

# UnlockedShopPacks lives at the end of MiscSaveData, after the recipe bitfield.
RECIPE_BYTES_LE2 = (NUM_DECK_DATA_SLOTS_LE2 + 7) // 8
UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC = (
    16 + 8 + 32 + (4 * NUM_DECK_DATA_SLOTS_LE2) + RECIPE_BYTES_LE2
)
assert UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC == 2944, (
    "MiscSaveData layout drift — verify against CLAUDE.md"
)
UNLOCKED_DLC_SHOP_PACKS_OFFSET_IN_MISC = UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC + 4
UNLOCKED_CONTENT_OFFSET_IN_MISC = UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC + 20

# CampaignSaveData layout
DUELS_PER_SERIES = 50
DUEL_STRUCT_SIZE = 24
SERIES_HEADER_SIZE = 8
CAMPAIGN_HEADER_SIZE = 8

DP_MAX = 9_999_999_999
UNLOCKED_CONTENT_ALL = 0x7

CANDIDATE_EXE_NAMES = ["Lotd.exe", "LotdLE.exe", "YuGiOh.exe", "Yu-Gi-Oh!.exe"]


# --- Win32 plumbing for write_int_force ----------------------------------
_PAGE_READWRITE = 0x04


def _virtual_protect_ex(handle: int, addr: int, size: int, new_protect: int) -> int:
    fn = ctypes.windll.kernel32.VirtualProtectEx
    fn.argtypes = [
        wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)
    ]
    fn.restype = wt.BOOL
    old = wt.DWORD(0)
    if not fn(handle, ctypes.c_void_p(addr), size, new_protect, ctypes.byref(old)):
        raise OSError(f"VirtualProtectEx failed (GetLastError={ctypes.get_last_error()})")
    return old.value


@dataclass
class DuelState:
    state: int
    reverse_state: int
    unk1: int
    unk2: int
    unk3: int
    unk4: int


class SaveDataNotReady(RuntimeError):
    """Raised when a read/write is attempted before save-data is resolved."""


class MemoryHandle:
    """Owns the pymem connection + cached save-data base pointer."""

    def __init__(self) -> None:
        self.pm: pymem.Pymem | None = None
        self.process_name: str | None = None
        self._save_data: int | None = None
        # The first ready-check tick records the misc-header bytes; the second
        # confirms they're still the same. Reset on every attach / disconnect.
        self._last_misc_header: bytes | None = None

    # --- attach / readiness -------------------------------------------

    def attach(self) -> bool:
        """Find the Lotd process. Does NOT resolve save data."""
        for name in CANDIDATE_EXE_NAMES:
            try:
                pm = pymem.Pymem(name)
            except Exception:
                continue
            self.pm = pm
            self.process_name = name
            return True
        self.pm = None
        self.process_name = None
        return False

    def detach(self) -> None:
        self.pm = None
        self.process_name = None
        self._save_data = None
        self._last_misc_header = None

    def try_resolve_save_data(self) -> bool:
        """Walk the save-data pointer chain. Caches `_save_data` on success.

        Returns True only when all four readiness conditions in the module
        docstring hold."""
        if self.pm is None:
            return False
        try:
            v2 = self.pm.read_int(SAVE_DATA_ADDRESS_1 + 176)
            if v2 < 0:
                # condition 4: an active slot must be loaded
                return False

            v3 = self.pm.read_ulonglong(SAVE_DATA_ADDRESS_1)
            if v3 == 0:
                return False
            v4 = self.pm.read_ulonglong(v3 + 8)
            if v4 == 0:
                return False
            v5 = v3
            for _ in range(1024):
                leaf = self.pm.read_uchar(v4 + 25)
                if leaf != 0:
                    break
                if self.pm.read_int(v4 + 32) >= v2:
                    v5 = v4
                    v4 = self.pm.read_ulonglong(v4)
                else:
                    v4 = self.pm.read_ulonglong(v4 + 16)
                if v4 == 0:
                    return False
            else:
                return False  # tree walk did not terminate

            if v5 == v3 or v2 < self.pm.read_int(v5 + 32):
                v5 = v3

            result = self.pm.read_ulonglong(v5 + 40)
            if result == 0:
                return False
            base = self.pm.read_ulonglong(result + SAVE_DATA_MEM_OFFSET)
            if base == 0:
                return False
            save = self.pm.read_ulonglong(base + SAVE_DATA_ADDRESS_OFFSET)
            if save == 0:
                return False
        except Exception:
            return False

        # Two-tick stability check on the 16-byte miscData header.
        try:
            header = self.pm.read_bytes(save + MISC_DATA_OFFSET_LE2, 16)
        except Exception:
            return False
        if self._last_misc_header is None:
            # First successful walk — record and require a confirming tick.
            self._last_misc_header = header
            return False
        if header != self._last_misc_header:
            self._last_misc_header = header
            return False

        self._save_data = save
        return True

    def wait_for_save_data(
        self,
        timeout_seconds: float = 60.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Block until `try_resolve_save_data` returns True or timeout. Reset
        the two-tick stability state on entry."""
        self._last_misc_header = None
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.try_resolve_save_data():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval)

    @property
    def save_data(self) -> int:
        if self._save_data is None:
            raise SaveDataNotReady("save data not resolved — call wait_for_save_data first")
        return self._save_data

    # --- low-level helpers --------------------------------------------

    def _misc_addr(self, offset: int) -> int:
        return self.save_data + MISC_DATA_OFFSET_LE2 + offset

    def _duel_offset(self, series_idx: int, duel_idx: int) -> int:
        base = CAMPAIGN_HEADER_SIZE + series_idx * (
            DUEL_STRUCT_SIZE * DUELS_PER_SERIES + SERIES_HEADER_SIZE
        )
        if duel_idx == 0:
            return base
        return base + DUEL_STRUCT_SIZE + SERIES_HEADER_SIZE + (duel_idx - 1) * DUEL_STRUCT_SIZE

    def _duel_addr(self, series_idx: int, duel_idx: int) -> int:
        return self.save_data + CAMPAIGN_DATA_OFFSET_LE2 + self._duel_offset(series_idx, duel_idx)

    # --- reads --------------------------------------------------------

    def read_misc(self, offset: int, size: int) -> bytes:
        return self.pm.read_bytes(self._misc_addr(offset), size)

    def read_unlocked_shop_packs(self) -> int:
        return self.pm.read_uint(self._misc_addr(UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC))

    def read_unlocked_dlc_shop_packs(self) -> int:
        return self.pm.read_uint(self._misc_addr(UNLOCKED_DLC_SHOP_PACKS_OFFSET_IN_MISC))

    def read_unlocked_content(self) -> int:
        return self.pm.read_int(self._misc_addr(UNLOCKED_CONTENT_OFFSET_IN_MISC))

    def read_dp(self) -> int:
        return self.pm.read_longlong(self._misc_addr(DUEL_POINTS_OFFSET_IN_MISC))

    def read_campaign_duel(self, series_idx: int, slot: int) -> DuelState:
        """slot is the 1-indexed display slot from duel_table. Verified
        empirically 2026-05-05: the in-memory `CampaignSaveData.Duel[]`
        array has a placeholder entry at index 0 (no opponent_deck_id, no
        name in dueldata), so the named duels start at array index 1.
        Mapping: array_index = slot."""
        addr = self._duel_addr(series_idx, slot)
        return DuelState(
            state=self.pm.read_int(addr),
            reverse_state=self.pm.read_int(addr + 4),
            unk1=self.pm.read_int(addr + 8),
            unk2=self.pm.read_int(addr + 12),
            unk3=self.pm.read_int(addr + 16),
            unk4=self.pm.read_int(addr + 20),
        )

    def read_card_byte(self, index: int) -> int:
        if not (0 <= index < CARD_LIST_SIZE_LE2):
            raise ValueError(f"card index {index} out of range")
        return self.pm.read_uchar(self.save_data + CARD_LIST_OFFSET_LE2 + index)

    def read_card_count(self, index: int) -> int:
        return self.read_card_byte(index) & 0x07

    # --- writes (additive only — never lower / clear) -----------------

    def or_unlocked_shop_packs(self, mask: int) -> None:
        addr = self._misc_addr(UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC)
        cur = self.pm.read_uint(addr)
        if (cur & mask) == mask:
            return
        self.pm.write_uint(addr, cur | mask)

    def or_unlocked_dlc_shop_packs(self, mask: int) -> None:
        addr = self._misc_addr(UNLOCKED_DLC_SHOP_PACKS_OFFSET_IN_MISC)
        cur = self.pm.read_uint(addr)
        if (cur & mask) == mask:
            return
        self.pm.write_uint(addr, cur | mask)

    def set_unlocked_shop_packs(self, mask: int) -> None:
        """Overwrite UnlockedShopPacks to *exactly* `mask`. Used by the sync
        enforcer to keep shop unlocks in lockstep with AP items — campaign
        progression unlocks shop packs game-side, and we want only the
        AP-granted set to appear in the shop."""
        addr = self._misc_addr(UNLOCKED_SHOP_PACKS_OFFSET_IN_MISC)
        cur = self.pm.read_uint(addr)
        target = mask & 0xFFFFFFFF
        if cur == target:
            return
        self.pm.write_uint(addr, target)

    def set_unlocked_dlc_shop_packs(self, mask: int) -> None:
        """Overwrite UnlockedDlcShopPacks to exactly `mask`. See
        `set_unlocked_shop_packs` for the rationale."""
        addr = self._misc_addr(UNLOCKED_DLC_SHOP_PACKS_OFFSET_IN_MISC)
        cur = self.pm.read_uint(addr)
        target = mask & 0xFFFFFFFF
        if cur == target:
            return
        self.pm.write_uint(addr, target)

    def write_unlocked_content_all(self) -> None:
        """OR `0x7` into UnlockedContent so Card Shop / Battle Pack /
        Duelist Challenges menus are accessible. Idempotent."""
        addr = self._misc_addr(UNLOCKED_CONTENT_OFFSET_IN_MISC)
        cur = self.pm.read_int(addr)
        if (cur & UNLOCKED_CONTENT_ALL) == UNLOCKED_CONTENT_ALL:
            return
        self.pm.write_int(addr, cur | UNLOCKED_CONTENT_ALL)

    def raise_duel_state(self, series_idx: int, slot: int, min_state: int) -> None:
        """Ensure `Duel.State >= min_state` for the slot. Never lowers the
        existing state — completed duels (3) stay completed even if min_state
        is 1. Mapping `slot -> array_index = slot` (placeholder at index 0)."""
        addr = self._duel_addr(series_idx, slot)
        cur = self.pm.read_int(addr)
        if cur >= min_state:
            return
        self.pm.write_int(addr, min_state)

    def lock_duel_unless_complete(self, series_idx: int, slot: int) -> None:
        """Force `Duel.State = 0` (Locked) UNLESS already at 3 (Complete).

        Used by the sync enforcer to clear duels that the game auto-unlocked
        game-side (completing duel N raises slot N+1 to State=1) but which
        AP did not grant. Preserving 3 keeps already-fired location checks
        intact and avoids re-firing on subsequent rising edges."""
        addr = self._duel_addr(series_idx, slot)
        cur = self.pm.read_int(addr)
        if cur == 0 or cur == 3:
            return
        self.pm.write_int(addr, 0)

    def raise_card_count(self, index: int, min_count: int) -> None:
        """Ensure card-list byte's count nibble >= min_count, set Seen bit.
        Never lowers the count (player-deck-bound copies stay where they are)."""
        if not (0 <= min_count <= 3):
            raise ValueError(f"min_count must be 0..3, got {min_count}")
        if not (0 <= index < CARD_LIST_SIZE_LE2):
            raise ValueError(f"card index {index} out of range")
        addr = self.save_data + CARD_LIST_OFFSET_LE2 + index
        cur = self.pm.read_uchar(addr)
        cur_count = cur & 0x07
        if cur_count >= min_count and (cur & 0x08):
            return
        new = (cur & ~0x07) | max(cur_count, min_count) | 0x08
        if new == cur:
            return
        self.pm.write_uchar(addr, new)

    def increment_card_count(self, index: int) -> int:
        """Add 1 to card-list byte's count nibble (clamped at 3), set Seen bit.
        Returns the new count. Used for crafting: each purchase grants one
        more copy until the per-card cap of 3."""
        if not (0 <= index < CARD_LIST_SIZE_LE2):
            raise ValueError(f"card index {index} out of range")
        addr = self.save_data + CARD_LIST_OFFSET_LE2 + index
        cur = self.pm.read_uchar(addr)
        cur_count = cur & 0x07
        new_count = min(3, cur_count + 1)
        new = (cur & ~0x07) | new_count | 0x08
        if new != cur:
            self.pm.write_uchar(addr, new)
        return new_count

    def add_dp(self, amount: int) -> int:
        """Add (signed) `amount` to DuelPoints, clamped to [0, DP_MAX].
        Returns the new value."""
        addr = self._misc_addr(DUEL_POINTS_OFFSET_IN_MISC)
        cur = self.pm.read_longlong(addr)
        new = max(0, min(DP_MAX, cur + amount))
        if new == cur:
            return cur
        self.pm.write_longlong(addr, new)
        return new

    def hide_default_cards(self) -> int:
        """Write 0 to the default-structure-deck-cards static, suppressing
        the merge of starter-deck cards into the deck-edit trunk. Returns
        the original int32 (so the caller can restore on disconnect if it
        wants — runtime-only, reverts on game restart anyway)."""
        if self.pm is None:
            raise SaveDataNotReady("not attached")
        original = self.pm.read_int(DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2)
        if original == 0:
            return original
        old_protect = _virtual_protect_ex(
            self.pm.process_handle,
            DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2,
            4,
            _PAGE_READWRITE,
        )
        try:
            self.pm.write_int(DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2, 0)
        finally:
            _virtual_protect_ex(
                self.pm.process_handle,
                DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2,
                4,
                old_protect,
            )
        return original
