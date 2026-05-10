"""Standalone smoke test: read/write campaign duel state in the running Lotd LE process.

Layout from pixeltris/Lotd CampaignSaveData.cs + GameSaveData.cs:
  - campaignData = saveData + 7024 (LE-v2)
  - 8 bytes header (0, 1)
  - 6 series, in order: YuGiOh, GX, 5D, ZEXAL, ARC-V, VRAINS
  - Each series: duel0 (24 bytes) + 8 extra bytes + 49 more duels (24 bytes each) = 1216 bytes
  - Each Duel struct (24 bytes): State i32, ReverseDuelState i32, Unk1 i32, Unk2 i32, Unk3 i32, Unk4 i32

CampaignDuelState enum:
  0 Locked, 1 Available, 2 AvailableAttempted, 3 Complete, 4+ AvailableAlt

Usage:
  python campaign_probe.py --read                                 # dump all duels for all series
  python campaign_probe.py --read --series ZEXAL                  # dump one series
  python campaign_probe.py --get YuGiOh 0                         # show one duel struct
  python campaign_probe.py --set YuGiOh 5 1                       # set duel state
  python campaign_probe.py --set-reverse YuGiOh 5 1               # set reverse duel state
  python campaign_probe.py --complete YuGiOh 5                    # convenience: State=3
"""
from __future__ import annotations

import argparse
import sys

import pymem


SAVE_DATA_ADDRESS_1 = 0x142924010
SAVE_DATA_MEM_OFFSET = 80
SAVE_DATA_ADDRESS_OFFSET = 10208
CAMPAIGN_DATA_OFFSET_LE2 = 7024

DUELS_PER_SERIES = 50
DUEL_STRUCT_SIZE = 24
SERIES_HEADER_SIZE = 8        # extra 8 bytes after duel 0 of each series
CAMPAIGN_HEADER_SIZE = 8      # 2 int32s at start of CampaignSaveData chunk

# Series order matches DuelSeries enum (CampaignSaveData.cs IndexToSeries).
SERIES_ORDER = ["YuGiOh", "GX", "5D", "ZEXAL", "ARC-V", "VRAINS"]
SERIES_ALIASES = {
    "yugioh": "YuGiOh", "yu-gi-oh": "YuGiOh", "dm": "YuGiOh",
    "gx": "GX",
    "5d": "5D", "5ds": "5D", "5d's": "5D",
    "zexal": "ZEXAL",
    "arcv": "ARC-V", "arc-v": "ARC-V",
    "vrains": "VRAINS",
}

STATE_NAMES = {
    0: "Locked",
    1: "Available",
    2: "AvailableAttempted",
    3: "Complete",
    4: "AvailableAlt",
}

CANDIDATE_EXE_NAMES = ["Lotd.exe", "LotdLE.exe", "YuGiOh.exe", "Yu-Gi-Oh!.exe"]


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


def get_save_data_address(pm: pymem.Pymem) -> int:
    """Verbatim port of MemTools.Addresses.Lotd.cs::GetBaseSaveDataAddress."""
    v2 = pm.read_int(SAVE_DATA_ADDRESS_1 + 176)
    v3 = pm.read_ulonglong(SAVE_DATA_ADDRESS_1)
    v4 = pm.read_ulonglong(v3 + 8)
    v5 = v3
    for _ in range(1024):
        leaf = pm.read_uchar(v4 + 25)
        if leaf != 0:
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
    return save


def normalize_series(name: str) -> str:
    key = name.lower().replace("'", "").replace(" ", "")
    if key in SERIES_ALIASES:
        return SERIES_ALIASES[key]
    if name in SERIES_ORDER:
        return name
    raise ValueError(f"unknown series {name!r}; known: {', '.join(SERIES_ORDER)}")


def series_base_offset(series_idx: int) -> int:
    """Byte offset of a series within the CampaignSaveData chunk."""
    return CAMPAIGN_HEADER_SIZE + series_idx * (DUEL_STRUCT_SIZE * DUELS_PER_SERIES + SERIES_HEADER_SIZE)


def duel_offset(series_idx: int, duel_idx: int) -> int:
    """Byte offset of a single Duel struct within the CampaignSaveData chunk."""
    base = series_base_offset(series_idx)
    if duel_idx == 0:
        return base
    return base + DUEL_STRUCT_SIZE + SERIES_HEADER_SIZE + (duel_idx - 1) * DUEL_STRUCT_SIZE


def duel_address(save_data: int, series_idx: int, duel_idx: int) -> int:
    return save_data + CAMPAIGN_DATA_OFFSET_LE2 + duel_offset(series_idx, duel_idx)


def state_name(value: int) -> str:
    if value in STATE_NAMES:
        return STATE_NAMES[value]
    if value >= 4:
        return f"AvailableAlt({value})"
    return f"unknown({value})"


def read_duel(pm: pymem.Pymem, save_data: int, series_idx: int, duel_idx: int) -> dict:
    addr = duel_address(save_data, series_idx, duel_idx)
    return {
        "addr": addr,
        "state": pm.read_int(addr),
        "reverse_state": pm.read_int(addr + 4),
        "unk1": pm.read_int(addr + 8),
        "unk2": pm.read_int(addr + 12),
        "unk3": pm.read_int(addr + 16),
        "unk4": pm.read_int(addr + 20),
    }


