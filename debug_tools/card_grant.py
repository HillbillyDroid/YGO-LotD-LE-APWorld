"""Standalone smoke test: read/write card-list state in the running Lotd LE process.

Layout from pixeltris/Lotd CardListSaveData.cs + GameSaveData.cs:
  - cardList = saveData + 24008 (LE-v2 CardListOffset)
  - 20000 bytes; one byte per internal card *index* (NOT cardId)
  - Each byte (CardState.RawValue):
      bits 0-2 (mask 0x07): Count owned (0..3)
      bit 3   (mask 0x08): Seen flag (0 = "NEW" marker shown)
      bits 4-7 (mask 0xF0): unknown

Index→cardId mapping lives in cardPropsBinAddress (0x142847E50) at runtime, or in
bin/CARD_Indx_+CARD_Prop.bin archives statically. This script works in *index*
space — feeding it a YGO cardId requires a separate mapping step (TODO).

Usage:
  python card_grant.py --read INDEX                    # show byte at cardList+INDEX
  python card_grant.py --read INDEX --window 32        # dump 32 bytes around INDEX
  python card_grant.py --grant INDEX                   # set count to 3, mark Seen
  python card_grant.py --grant INDEX --count 1         # set count to N, mark Seen
  python card_grant.py --set-raw INDEX 0x0B            # write exact RawValue
  python card_grant.py --scan-owned                    # list every index with Count>0
  python card_grant.py --read-dp                       # show current DuelPoints
  python card_grant.py --add-dp 100000                  # grant 100k DP (negative ok)
  python card_grant.py --set-dp 0                      # zero DP out
  python card_grant.py --hide-default-cards            # stop starter-deck cards from being merged into trunk
  python card_grant.py --restore-default-cards 0x1     # restore original value (printed by --hide-default-cards)
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys

import pymem


SAVE_DATA_ADDRESS_1 = 0x142924010
SAVE_DATA_MEM_OFFSET = 80
SAVE_DATA_ADDRESS_OFFSET = 10208
CARD_LIST_OFFSET_LE2 = 24008
CARD_LIST_SIZE_LE2 = 20000
MISC_DATA_OFFSET_LE2 = 4056
DUEL_POINTS_OFFSET_IN_MISC = 16  # int64 at miscData + 16
DP_MAX = 9_999_999_999  # in-game DP display caps at 10 digits; stay one under
# pixeltris's HideDefaultStructureDeckCards: int32 array of YDC-deck offsets terminated by <=0.
# Writing 0 to the first slot makes the loop exit immediately → no starter cards merged into
# the trunk. Runtime-only; reverts on game relaunch.
DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2 = 0x140A6C818
# Empirical valid card-index range in LE-v2 (verified 2026-05-04). Indices outside
# this band exist in the array but are unused/empty slots — writing to them grants
# nothing in-game.
VALID_INDEX_MIN = 3000
VALID_INDEX_MAX = 15000

CANDIDATE_EXE_NAMES = ["Lotd.exe", "LotdLE.exe", "YuGiOh.exe", "Yu-Gi-Oh!.exe"]


# --- Win32 plumbing for region scanning ---
PAGE_READABLE = 0x02 | 0x04 | 0x20 | 0x40 | 0x80  # READONLY|READWRITE|EXECUTE_READ|EXECUTE_READWRITE|EXECUTE_WRITECOPY
PAGE_READWRITE = 0x04
MEM_COMMIT = 0x1000


def _virtual_protect_ex(handle: int, addr: int, size: int, new_protect: int) -> int:
    """Wrap VirtualProtectEx; return the previous protection. Raises on failure."""
    VirtualProtectEx = ctypes.windll.kernel32.VirtualProtectEx
    VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    VirtualProtectEx.restype = wt.BOOL
    old = wt.DWORD(0)
    if not VirtualProtectEx(handle, ctypes.c_void_p(addr), size, new_protect, ctypes.byref(old)):
        raise OSError(f"VirtualProtectEx failed (GetLastError={ctypes.get_last_error()})")
    return old.value


def write_int_force(pm: pymem.Pymem, addr: int, value: int) -> None:
    """Write an int32 even if the page is read-only (.rdata). Restores protection after."""
    old = _virtual_protect_ex(pm.process_handle, addr, 4, PAGE_READWRITE)
    try:
        pm.write_int(addr, value)
    finally:
        _virtual_protect_ex(pm.process_handle, addr, 4, old)

class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


def iter_committed_regions(handle: int, max_addr: int = 0x7FFFFFFFFFFF):
    VirtualQueryEx = ctypes.windll.kernel32.VirtualQueryEx
    VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID, ctypes.c_void_p, ctypes.c_size_t]
    VirtualQueryEx.restype = ctypes.c_size_t
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    while addr < max_addr:
        ret = VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if ret == 0:
            break
        if mbi.State == MEM_COMMIT and (mbi.Protect & PAGE_READABLE):
            yield mbi.BaseAddress, mbi.RegionSize
        addr = mbi.BaseAddress + mbi.RegionSize


def find_signature_in_process(pm: pymem.Pymem, signature: bytes, exclude_addr: int) -> list[int]:
    """Return all addresses in the target process where `signature` occurs, except inside the
    region containing exclude_addr (so we don't flag the source itself)."""
    matches = []
    for region_base, region_size in iter_committed_regions(pm.process_handle):
        if region_size > 256 * 1024 * 1024:  # skip huge regions to keep this fast
            continue
        try:
            data = pm.read_bytes(region_base, region_size)
        except Exception:
            continue
        start = 0
        while True:
            idx = data.find(signature, start)
            if idx == -1:
                break
            hit = region_base + idx
            if not (region_base <= exclude_addr < region_base + region_size):
                matches.append(hit)
            elif abs(hit - exclude_addr) > 1024:
                # match in same region but not the original location
                matches.append(hit)
            start = idx + 1
    return matches


def attach() -> pymem.Pymem:
    last_err: Exception | None = None
    for name in CANDIDATE_EXE_NAMES:
        try:
            pm = pymem.Pymem(name)
            print(f"attached to {name} (pid={pm.process_id})")
            return pm
        except Exception as e:
            last_err = e
    raise RuntimeError(f"could not attach to any of {CANDIDATE_EXE_NAMES}; last error: {last_err}")


def get_save_data_addresses(pm: pymem.Pymem) -> tuple[int, int]:
    """Returns (base, saveData). `base` is the pre-+10208 pointer used by
    GetDeckEditorOwnedCardListAddress (= base + 40 per source)."""
    v2 = pm.read_int(SAVE_DATA_ADDRESS_1 + 176)
    v3 = pm.read_ulonglong(SAVE_DATA_ADDRESS_1)
    v4 = pm.read_ulonglong(v3 + 8)
    v5 = v3
    for _ in range(1024):
        if pm.read_uchar(v4 + 25) != 0:
            break
        if pm.read_int(v4 + 32) >= v2:
            v5 = v4
            v4 = pm.read_ulonglong(v4)
        else:
            v4 = pm.read_ulonglong(v4 + 16)
    else:
        raise RuntimeError("save-data tree traversal did not terminate")
    if v5 == v3 or v2 < pm.read_int(v5 + 32):
        v5 = v3
    result = pm.read_ulonglong(v5 + 40)
    if result == 0:
        raise RuntimeError("save-data result pointer is null — is a save loaded?")
    base = pm.read_ulonglong(result + SAVE_DATA_MEM_OFFSET)
    save = pm.read_ulonglong(base + SAVE_DATA_ADDRESS_OFFSET)
    return base, save


def get_save_data_address(pm: pymem.Pymem) -> int:
    return get_save_data_addresses(pm)[1]


def warn_if_out_of_range(idx: int) -> None:
    if not VALID_INDEX_MIN <= idx <= VALID_INDEX_MAX:
        print(f"WARNING: index {idx} outside empirical valid range "
              f"{VALID_INDEX_MIN}..{VALID_INDEX_MAX}; writes here likely grant nothing in-game.")


def decode(raw: int) -> str:
    count = raw & 0x07
    seen = bool((raw >> 3) & 1)
    high = (raw >> 4) & 0x0F
    return f"count={count} seen={seen} high=0x{high:X}"


def encode(count: int, seen: bool, high: int = 0) -> int:
    return (count & 0x07) | ((1 if seen else 0) << 3) | ((high & 0x0F) << 4)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lotd LE card-list memory poke.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--read", type=lambda s: int(s, 0), metavar="INDEX",
                   help="show byte at cardList+INDEX")
    g.add_argument("--grant", type=lambda s: int(s, 0), metavar="INDEX",
                   help="set Count and mark Seen at cardList+INDEX")
    g.add_argument("--revoke", type=lambda s: int(s, 0), metavar="INDEX",
                   help="zero the byte at cardList+INDEX (count=0, seen=0)")
    g.add_argument("--set-raw", nargs=2, metavar=("INDEX", "RAW"),
                   help="write exact RawValue at cardList+INDEX (both args accept hex)")
    g.add_argument("--scan-owned", action="store_true",
                   help="dump every index with Count>0")
    g.add_argument("--read-dp", action="store_true",
                   help="read current DuelPoints (int64 at miscData+16)")
    g.add_argument("--add-dp", type=lambda s: int(s, 0), metavar="AMOUNT",
                   help="add AMOUNT to current DuelPoints (negative ok). Clamped to [0, DP_MAX].")
    g.add_argument("--set-dp", type=lambda s: int(s, 0), metavar="AMOUNT",
                   help="set DuelPoints to AMOUNT (clamped to [0, DP_MAX]).")
    g.add_argument("--hide-default-cards", action="store_true",
                   help="zero defaultStructureDeckCardsAddress so starter-deck cards stop being merged into the trunk. Runtime-only; reverts on game relaunch.")
    g.add_argument("--restore-default-cards", type=lambda s: int(s, 0), metavar="ORIG_INT32",
                   help="write ORIG_INT32 back to defaultStructureDeckCardsAddress (use with value printed by --hide-default-cards).")
    g.add_argument("--probe-deck-edit", action="store_true",
                   help="dump 32 bytes from base+N for several candidate N, looking for the live trunk array")
    g.add_argument("--probe-deck-edit-offset", type=lambda s: int(s, 0), metavar="OFFSET",
                   help="dump full card-list-sized region from base+OFFSET (use after --probe-deck-edit narrows it)")
    g.add_argument("--grant-deck-edit", nargs=2, metavar=("OFFSET", "INDEX"),
                   help="grant count=3 at (base+OFFSET)+INDEX — for testing the live trunk address")
    g.add_argument("--grant-at", nargs=2, metavar=("ABS_ADDR", "INDEX"),
                   help="grant count=3 at ABS_ADDR+INDEX (absolute address); also dumps 16-byte windows around the byte before/after")
    g.add_argument("--read-at", nargs="+", metavar="ABS_ADDR",
                   help="read full 20000-byte cardList-shaped region at each ABS_ADDR; cross-check vs persistent cardList")
    g.add_argument("--find-live-trunk", action="store_true",
                   help="scan process memory for duplicate(s) of cardList — find the live trunk array")
    p.add_argument("--window", type=int, default=1,
                   help="(with --read) bytes to dump centered on INDEX (default 1)")
    p.add_argument("--count", type=int, default=3,
                   help="(with --grant) Count value 0..3 (default 3)")
    args = p.parse_args(argv)
    if args.set_raw:
        try:
            idx, raw = int(args.set_raw[0], 0), int(args.set_raw[1], 0)
        except ValueError:
            p.error("--set-raw args must be integers (decimal or 0x... hex)")
        if not 0 <= idx < CARD_LIST_SIZE_LE2:
            p.error(f"INDEX out of range 0..{CARD_LIST_SIZE_LE2 - 1}")
        if not 0 <= raw <= 0xFF:
            p.error("RAW must be a byte (0..255)")
        args.set_raw = (idx, raw)
    for attr in ("read", "grant", "revoke"):
        v = getattr(args, attr)
        if v is not None and not 0 <= v < CARD_LIST_SIZE_LE2:
            p.error(f"INDEX out of range 0..{CARD_LIST_SIZE_LE2 - 1}")
    if not 0 <= args.count <= 3:
        p.error("--count must be in 0..3")
    if args.window < 1 or args.window > 256:
        p.error("--window must be in 1..256")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    pm = attach()

    if args.hide_default_cards:
        addr = DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2
        before = pm.read_int(addr)
        print(f"\ndefaultStructureDeckCards @ 0x{addr:X}")
        print(f"  before = {before} (0x{before & 0xFFFFFFFF:08X})")
        if before == 0:
            print("  already zero — nothing to do.")
            return 0
        write_int_force(pm, addr, 0)
        readback = pm.read_int(addr)
        print(f"  after  = {readback}")
        if readback != 0:
            print("WRITE FAILED — value did not stick.")
            return 1
        print(f"\nOK — re-enter deck-edit / trunk to refresh. To restore this session: "
              f"--restore-default-cards 0x{before & 0xFFFFFFFF:X}")
        return 0

    if args.restore_default_cards is not None:
        addr = DEFAULT_STRUCTURE_DECK_CARDS_ADDRESS_LE2
        val = args.restore_default_cards & 0xFFFFFFFF
        # interpret as signed int32 for write_int
        if val >= 0x80000000:
            signed = val - 0x100000000
        else:
            signed = val
        before = pm.read_int(addr)
        print(f"\ndefaultStructureDeckCards @ 0x{addr:X}")
        print(f"  before = {before}")
        write_int_force(pm, addr, signed)
        readback = pm.read_int(addr)
        print(f"  after  = {readback}")
        if readback != signed:
            print("WRITE FAILED — value did not stick.")
            return 1
        return 0

    base, save_data = get_save_data_addresses(pm)
    cardlist_addr = save_data + CARD_LIST_OFFSET_LE2
    dp_addr = save_data + MISC_DATA_OFFSET_LE2 + DUEL_POINTS_OFFSET_IN_MISC
    print(f"base          = 0x{base:X}")
    print(f"saveData      = 0x{save_data:X}  (base + {save_data - base})")
    print(f"cardList      = 0x{cardlist_addr:X}")
    print(f"duelPoints    = 0x{dp_addr:X}")

    if args.read_dp:
        dp = pm.read_longlong(dp_addr)
        print(f"\nDuelPoints = {dp:,}  (raw int64 = {dp})")
        return 0

    if args.add_dp is not None or args.set_dp is not None:
        before = pm.read_longlong(dp_addr)
        if args.set_dp is not None:
            new = args.set_dp
        else:
            new = before + args.add_dp
        new = max(0, min(DP_MAX, new))
        print(f"\nDuelPoints write @ 0x{dp_addr:X}")
        print(f"  before = {before:,}")
        print(f"  after  = {new:,}  (delta {new - before:+,})")
        pm.write_longlong(dp_addr, new)
        readback = pm.read_longlong(dp_addr)
        if readback != new:
            print(f"WRITE FAILED — readback = {readback:,}")
            return 1
        print("\nOK — re-enter a screen that displays DP (shop / main menu) to refresh the UI.")
        return 0

    if args.probe_deck_edit:
        candidates = [0, 8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128]
        print("\nprobing base+N for card-state-shaped data (32 bytes each):")
        for off in candidates:
            try:
                raw = pm.read_bytes(base + off, 32)
            except Exception as e:
                print(f"  base+{off:4d} @ 0x{base + off:X}  read failed: {e}")
                continue
            counts = [b & 0x07 for b in raw]
            looks_carddata = all(c <= 3 for c in counts) and any(c > 0 for c in counts)
            note = "  ← card-state-shaped" if looks_carddata else ""
            hex_bytes = " ".join(f"{b:02X}" for b in raw[:16])
            print(f"  base+{off:4d} @ 0x{base + off:X}  {hex_bytes} ...{note}")
            if looks_carddata:
                decoded = [f"#{i}={decode(b)}" for i, b in enumerate(raw[:8]) if (b & 0x07) > 0]
                if decoded:
                    print(f"    first owned: {', '.join(decoded[:5])}")
        print("\nNote: source says base+40 in original Lotd but 'might be different in LE'.")
        print("Compare any card-state-shaped offset to saveData+24008 (cardList) — if values")
        print("match for some indices but differ for others, the live trunk is the right target.")
        return 0

    if args.probe_deck_edit_offset is not None:
        off = args.probe_deck_edit_offset
        addr = base + off
        print(f"\nprobing base+{off} (0x{addr:X}) for {CARD_LIST_SIZE_LE2} bytes")
        raw = pm.read_bytes(addr, CARD_LIST_SIZE_LE2)
        owned = [(i, b) for i, b in enumerate(raw) if (b & 0x07) > 0]
        print(f"owned-shaped entries (count>0): {len(owned)}")
        for i, b in owned[:30]:
            print(f"  index {i:5d} @ 0x{addr + i:X}  raw=0x{b:02X}  {decode(b)}")
        if len(owned) > 30:
            print(f"  ... ({len(owned) - 30} more)")
        # cross-check vs cardList
        cl = pm.read_bytes(cardlist_addr, CARD_LIST_SIZE_LE2)
        diffs = sum(1 for a, b in zip(raw, cl) if a != b)
        print(f"\ncross-check vs cardList (saveData+24008): {diffs} bytes differ "
              f"out of {CARD_LIST_SIZE_LE2}")
        return 0

    if args.find_live_trunk:
        # Pick the densest 256-byte window from cardList — longer + denser =
        # vastly fewer false matches than a sparse 64-byte signature.
        full = pm.read_bytes(cardlist_addr, CARD_LIST_SIZE_LE2)
        sig_size = 256
        best_off = -1
        best_nonzero = -1
        for off in range(0, CARD_LIST_SIZE_LE2 - sig_size, 8):
            window = full[off:off + sig_size]
            nonzero = sum(1 for b in window if b != 0)
            if nonzero > best_nonzero:
                best_nonzero = nonzero
                best_off = off
        if best_off < 0:
            print("cardList is empty? aborting.")
            return 1
        signature = bytes(full[best_off:best_off + sig_size])
        sig_src_addr = cardlist_addr + best_off
        print(f"\nsignature: {sig_size} bytes from cardList+{best_off} "
              f"(0x{sig_src_addr:X}, {best_nonzero}/{sig_size} non-zero)")
        print(f"  hex (first 64 bytes): {signature[:64].hex()}")
        print("scanning process memory (this may take ~30s)...")
        matches = find_signature_in_process(pm, signature, exclude_addr=sig_src_addr)
        print(f"\nfound {len(matches)} match(es):")
        for hit in matches:
            implied_cardlist_base = hit - best_off
            note = ""
            if hit == sig_src_addr:
                note = "  ← source (cardList itself)"
            print(f"  0x{hit:X}  → if this is a cardList copy, base address = 0x{implied_cardlist_base:X}{note}")
        if len(matches) > 1:
            print("\n>1 match — likely the live trunk array. Try:")
            for hit in matches:
                if hit != sig_src_addr:
                    implied = hit - best_off
                    print(f"  python card_grant.py --grant-deck-edit-raw 0x{implied:X} INDEX")
        elif len(matches) == 1 and matches[0] == sig_src_addr:
            print("\nOnly cardList itself matched. Live trunk may not be a verbatim copy,")
            print("or it may be in a region we skipped (>256 MB). Open deck-edit and try again.")
        return 0

    if args.grant_deck_edit:
        try:
            off, idx = int(args.grant_deck_edit[0], 0), int(args.grant_deck_edit[1], 0)
        except ValueError:
            print("--grant-deck-edit args must be integers")
            return 1
        target = base + off + idx
        before = pm.read_uchar(target)
        new = encode(count=3, seen=True, high=(before >> 4) & 0xF)
        print(f"\ngranting at base+{off}+{idx} = 0x{target:X}")
        print(f"  before = 0x{before:02X}  ({decode(before)})")
        pm.write_uchar(target, new)
        readback = pm.read_uchar(target)
        print(f"  after  = 0x{readback:02X}  ({decode(readback)})")
        return 0

    if args.grant_at:
        try:
            abs_addr, idx = int(args.grant_at[0], 0), int(args.grant_at[1], 0)
        except ValueError:
            print("--grant-at args must be integers (decimal or 0x... hex)")
            return 1
        target = abs_addr + idx
        before = pm.read_uchar(target)
        new = encode(count=3, seen=True, high=(before >> 4) & 0xF)
        print(f"\ngranting at 0x{abs_addr:X}+{idx} = 0x{target:X}")
        print(f"  before = 0x{before:02X}  ({decode(before)})")
        pm.write_uchar(target, new)
        readback = pm.read_uchar(target)
        print(f"  after  = 0x{readback:02X}  ({decode(readback)})")
        return 0

    if args.read_at:
        try:
            addrs = [int(s, 0) for s in args.read_at]
        except ValueError:
            print("--read-at args must be integers (decimal or 0x... hex)")
            return 1
        cl = pm.read_bytes(cardlist_addr, CARD_LIST_SIZE_LE2)
        for addr in addrs:
            print(f"\n=== reading {CARD_LIST_SIZE_LE2} bytes at 0x{addr:X} ===")
            try:
                raw = pm.read_bytes(addr, CARD_LIST_SIZE_LE2)
            except Exception as e:
                print(f"  read failed: {e}")
                continue
            owned = [(i, b) for i, b in enumerate(raw) if (b & 0x07) > 0]
            print(f"  owned-shaped entries (count>0): {len(owned)}")
            for i, b in owned[:10]:
                print(f"    index {i:5d}  raw=0x{b:02X}  {decode(b)}")
            if len(owned) > 10:
                print(f"    ... ({len(owned) - 10} more)")
            diffs = sum(1 for a, b in zip(raw, cl) if a != b)
            first_diffs = [(i, a, b) for i, (a, b) in enumerate(zip(raw, cl)) if a != b][:8]
            print(f"  vs cardList: {diffs} differing bytes")
            for i, a, b in first_diffs:
                print(f"    diff @ idx {i}: this=0x{a:02X} cardList=0x{b:02X}")
        return 0

    if args.read is not None:
        idx = args.read
        n = max(1, args.window)
        start_idx = max(0, idx - n // 2)
        end_idx = min(CARD_LIST_SIZE_LE2, start_idx + n)
        raw = pm.read_bytes(cardlist_addr + start_idx, end_idx - start_idx)
        print(f"\nbytes [index {start_idx}..{end_idx - 1}]:")
        for i, b in enumerate(raw):
            marker = "  ← target" if start_idx + i == idx else ""
            print(f"  index {start_idx + i:5d} @ 0x{cardlist_addr + start_idx + i:X}  "
                  f"raw=0x{b:02X}  {decode(b)}{marker}")
        return 0

    if args.scan_owned:
        raw = pm.read_bytes(cardlist_addr, CARD_LIST_SIZE_LE2)
        owned = [(i, b) for i, b in enumerate(raw) if (b & 0x07) > 0]
        print(f"\nowned indices: {len(owned)}")
        for i, b in owned:
            print(f"  index {i:5d} @ 0x{cardlist_addr + i:X}  raw=0x{b:02X}  {decode(b)}")
        return 0

    if args.grant is not None:
        idx = args.grant
        warn_if_out_of_range(idx)
        target = cardlist_addr + idx
        before = pm.read_uchar(target)
        new = encode(count=args.count, seen=True, high=(before >> 4) & 0xF)
        print(f"\ngranting index {idx} (count={args.count}, seen=True) @ 0x{target:X}")
        print(f"  before = 0x{before:02X}  ({decode(before)})")
        pm.write_uchar(target, new)
        readback = pm.read_uchar(target)
        print(f"  after  = 0x{readback:02X}  ({decode(readback)})")
        if readback != new:
            print("WRITE FAILED — value did not stick.")
            return 1
        print("\nOK — re-enter deck-edit / trunk to refresh the UI.")
        return 0

    if args.revoke is not None:
        idx = args.revoke
        target = cardlist_addr + idx
        before = pm.read_uchar(target)
        print(f"\nrevoking index {idx} @ 0x{target:X}")
        print(f"  before = 0x{before:02X}  ({decode(before)})")
        pm.write_uchar(target, 0)
        readback = pm.read_uchar(target)
        print(f"  after  = 0x{readback:02X}  ({decode(readback)})")
        if readback != 0:
            print("WRITE FAILED — value did not stick.")
            return 1
        print("\nOK — re-enter deck-edit / trunk to refresh the UI.")
        return 0

    if args.set_raw:
        idx, raw_val = args.set_raw
        warn_if_out_of_range(idx)
        target = cardlist_addr + idx
        before = pm.read_uchar(target)
        print(f"\nwriting raw 0x{raw_val:02X} at index {idx} @ 0x{target:X}")
        print(f"  before = 0x{before:02X}  ({decode(before)})")
        pm.write_uchar(target, raw_val)
        readback = pm.read_uchar(target)
        print(f"  after  = 0x{readback:02X}  ({decode(readback)})")
        if readback != raw_val:
            print("WRITE FAILED — value did not stick.")
            return 1
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
