"""Standalone smoke test: unlock one shop pack in the running Lotd LE process.

Ports the pointer chain from pixeltris/Lotd MemTools.Addresses.Lotd.cs and
the MiscSaveData layout from MemTools.SaveData/MiscSaveData.cs.

Run with the game already loaded to a save (main menu or card shop screen).

  python unlock_shop_pack.py                  # default: flip Yugi
  python unlock_shop_pack.py --pack Bakura    # flip a named pack
  python unlock_shop_pack.py --bit 27         # flip a raw bit (for VRAINS mapping)
  python unlock_shop_pack.py --read           # read-only, decode current value
  python unlock_shop_pack.py --clear          # zero the field (then flip one bit at a time)
"""
from __future__ import annotations

import argparse
import sys

import pymem


# --- Lotd LE v2 absolute addresses (from MemTools.Addresses.LotdLE_v2.cs) ---
SAVE_DATA_ADDRESS_1 = 0x142924010
SAVE_DATA_MEM_OFFSET = 80
SAVE_DATA_ADDRESS_OFFSET = 10208
MISC_DATA_OFFSET_LE2 = 4056
NUM_DECK_DATA_SLOTS_LE2 = 700

# UnlockedShopPacks bit positions (from MiscSaveData.cs enum).
SHOP_PACKS = {
    "Grandpa Muto":    1 << 1,
    "Mai Valentine":   1 << 2,
    "Bakura":          1 << 3,
    "Joey Wheeler":    1 << 4,
    "Seto Kaiba":      1 << 5,
    "Yugi":            1 << 6,
    "Alexis Rhodes":   1 << 7,
    "Bastion Misawa":  1 << 8,
    "Chazz Princeton": 1 << 9,
    "Syrus Truesdale": 1 << 10,
    "Jesse Anderson":  1 << 11,
    "Jaden Yuki":      1 << 12,
    "Tetsu Trudge":    1 << 13,
    "Leo Luna":        1 << 14,
    "Akiza Izinski":   1 << 15,
    "Jack Atlas":      1 << 16,
    "Crow":            1 << 17,
    "Yusei Fudo":      1 << 18,
    "Cathy Katherine": 1 << 19,
    "Quinton":         1 << 20,
    "Kite Tenjo":      1 << 21,
    "Shark":           1 << 22,
    "Yuma Tsukumo":    1 << 23,
    "Pendulum":        1 << 24, # this pack seems to have been removed from the game
    "Gong Strong":     1 << 25,
    "Zuzu Boyle":      1 << 26,
    "Shay":            1 << 27,
    "Declan Akaba":    1 << 28,
    "Yuya Sakaki":     1 << 29,
    "Playmaker":       1 << 30,
}