def print_duel(series_name: str, duel_idx: int, d: dict) -> None:
    print(
        f"  {series_name:6s} #{duel_idx:02d} @ 0x{d['addr']:X}  "
        f"State={d['state']} ({state_name(d['state'])})  "
        f"Reverse={d['reverse_state']} ({state_name(d['reverse_state'])})  "
        f"Unk={d['unk1']},{d['unk2']},{d['unk3']},{d['unk4']}"
    )


def cmd_read(pm: pymem.Pymem, save_data: int, only_series: str | None) -> None:
    series_indices = (
        [SERIES_ORDER.index(only_series)] if only_series else range(len(SERIES_ORDER))
    )
    for i in series_indices:
        sname = SERIES_ORDER[i]
        print(f"\n=== {sname} ===")
        any_active = False
        for j in range(DUELS_PER_SERIES):
            d = read_duel(pm, save_data, i, j)
            if d["state"] == 0 and d["reverse_state"] == 0 and not any(d[k] for k in ("unk1", "unk2", "unk3", "unk4")):
                continue
            any_active = True
            print_duel(sname, j, d)
        if not any_active:
            print(f"  ({sname}: all duels Locked / zeroed)")


def cmd_get(pm: pymem.Pymem, save_data: int, series_name: str, duel_idx: int) -> None:
    i = SERIES_ORDER.index(series_name)
    d = read_duel(pm, save_data, i, duel_idx)
    print_duel(series_name, duel_idx, d)


def cmd_set(pm: pymem.Pymem, save_data: int, series_name: str, duel_idx: int, value: int, reverse: bool = False) -> None:
    i = SERIES_ORDER.index(series_name)
    addr = duel_address(save_data, series_idx=i, duel_idx=duel_idx)
    field_addr = addr + (4 if reverse else 0)
    field_label = "ReverseDuelState" if reverse else "State"
    before = pm.read_int(field_addr)
    print(
        f"writing {field_label} = {value} ({state_name(value)}) for "
        f"{series_name} #{duel_idx} @ 0x{field_addr:X}"
    )
    print(f"  before = {before} ({state_name(before)})")
    pm.write_int(field_addr, value)
    after = pm.read_int(field_addr)
    print(f"  after  = {after} ({state_name(after)})")
    if after != value:
        print("WRITE FAILED — value did not stick.")
        sys.exit(1)
    print("\nOK — re-enter the campaign menu in-game to refresh the UI.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lotd LE campaign-duel memory poke.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--read",
        action="store_true",
        help="dump all non-zero duels (use --series to limit)",
    )
    g.add_argument(
        "--get",
        nargs=2,
        metavar=("SERIES", "INDEX"),
        help="dump one duel: SERIES is one of YuGiOh/GX/5D/ZEXAL/ARC-V/VRAINS, INDEX is 0..49",
    )
    g.add_argument(
        "--set",
        nargs=3,
        metavar=("SERIES", "INDEX", "STATE"),
        help="write Duel.State (0=Locked, 1=Available, 2=AvailableAttempted, 3=Complete, 4+=AvailableAlt)",
    )
    g.add_argument(
        "--set-reverse",
        nargs=3,
        metavar=("SERIES", "INDEX", "STATE"),
        help="write Duel.ReverseDuelState (same enum)",
    )
    g.add_argument(
        "--complete",
        nargs=2,
        metavar=("SERIES", "INDEX"),
        help="shorthand for --set SERIES INDEX 3",
    )
    p.add_argument(
        "--series",
        help="(with --read) limit to one series",
    )
    args = p.parse_args(argv)

    def _parse_series_index(s_name: str, idx_str: str) -> tuple[str, int]:
        try:
            sname = normalize_series(s_name)
        except ValueError as e:
            p.error(str(e))
        try:
            idx = int(idx_str, 0)
        except ValueError:
            p.error("INDEX must be an integer")
        if not 0 <= idx < DUELS_PER_SERIES:
            p.error(f"INDEX must be in 0..{DUELS_PER_SERIES - 1}")
        return sname, idx

    if args.series:
        try:
            args.series = normalize_series(args.series)
        except ValueError as e:
            p.error(str(e))

    if args.get:
        s, i = _parse_series_index(args.get[0], args.get[1])
        args.get = (s, i)
    if args.set:
        s, i = _parse_series_index(args.set[0], args.set[1])
        try:
            v = int(args.set[2], 0)
        except ValueError:
            p.error("STATE must be an integer")
        args.set = (s, i, v)
    if args.set_reverse:
        s, i = _parse_series_index(args.set_reverse[0], args.set_reverse[1])
        try:
            v = int(args.set_reverse[2], 0)
        except ValueError:
            p.error("STATE must be an integer")
        args.set_reverse = (s, i, v)
    if args.complete:
        s, i = _parse_series_index(args.complete[0], args.complete[1])
        args.complete = (s, i)

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    pm = attach()
    save_data = get_save_data_address(pm)
    campaign_addr = save_data + CAMPAIGN_DATA_OFFSET_LE2
    print(f"saveData      = 0x{save_data:X}")
    print(f"campaignData  = 0x{campaign_addr:X}")

    if args.read:
        cmd_read(pm, save_data, args.series)
        return 0

    if args.get:
        cmd_get(pm, save_data, *args.get)
        return 0

    if args.set:
        cmd_set(pm, save_data, *args.set)
        return 0

    if args.set_reverse:
        s, i, v = args.set_reverse
        cmd_set(pm, save_data, s, i, v, reverse=True)
        return 0

    if args.complete:
        s, i = args.complete
        cmd_set(pm, save_data, s, i, 3)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
