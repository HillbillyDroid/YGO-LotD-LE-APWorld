# -*- coding: utf-8 -*-
"""Extract campaign duel names from the YGO_2020.dat archive (offline, no game).

Format references:
  - Lotd/LotdArchive.cs::Load            — TOC walker, 4-byte aligned data offsets
  - nzxth2/YGO_LOTD_LE_DuelData_Editor   — 92-byte record format
  - pixeltris/Lotd/SaveData/CampaignSaveData.cs::IndexToSeries — series order

TOC format (`YGO_2020.toc`, plain text):
  - Line 0: 'UT' (header marker, skipped)
  - Subsequent lines: '<hex length> <hex pathlen> <path>'
  - Files concatenated in .dat at running offset, padded to 4-byte alignment

dueldata_E.bin format (per nzxth2):
  - Header: int64 duelCount  (LE)
  - Per record (92 bytes):
      11 int32:  field1..field11
      6 int64:   pointer1..pointer6 (file-relative offsets to strings)
  - Strings at end of file. pointer1..3 = ascii, pointer4..6 = utf16-le.
  - String identity (per pixeltris DuelData.cs Save order):
      ptr1 = codeName
      ptr2 = playerAlternateSkin
      ptr3 = opponentAlternateSkin
      ptr4 = name           (the campaign duel display title)
      ptr5 = description
      ptr6 = tip
  - Field identity (10 fields per pixeltris; nzxth2 has 11, last one extra in LE):
      f1 = id
      f2 = series (DuelSeries enum: 0..5 = YuGiOh/GX/5Ds/ZEXAL/ARC-V/VRAINS)
      f3 = displayIndex
      f4 = playerCharId
      f5 = opponentCharId
      f6 = playerDeckId
      f7 = opponentDeckId
      f8 = arenaId
      f9 = unk8
      f10 = dlcId
      f11 = (LE-only addition, semantics unknown — capture verbatim)

Outputs:
  - duel_data_raw.json:                  every parsed record verbatim (debug)
  - ap_world/data/duel_table.py:         (series, slot) -> name mapping for AP

Usage:
  python extract_duel_names.py
  python extract_duel_names.py --install "F:\\SteamLibrary\\steamapps\\common\\Yu-Gi-Oh! Legacy of the Duelist Link Evolution"
  python extract_duel_names.py --raw-only          # skip duel_table.py emit
  python extract_duel_names.py --hex 4             # hex-dump first 4 records and exit
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


DEFAULT_INSTALL = Path("F:/SteamLibrary/steamapps/common/Yu-Gi-Oh! Legacy of the Duelist Link Evolution")
TOC_NAME = "YGO_2020.toc"
DAT_NAME = "YGO_2020.dat"
TARGET_FILE = "main\\dueldata_E.bin"

RECORD_SIZE = 92
HEADER_SIZE = 8

SERIES_NAMES = ["YuGiOh", "GX", "5Ds", "ZEXAL", "ARC-V", "VRAINS"]


def parse_toc(toc_path: Path) -> list[tuple[str, int, int]]:
    """Returns list of (path, offset_in_dat, length). 4-byte alignment between files."""
    out: list[tuple[str, int, int]] = []
    offset = 0
    with toc_path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("UT"):
                continue
            # Don't strip leading whitespace permanently — we need it for path
            # disambiguation (paths can contain spaces). Tokenize: take 1st two
            # whitespace-separated tokens, treat the rest of the line as path.
            stripped = line.rstrip("\r\n")
            if not stripped.strip():
                continue
            # find first non-space
            i = 0
            while i < len(stripped) and stripped[i] == " ":
                i += 1
            j = stripped.find(" ", i)
            if j < 0:
                raise RuntimeError(f"unparseable toc line: {line!r}")
            length_str = stripped[i:j]
            # skip any spaces between length and pathlen
            k = j
            while k < len(stripped) and stripped[k] == " ":
                k += 1
            m = stripped.find(" ", k)
            if m < 0:
                raise RuntimeError(f"unparseable toc line: {line!r}")
            pathlen_str = stripped[k:m]
            path = stripped[m + 1:]
            length = int(length_str, 16)
            pathlen = int(pathlen_str, 16)
            if pathlen != len(path):
                raise RuntimeError(
                    f"pathlen mismatch: declared 0x{pathlen:X}, actual {len(path)} for {path!r}"
                )
            out.append((path, offset, length))
            offset += length
            if length % 4 != 0:
                offset += 4 - (length % 4)
    return out


def find_dueldata_offset(entries: list[tuple[str, int, int]]) -> tuple[int, int]:
    for path, off, length in entries:
        if path == TARGET_FILE:
            return off, length
    raise RuntimeError(f"{TARGET_FILE!r} not found in toc")


def read_blob(dat_path: Path, offset: int, length: int) -> bytes:
    with dat_path.open("rb") as f:
        f.seek(offset)
        data = f.read(length)
    if len(data) != length:
        raise RuntimeError(f"short read: got {len(data)}, expected {length}")
    return data


def read_utf16_z(blob: bytes, offset: int, max_chars: int = 256) -> str:
    end = offset
    while end + 1 < len(blob):
        if blob[end] == 0 and blob[end + 1] == 0:
            break
        end += 2
        if (end - offset) // 2 >= max_chars:
            break
    return blob[offset:end].decode("utf-16-le", errors="replace")


def read_ascii_z(blob: bytes, offset: int, max_chars: int = 256) -> str:
    end = blob.find(b"\x00", offset, offset + max_chars)
    if end == -1:
        end = offset + max_chars
    return blob[offset:end].decode("ascii", errors="replace")


def parse_dueldata(blob: bytes) -> list[dict]:
    duel_count = struct.unpack_from("<Q", blob, 0)[0]
    if not (0 < duel_count < 100_000):
        raise RuntimeError(f"implausible duelCount {duel_count}")
    print(f"  duelCount = {duel_count}")
    records: list[dict] = []
    for i in range(duel_count):
        rec_off = HEADER_SIZE + i * RECORD_SIZE
        ints = struct.unpack_from("<11i", blob, rec_off)
        offsets = struct.unpack_from("<6q", blob, rec_off + 44)
        (id_, series, displayIndex, playerCharId, opponentCharId,
         playerDeckId, opponentDeckId, arenaId, unk8, dlcId, f11) = ints
        codeNameO, pSkinO, oSkinO, nameO, descO, tipO = offsets
        records.append({
            "index": i,
            "id": id_,
            "series": series,
            "display_index": displayIndex,
            "player_char_id": playerCharId,
            "opponent_char_id": opponentCharId,
            "player_deck_id": playerDeckId,
            "opponent_deck_id": opponentDeckId,
            "arena_id": arenaId,
            "unk8": unk8,
            "dlc_id": dlcId,
            "field11": f11,
            "code_name": read_ascii_z(blob, codeNameO) if codeNameO > 0 else None,
            "name": read_utf16_z(blob, nameO) if nameO > 0 else None,
            "description": read_utf16_z(blob, descO) if descO > 0 else None,
            "tip": read_utf16_z(blob, tipO) if tipO > 0 else None,
        })
    return records


def hex_dump_records(blob: bytes, n: int) -> None:
    duel_count = struct.unpack_from("<Q", blob, 0)[0]
    print(f"duelCount = {duel_count}")
    for i in range(min(n, duel_count)):
        rec_off = HEADER_SIZE + i * RECORD_SIZE
        ints = struct.unpack_from("<11i", blob, rec_off)
        offsets = struct.unpack_from("<6q", blob, rec_off + 44)
        codeNameO, pSkinO, oSkinO, nameO, descO, tipO = offsets
        print(f"\nrec {i} @ +0x{rec_off:X}:")
        print(f"  ints: {ints}")
        print(f"  offsets: codeName=0x{codeNameO:X} pSkin=0x{pSkinO:X} oSkin=0x{oSkinO:X}")
        print(f"           name=0x{nameO:X} desc=0x{descO:X} tip=0x{tipO:X}")
        if codeNameO > 0:
            print(f"  codeName: {read_ascii_z(blob, codeNameO)!r}")
        if nameO > 0:
            print(f"  name:     {read_utf16_z(blob, nameO)!r}")
        if descO > 0:
            print(f"  desc:     {read_utf16_z(blob, descO)[:60]!r}")


def build_duel_table(records: list[dict], path: Path) -> None:
    """Group records by (series, display_index) and emit a Python module.

    Series 0..5 = YuGiOh/GX/5Ds/ZEXAL/ARC-V/VRAINS. Per-series duel counts are
    NOT uniform (the plan's "50 per series × 6 = 300" assumption is wrong); the
    actual file has 32/32/32/26/33/28 = 183 named campaign duels in LE-v2.

    `display_index` in the file is 1-indexed. We emit a `slot` matching that
    1-indexed display position (i.e. `slot = display_index`) so the AP world
    references match what the game shows in the campaign menu.

    The mapping from (series, slot) to the in-memory `CampaignSaveData.Duel[]`
    array index is `slot` (verified empirically 2026-05-05). The in-memory
    array has a placeholder entry at index 0 per series — `Duel[0]` carries
    no `opponent_deck_id` and has no name in dueldata. The first named
    campaign duel ("The Duelist Kingdom" for YuGiOh) sits at array index 1,
    matching its `display_index = 1`.

    TODO (2026-05-05): emit `'tutorial': True` on slot==1 of each series.
    Slot 1 of every series is the in-game tutorial duel; the AP world treats
    it as inactive (no item, no locations) and precollects the slot-2 unlock
    instead. Without this flag, regenerating duel_table.py via this script
    will silently re-introduce the tutorials as AP duels.
    """
    # Determine actual size of each series from the data.
    by_series: dict[int, dict[int, dict]] = {i: {} for i in range(6)}
    series_max_slot: dict[int, int] = {i: 0 for i in range(6)}
    skipped_series = 0
    skipped_slot = 0
    skipped_unnamed = 0
    duplicate = 0
    for r in records:
        if not r.get("name"):
            skipped_unnamed += 1
            continue
        s = r["series"]
        if s not in range(6):
            skipped_series += 1
            continue
        di = r["display_index"]
        if not (1 <= di <= 50):
            skipped_slot += 1
            continue
        if di in by_series[s]:
            existing = by_series[s][di]
            print(
                f"  DUPLICATE (series={s}, slot={di}): keep id={existing['id']} {existing['name']!r}"
                f"  (drop id={r['id']} {r['name']!r})"
            )
            duplicate += 1
            continue
        by_series[s][di] = r
        if di > series_max_slot[s]:
            series_max_slot[s] = di

    print(
        f"\nfilter: skipped_unnamed={skipped_unnamed} "
        f"skipped_bad_series={skipped_series} skipped_bad_slot={skipped_slot} "
        f"duplicates={duplicate}"
    )
    print("series coverage:")
    total_named = 0
    for i, sname in enumerate(SERIES_NAMES):
        slots = by_series[i]
        max_slot = series_max_slot[i]
        missing = [j for j in range(1, max_slot + 1) if j not in slots]
        marker = "" if not missing else f"  GAPS={missing}"
        print(f"  {sname:8s} {len(slots)} slots, max_slot={max_slot}{marker}")
        total_named += len(slots)
    print(f"  TOTAL named duels: {total_named}")

    lines = [
        "# Auto-generated by extract_duel_names.py from YGO_2020.dat (main\\dueldata_E.bin).",
        "# series matches DuelSeries enum (CampaignSaveData.cs IndexToSeries):",
        "#   0=YuGiOh, 1=GX, 2=5Ds, 3=ZEXAL, 4=ARC-V, 5=VRAINS.",
        "# slot is the 1-indexed display_index from dueldata_E.bin (matches the",
        "# campaign menu order). To address CampaignSaveData.Duel[i] in memory,",
        "# use array_index = slot. (Duel[0] is an unnamed placeholder slot.)",
        "",
        f"SERIES_NAMES = {SERIES_NAMES!r}",
        f"SERIES_SLOT_COUNTS = {[series_max_slot[i] for i in range(6)]!r}",
        "",
        "DUELS = [",
    ]
    for i in range(6):
        sname = SERIES_NAMES[i]
        max_slot = series_max_slot[i]
        for slot in range(1, max_slot + 1):
            r = by_series[i].get(slot)
            if r is None:
                lines.append(
                    f"    {{'series': {i}, 'slot': {slot}, 'name': None, "
                    f"'item_id': None, 'opponent_deck_id': None}},  # {sname} #{slot} (gap)"
                )
            else:
                name_lit = repr(r["name"])
                lines.append(
                    f"    {{'series': {i}, 'slot': {slot}, 'name': {name_lit}, "
                    f"'item_id': {r['id']}, 'opponent_deck_id': {r['opponent_deck_id']}}},"
                )
    lines.append("]")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote duel table -> {path}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--install", default=str(DEFAULT_INSTALL),
                   help="game install directory")
    # Defaults assume the script runs from `debug_tools/` (its current home)
    # and emits artifacts to the repo root / ap_world tree.
    _here = Path(__file__).resolve().parent
    _repo_root = _here.parent
    p.add_argument("--raw-out", default=str(_here / "duel_data_raw.json"))
    p.add_argument("--table-out", default=str(_repo_root / "ap_world" / "data" / "duel_table.py"))
    p.add_argument("--raw-only", action="store_true",
                   help="skip duel_table.py emit, only write json")
    p.add_argument("--hex", type=int, metavar="N",
                   help="hex-dump first N records and exit (no json/table emit)")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    install = Path(args.install)
    toc_path = install / TOC_NAME
    dat_path = install / DAT_NAME
    if not toc_path.is_file() or not dat_path.is_file():
        print(f"ERROR: missing toc or dat under {install}")
        return 2
    print(f"toc: {toc_path}")
    print(f"dat: {dat_path}")

    entries = parse_toc(toc_path)
    print(f"parsed {len(entries)} toc entries")
    off, length = find_dueldata_offset(entries)
    print(f"{TARGET_FILE} @ offset 0x{off:X} length 0x{length:X}")
    blob = read_blob(dat_path, off, length)

    if args.hex:
        hex_dump_records(blob, args.hex)
        return 0

    records = parse_dueldata(blob)

    raw_out = Path(args.raw_out)
    raw_out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(records)} records -> {raw_out}")

    if not args.raw_only:
        build_duel_table(records, Path(args.table_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