# UnlockedDlcShopPacks — uint32 at saveData+7000+4 (file offset 0x1B5C).
# Source pixeltris/Lotd calls this field UnlockedBattlePacks, but in LE-v2 it
# holds VRAINS shop packs, not battle packs. Bits 0-1 unmapped.
# See battle_pack_field_notes.md for the battle-pack-flag investigation.
# Mapped empirically 2026-05-02.
DLC_SHOP_PACKS = {
    "Blue Angel": 1 << 2,
    "Soulburner": 1 << 3,
    "Varis":      1 << 4,
    "Ai":         1 << 5,
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
    raise RuntimeError(
        f"could not attach to any of {CANDIDATE_EXE_NAMES}; last error: {last_err}"
    )


def get_base_save_data_address(pm: pymem.Pymem) -> int:
    """Verbatim port of MemTools.Addresses.Lotd.cs::GetBaseSaveDataAddress."""
    v2 = pm.read_int(SAVE_DATA_ADDRESS_1 + 176)
    print(f"  v2 (slot index)       = {v2}")

    v3 = pm.read_ulonglong(SAVE_DATA_ADDRESS_1)
    print(f"  v3 = [0x{SAVE_DATA_ADDRESS_1:X}] = 0x{v3:X}")

    v4 = pm.read_ulonglong(v3 + 8)
    print(f"  v4 = [v3+8]           = 0x{v4:X}")

    v5 = v3

    # Tree traversal — bail out if it looks runaway.
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
    print(f"  result = [v5+40]      = 0x{result:X}")
    if result == 0:
        raise RuntimeError("save-data result pointer is null — is a save loaded?")

    base = pm.read_ulonglong(result + SAVE_DATA_MEM_OFFSET)
    print(f"  base = [result+{SAVE_DATA_MEM_OFFSET}]    = 0x{base:X}")
    return base


def get_save_data_address(pm: pymem.Pymem) -> int:
    base = get_base_save_data_address(pm)
    save = pm.read_ulonglong(base + SAVE_DATA_ADDRESS_OFFSET)
    print(f"  saveData = [base+{SAVE_DATA_ADDRESS_OFFSET}] = 0x{save:X}")
    return save


def unlocked_shop_packs_address(save_data: int) -> int:
    misc = save_data + MISC_DATA_OFFSET_LE2
    # Layout: 16 header + 8 DuelPoints + 32 UnlockedAvatars + 4*N challenges
    # + recipe bitfield (ceil(N/8)).
    recipe_bytes = (NUM_DECK_DATA_SLOTS_LE2 + 7) // 8
    offset = 16 + 8 + 32 + (4 * NUM_DECK_DATA_SLOTS_LE2) + recipe_bytes
    print(f"  miscData = saveData+{MISC_DATA_OFFSET_LE2} = 0x{misc:X}")
    print(f"  unlockedShopPacks @ miscData+{offset} = 0x{misc + offset:X}")
    return misc + offset


def decode_packs(value: int) -> str:
    names = [name for name, bit in SHOP_PACKS.items() if value & bit]
    extra = value & ~sum(SHOP_PACKS.values())
    if extra:
        names.append(f"unknown(0x{extra:X})")
    return ", ".join(names) if names else "(none)"


# UnlockedContent flags (from MiscSaveData.cs).
UNLOCKED_CONTENT = {
    "DuelistChallenges": 0x1,
    "BattlePack":        0x2,
    "CardShop":          0x4,
}


def unlocked_content_address(save_data: int) -> int:
    """`UnlockedContent` lives 20 bytes past `UnlockedShopPacks`:
    +4 BattlePacks, +8 padding, +4 CompleteTutorials = 20."""
    return unlocked_shop_packs_address(save_data) + 20


def decode_content(value: int) -> str:
    names = [n for n, b in UNLOCKED_CONTENT.items() if value & b]
    extra = value & ~sum(UNLOCKED_CONTENT.values())
    if extra:
        names.append(f"unknown(0x{extra:X})")
    return ", ".join(names) if names else "(none)"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lotd LE shop-pack memory poke.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pack", help="named pack to unlock (e.g. Yugi, Pendulum)")
    g.add_argument("--bit", type=int, help="raw bit position 0..31 to OR in")
    g.add_argument("--read", action="store_true", help="read-only; decode current bitfield")
    g.add_argument("--clear", action="store_true", help="zero the entire UnlockedShopPacks field")
    g.add_argument(
        "--unlock-content",
        choices=list(UNLOCKED_CONTENT) + ["All"],
        help="OR a flag into UnlockedContent (CardShop, BattlePack, DuelistChallenges, All)",
    )
    g.add_argument(
        "--scan-after",
        type=int,
        nargs="?",
        const=64,
        metavar="BYTES",
        help="dump N bytes (default 64) right after UnlockedContent — for hunting unknown VRAINS-pack fields",
    )
    g.add_argument(
        "--write-after",
        type=int,
        metavar="OFFSET",
        help="OR 0xFFFFFFFF into the uint32 at UnlockedContent+OFFSET (multiples of 4); use to probe candidate fields",
    )
    g.add_argument(
        "--clear-bit",
        nargs=2,
        metavar=("OFFSET", "BIT"),
        help="clear BIT (0..31) in the uint32 at UnlockedShopPacks+OFFSET. OFFSET=0 → UnlockedShopPacks, 4 → UnlockedDlcShopPacks",
    )
    g.add_argument(
        "--set-bit",
        nargs=2,
        metavar=("OFFSET", "BIT"),
        help="set BIT (0..31) in the uint32 at UnlockedShopPacks+OFFSET. OFFSET=0 → UnlockedShopPacks, 4 → UnlockedDlcShopPacks",
    )
    g.add_argument(
        "--probe-byte",
        nargs="+",
        metavar="SAVE_OFFSET",
        help="dump 16 bytes around saveData+OFFSET (offset accepts 0x... hex or decimal). Multiple offsets allowed.",
    )
    g.add_argument(
        "--write-byte",
        nargs=2,
        metavar=("SAVE_OFFSET", "VALUE"),
        help="write VALUE (0..255) to byte at saveData+OFFSET. Both args accept 0x... hex or decimal.",
    )
    args = p.parse_args(argv)
    for attr, flag in (("clear_bit", "--clear-bit"), ("set_bit", "--set-bit")):
        val = getattr(args, attr)
        if val is None:
            continue
        try:
            off, bit = int(val[0]), int(val[1])
        except ValueError:
            p.error(f"{flag} args must be integers")
        if off < 0 or off % 4 != 0:
            p.error(f"{flag} OFFSET must be a non-negative multiple of 4")
        if not 0 <= bit <= 31:
            p.error(f"{flag} BIT must be in 0..31")
        setattr(args, attr, (off, bit))
    if args.bit is not None and not 0 <= args.bit <= 31:
        p.error("--bit must be in 0..31")
    all_packs = list(SHOP_PACKS) + list(DLC_SHOP_PACKS)
    if args.pack and args.pack not in SHOP_PACKS and args.pack not in DLC_SHOP_PACKS:
        p.error(f"unknown pack {args.pack!r}; known: {', '.join(all_packs)}")
    if args.scan_after is not None and (args.scan_after <= 0 or args.scan_after % 4 != 0):
        p.error("--scan-after BYTES must be a positive multiple of 4")
    if args.write_after is not None and (args.write_after < 0 or args.write_after % 4 != 0):
        p.error("--write-after OFFSET must be a non-negative multiple of 4")
    if args.probe_byte is not None:
        try:
            args.probe_byte = [int(s, 0) for s in args.probe_byte]
        except ValueError:
            p.error("--probe-byte offsets must be integers (decimal or 0x... hex)")
        if any(o < 0 for o in args.probe_byte):
            p.error("--probe-byte offsets must be non-negative")
    if args.write_byte is not None:
        try:
            wb_off, wb_val = int(args.write_byte[0], 0), int(args.write_byte[1], 0)
        except ValueError:
            p.error("--write-byte args must be integers (decimal or 0x... hex)")
        if wb_off < 0:
            p.error("--write-byte OFFSET must be non-negative")
        if not 0 <= wb_val <= 255:
            p.error("--write-byte VALUE must be in 0..255")
        args.write_byte = (wb_off, wb_val)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    pm = attach()
    print(f"saveDataAddress1 = 0x{SAVE_DATA_ADDRESS_1:X}")

    save_data = get_save_data_address(pm)
    addr = unlocked_shop_packs_address(save_data)

    content_addr = unlocked_content_address(save_data)
    content = pm.read_int(content_addr)
    print(f"  unlockedContent  @ 0x{content_addr:X} = 0x{content:08X} ({decode_content(content)})")

    dlc_addr = addr + 4
    dlc = pm.read_uint(dlc_addr)
    dlc_named = [n for n, b in DLC_SHOP_PACKS.items() if dlc & b]
    dlc_decoded = ", ".join(dlc_named) if dlc_named else "(none)"
    print(f"  unlockedDlcShopPacks @ 0x{dlc_addr:X} = 0x{dlc:08X} ({dlc_decoded})")

    before = pm.read_uint(addr)
    print(f"\nbefore = 0x{before:08X} ({decode_packs(before)})")

    if args.read:
        return 0

    if args.probe_byte is not None:
        for off in args.probe_byte:
            window = 16
            start = save_data + off - window // 2
            raw = pm.read_bytes(start, window)
            hex_bytes = " ".join(f"{b:02X}" for b in raw)
            ascii_bytes = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
            target_idx = window // 2
            print(f"\nsaveData+0x{off:X} (file/mem offset) at 0x{save_data + off:X}:")
            print(f"  bytes [0x{off - window//2:X}..0x{off + window//2 - 1:X}]:")
            print(f"  {hex_bytes}  |{ascii_bytes}|")
            marker = "    " * target_idx + "^^"
            print(f"  {marker}  ← byte at +0x{off:X} = 0x{raw[target_idx]:02X} ({raw[target_idx]})")
        return 0

    if args.write_byte is not None:
        wb_off, wb_val = args.write_byte
        target = save_data + wb_off
        before_byte = pm.read_uchar(target)
        print(f"\nwriting 0x{wb_val:02X} to saveData+0x{wb_off:X} (0x{target:X})")
        print(f"  before = 0x{before_byte:02X} ({before_byte})")
        pm.write_uchar(target, wb_val)
        readback = pm.read_uchar(target)
        print(f"  after  = 0x{readback:02X} ({readback})")
        if readback != wb_val:
            print("WRITE FAILED — value did not stick.")
            return 1
        return 0

    if args.scan_after is not None:
        n = args.scan_after
        scan_addr = addr + 4  # start right past UnlockedContent
        print(f"\nscan: {n} bytes @ 0x{scan_addr:X} (UnlockedContent+4)")
        raw = pm.read_bytes(scan_addr, n)
        for i in range(0, n, 4):
            word = int.from_bytes(raw[i:i+4], "little")
            popcount = bin(word).count("1")
            note = ""
            if word == 0:
                note = "  (zero)"
            elif word == 0xFFFFFFFF:
                note = "  (all-ones)"
            elif 0 < popcount <= 32 and word < 0x100000000:
                note = f"  (popcount={popcount} → looks bitfield-y)" if popcount <= 16 else ""
            print(f"  +{i:03d} @ 0x{scan_addr + i:X}  0x{word:08X}{note}")
        return 0

    if args.write_after is not None:
        target_addr = content_addr + 4 + args.write_after
        before_word = pm.read_uint(target_addr)
        print(
            f"\nORing 0xFFFFFFFF into uint32 at "
            f"UnlockedContent+4+{args.write_after} (0x{target_addr:X})"
        )
        print(f"  before = 0x{before_word:08X}")
        pm.write_uint(target_addr, before_word | 0xFFFFFFFF)
        readback = pm.read_uint(target_addr)
        print(f"  after  = 0x{readback:08X}")
        if readback != 0xFFFFFFFF:
            print("WRITE FAILED — value did not stick.")
            return 1
        print("\nOK — re-enter the card shop and note which previously-locked packs now appear.")
        print("If nothing new appears, this field probably isn't the missing-packs bitfield.")
        return 0

    if args.unlock_content:
        flag = 0x7 if args.unlock_content == "All" else UNLOCKED_CONTENT[args.unlock_content]
        new_content = content | flag
        print(f"\nORing UnlockedContent {args.unlock_content} (0x{flag:X})")
        pm.write_int(content_addr, new_content)
        readback = pm.read_int(content_addr)
        print(f"unlockedContent after = 0x{readback:08X} ({decode_content(readback)})")
        if not (readback & flag):
            print("WRITE FAILED — value did not stick.")
            return 1
        print("\nOK — re-enter the main menu and the corresponding menu entry should appear.")
        return 0

    if args.clear_bit is not None:
        cb_off, cb_bit = args.clear_bit
        target_addr = addr + cb_off
        mask = 1 << cb_bit
        before_word = pm.read_uint(target_addr)
        print(
            f"\nclearing bit {cb_bit} (0x{mask:X}) at "
            f"UnlockedShopPacks+{cb_off} (0x{target_addr:X})"
        )
        print(f"  before = 0x{before_word:08X}")
        pm.write_uint(target_addr, before_word & ~mask & 0xFFFFFFFF)
        readback = pm.read_uint(target_addr)
        print(f"  after  = 0x{readback:08X}")
        if readback & mask:
            print("WRITE FAILED — bit still set.")
            return 1
        return 0

    if args.set_bit is not None:
        sb_off, sb_bit = args.set_bit
        target_addr = addr + sb_off
        mask = 1 << sb_bit
        before_word = pm.read_uint(target_addr)
        print(
            f"\nsetting bit {sb_bit} (0x{mask:X}) at "
            f"UnlockedShopPacks+{sb_off} (0x{target_addr:X})"
        )
        print(f"  before = 0x{before_word:08X}")
        pm.write_uint(target_addr, before_word | mask)
        readback = pm.read_uint(target_addr)
        print(f"  after  = 0x{readback:08X}")
        if not (readback & mask):
            print("WRITE FAILED — bit not set.")
            return 1
        print("\nOK — re-enter the in-game card shop and note which pack appeared.")
        return 0

    if args.clear:
        print("clearing UnlockedShopPacks (writing 0x00000000)")
        pm.write_uint(addr, 0)
        after = pm.read_uint(addr)
        print(f"after  = 0x{after:08X} ({decode_packs(after)})")
        if after != 0:
            print("WRITE FAILED — value did not stick.")
            return 1
        return 0

    if args.bit is not None:
        bit_pos = args.bit
        bit = 1 << bit_pos
        named = next((n for n, v in SHOP_PACKS.items() if v == bit), None)
        label = f"bit {bit_pos} (0x{bit:X})" + (f" = {named}" if named else " [UNNAMED]")
        target_addr = addr
        target_before = before
    else:
        pack = args.pack or "Yugi"
        if pack in SHOP_PACKS:
            bit = SHOP_PACKS[pack]
            target_addr = addr
            target_before = before
        else:
            bit = DLC_SHOP_PACKS[pack]
            target_addr = dlc_addr
            target_before = dlc
        label = f"{pack} (0x{bit:X})"

    if target_before & bit:
        print(f"{label} is already set — try a different bit, or --clear first.")
        return 1

    print(f"ORing {label} at 0x{target_addr:X}")
    pm.write_uint(target_addr, target_before | bit)

    after = pm.read_uint(target_addr)
    print(f"after  = 0x{after:08X}")

    if not (after & bit):
        print("WRITE FAILED — value did not stick.")
        return 1

    print(f"\nOK — re-enter the in-game card shop and note which pack appeared for {label}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
