# -*- coding: utf-8 -*-
"""Offline: extract archetype -> [card_index] map from the game archive.

Reads `bin/CARD_Named.bin` out of `YGO_2020.dat` (located via `YGO_2020.toc`)
and emits `archetypes.json`:

    {
      "archetypes": {
        "<ArchetypeName>": [<index>, <index>, ...],
        ...
      },
      "by_index": {
        "<index>": ["<ArchetypeName>", ...],
        ...
      }
    }

Card indices match the internal index used everywhere else in this project
(`card_list.py::CARDS[i].index`, the `cardPropsBinAddress` array slot, the
`CardListSaveData` byte offset, etc).

Source mapping:
- TOC format: pixeltris `Lotd/LotdArchive.cs::Load` (plaintext, cumulative
  4-byte-aligned offsets, leading `UT` line skipped).
- CARD_Named.bin format: pixeltris `Lotd/FileFormats/bin/CardManager.cs::
  LoadCardNameTypes`.
- Archetype names: `CardNameType` enum in the same file (index = enum value).

Usage:
  python extract_archetypes.py
  python extract_archetypes.py --install-dir "F:/.../Link Evolution"
  python extract_archetypes.py --out archetypes.json
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


DEFAULT_INSTALL_DIR = Path(
    "F:/SteamLibrary/steamapps/common/"
    "Yu-Gi-Oh! Legacy of the Duelist Link Evolution"
)
TOC_NAME = "YGO_2020.toc"
DAT_NAME = "YGO_2020.dat"
TARGET_FILE = "bin/CARD_Named.bin"
TOC_ALIGN = 4


# CardNameType enum from tools_ref/CardManager.cs:1204-1507. Index = enum value.
# The first entry (`Null`) is a sentinel; LoadCardNameTypes still allocates a
# bucket for it but typically zero cards land there. Kept in the list to keep
# enum-index alignment trivial — filtered out at emit time.
ARCHETYPE_NAMES: list[str] = [
    "Null", "Toon", "Archfiend", "Gravekeeper", "Guardian", "Scorpion", "Amazoness",
    "Ninja", "Level", "Elemental Hero", "Destiny Hero", "NeosMaterial", "NeosFusion", "Neos",
    "Ojama", "Battery", "Dark World", "BES", "Ancient Gear", "Sphinx", "Machina",
    "Harpie", "Roid", "Vehicloid", "Neo-Spacian", "Cocoon", "Alien", "Mythical",
    "Hero", "Allure", "Gadget", "Six Samurai", "Crystal Beast", "Volcanic", "Blaze",
    "Venom", "Cloudian", "Glad Beasts", "Glad Beast Weapon", "Bamboo Sword", "Evil Hero", "Empty13",
    "Arcana", "Fossil", "Skyblaster", "Forbidden", "Rainbow", "Cyber Fusion",
    "Ice Barrier", "Ally of Justice", "Saber", "Worm", "Lightsworn", "Frog", "Nitro",
    "Genex", "MistValley", "Flamebell", "NeosNHERO", "Deformer", "Chain",
    "Natul", "Clear", "RedEyes", "BlackFeather", "SlashBuster", "Roaring",
    "Jurac", "RealGenex", "EarthbindGod", "Koakimail", "Infernity", "X_Saber",
    "FortuneLady", "Dragnity", "FortuneWitch", "Synchron", "Saviour",
    "Reptiles", "Shien", "Junk", "Tomabo", "Sin", "Gem", "GemKnight", "Laval",
    "Vailon", "Scrap", "Eleki", "Fusion", "Infinity", "Wisel", "TG",
    "Karakuri", "Ritua", "Gusta", "Invelds", "Reactor", "Agent", "Polestar",
    "PolestarBeast", "PolestarGhost", "PolestarAngel", "PolestarItem",
    "PoleGod", "SoundWarrior", "Resonator", "MHERO", "VHERO", "Meklord_Emp",
    "Meklord_Sld", "Meklord", "Zenmai", "Penguin", "Evold", "Evolder",
    "TrapHole", "TimeGod", "Sacred", "Velds", "Numbers", "Gagaga", "Gogogo",
    "Photon", "Ninjutsu", "Inzector", "Invasion", "Bouncer", "Butterfly",
    "HolySeal", "Majin", "Heroic", "Ooparts", "Spellbook", "Madolce",
    "Geargear", "Xyz", "Poseidon", "Mermail", "Abyss", "Magical", "Nimble",
    "Duston", "Medallion", "Noble Knight", "Fire King", "Galaxy", "HolySword",
    "FireStar", "FireDance", "HazeBeast", "Haze", "ZexalWeapon", "Hope",
    "GimmickPuppet", "Dododo", "BK", "PhantomMek", "FireKingBeast",
    "ChaosNumbers", "ChaosXyz", "Geargearno", "SDRobo", "SDRobo2", "Umbral",
    "HolyLightning", "Bujin", "Kowakuma", "Hole", "CNo39", "H_Challenger",
    "Malicebolus", "Ghostrick", "Vampire", "Cat", "CyberDragon", "Cybernetic",
    "Shinra", "Necrovalley", "Zubaba", "Fishborg", "RUM", "Medallion2",
    "Artifact", "Evolkaiser", "GalaxyEyes", "Tachyon", "Over100", "Wizard",
    "OddEyes", "LegendDragon", "LegendKnight", "WingedKuriboh", "Stardust",
    "Sprout", "Artorius", "Lancelot", "Superheavy", "Genso", "Tellarknight",
    "Shadoll", "DragonStar", "EM", "Change", "Higan", "UA", "DD", "DDD",
    "Furnimal", "Deathtoy", "Qliphot", "Bunborg", "Goblin", "Cthulhu",
    "Contract", "Gottoms", "Yosen", "Necroth", "Spirit_All", "Spirit_Tamer",
    "Spirit_Beast", "RR", "Infernoid", "Jinzo", "Gaia", "Monarch", "Charmer",
    "Possessed", "Crystal", "Warrior", "PowerTool", "BMG", "EdgeImp",
    "Sephira", "GensoPrincess", "Spirit_Rider", "Stellarknight", "Void", "Em",
    "Dragonsword", "Igknight", "Aroma", "Empowered", "AetherWeapon",
    "FortunePrince", "Aquaactress", "Aquarium", "ChaosSoldier", "Majespecter",
    "Gradle", "SOz", "Kaiju", "SR", "PSYFrame", "RedDemon", "Burgestoma",
    "Dante", "BusterBlader", "BusterSword", "Dynamist", "Shiranui",
    "Dragondevil", "Exodia", "PhantomKnight", "Phantom", "Super",
    "Super_Quantum", "Super_Machine", "BlueEyes", "HopeX", "Moonlight",
    "Amorphage", "ElfSwordsman", "MagicianGirl", "BlackMagic", "Metalphose",
    "Tramid", "ABF", "Houkai", "Chaos", "CyberAngel", "Cypher", "Cardian",
    "SilentSword", "SilentMagic", "MagnetWarrior", "BlackMagic2", "Kuriboh",
    "Crystron", "Kagoju", "ApoQliphot", "Chichukai", "ChichukaiRyu", "Spyral",
    "SpyralGear", "MakaiGekidan", "MakaiDaihon", "FallenAngel", "WW",
    "Beast12", "PendDragon", "SpyralBackRow", "Predaplants", "Predaplants2",
    "SuperheavySamSoul", "Invoked", "DarkXYZDragon", "ClearWing", "Venom2", 
    "PendulumGraph", "Skyscraper", "Magician", "Lyrilusc", "Supreme King", 
    "Supreme King Dragon", "True Draco", "Phantasm Spiral", "Pendulum", 
    "Gandora", "Trickstar", "Gouki", "World Chalice", "World Legacy",
    "Clear Wing 2", "Venom3", "Cyberdark", "Bonding", "Code Talker", 
    "Rokket Dragon", "Altergeist", "Krawler", "Metaphys", "Vendread", "F.A.",
    "Magical Musket", "Weather", "Parshath", "Secret Six", "Tindangle",
    "Mekk-Knight", "Mythical Beast", "Evolution Pill", "Borrel", 
    "Eyes Restrict", "Armed Dragon", "Spell Gear", "Knightmare", "Element Saber",
    "Elemental Lord", "Fur Hire", "Sky Striker Ace", "Sky Striker", "Crusadia",
    "Impcantation", "Blue-Eyes Support", "Fairy Tale", "Cynet", "Salamangreat",
    "Dinowrestler", "Orcust", "Thunder Dragon", "Forbidden Items", "Danger",
    "Galaxy Photon", "Nephthys", "Prank-Kids", "Mayakashi", "Valkyrie", 
    "Shiranui Sword", "Neos Support", "Harpie Support", "Machine Angel", 
    "Rose Dragon", "Unknown", "Shien Support", "Smile", "Assault", "Time Thief",
    "Infinitrack", "Witchcrafter", "Evil Eye", "Endymion", "Marincess", "Tenyi", 
    "Simorgh", "Battlewasp", "Destiny Board", "Evil Hero Support", "Unchained",
    "Unchained Soul", "Empty1", "Mathmech", "Dragonmaid", "Generaider", 
    "@Ignister", "A.I.", "Ancient Warriors", "Megalith", "Palladium Oracle",
    "Onomat", "Utopic Future", "Rose Support", "Rebellion Dragon", "Empty2", "Empty3",
    "Beast King", "Empty4", "Empty5", "Empty6", "Empty7", "Empty8", "Empty9",
    "Chaos Phantom", "Lord of Phantoms", "Spiral Spear", "Empty10", "Empty11", 
    "Potan", "Empty12", 

]


def parse_toc(toc_path: Path) -> dict[str, tuple[int, int]]:
    """Return {posix_path: (offset, length)}.

    Format (from LotdArchive.cs:125-184):
      - First line is `UT` (skip).
      - Each file line: leading spaces, then `<length_hex> <pathlen_hex> <path>`.
      - Cumulative file offset starts at 0; after each file advance by length,
        then pad up to 4-byte alignment.
    """
    text = toc_path.read_text(encoding="ascii", errors="strict")
    files: dict[str, tuple[int, int]] = {}
    offset = 0
    for line in text.splitlines():
        if line.startswith("UT"):
            continue
        if not line.strip():
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError(f"bad toc line: {line!r}")
        length_hex, pathlen_hex, path = parts
        length = int(length_hex, 16)
        pathlen = int(pathlen_hex, 16)
        if pathlen != len(path):
            raise ValueError(
                f"toc pathlen mismatch: {pathlen} vs {len(path)} for {path!r}"
            )
        files[path.replace("\\", "/")] = (offset, length)
        offset += length
        if length % TOC_ALIGN != 0:
            offset += TOC_ALIGN - (length % TOC_ALIGN)
    return files


def read_archive_blob(dat_path: Path, offset: int, length: int) -> bytes:
    with dat_path.open("rb") as f:
        f.seek(offset)
        buf = f.read(length)
    if len(buf) != length:
        raise IOError(f"short read at offset {offset}: got {len(buf)}/{length}")
    return buf


def parse_card_named(blob: bytes) -> dict[int, list[int]]:
    """Return {archetype_index: [card_index, ...]}.

    Format (CardManager.cs::LoadCardNameTypes):
      uint16 numArchetypes
      uint16 numCards
      numArchetypes * (int16 offset, int16 count)   # offset is into card-id blob (in int16 units)
      int16 cardIds[numCards]
    """
    if len(blob) < 4:
        raise ValueError("CARD_Named.bin too small")
    num_arch, num_cards = struct.unpack_from("<HH", blob, 0)
    cards_start = 4 + num_arch * 4
    cards_end = cards_start + num_cards * 2
    if cards_end != len(blob):
        raise ValueError(
            f"size mismatch: header says {cards_end}, file is {len(blob)}"
        )
    result: dict[int, list[int]] = {}
    for i in range(num_arch):
        off, count = struct.unpack_from("<hh", blob, 4 + i * 4)
        ids_off = cards_start + off * 2
        ids = list(struct.unpack_from(f"<{count}h", blob, ids_off))
        result[i] = ids
    return result, num_arch, num_cards


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-dir", type=Path, default=DEFAULT_INSTALL_DIR)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).parent / "old_archetypes.json",
    )
    args = ap.parse_args()

    toc_path = args.install_dir / TOC_NAME
    dat_path = args.install_dir / DAT_NAME
    if not toc_path.is_file() or not dat_path.is_file():
        print(
            f"ERROR: missing {TOC_NAME} or {DAT_NAME} under {args.install_dir}",
            file=sys.stderr,
        )
        return 1

    print(f"Parsing TOC: {toc_path}")
    files = parse_toc(toc_path)
    if TARGET_FILE not in files:
        print(f"ERROR: {TARGET_FILE} not in TOC", file=sys.stderr)
        return 1
    off, length = files[TARGET_FILE]
    print(f"  {TARGET_FILE}: offset=0x{off:X} length={length}")

    blob = read_archive_blob(dat_path, off, length)
    by_arch, num_arch, num_cards = parse_card_named(blob)
    print(f"  numArchetypes={num_arch} numCards={num_cards}")

    if num_arch != len(ARCHETYPE_NAMES):
        print(
            f"WARNING: enum has {len(ARCHETYPE_NAMES)} entries but file has "
            f"{num_arch} archetypes — name alignment may be off past index "
            f"{min(num_arch, len(ARCHETYPE_NAMES)) - 1}",
            file=sys.stderr,
        )

    archetypes: dict[str, list[int]] = {}
    by_index: dict[int, list[str]] = {}
    skipped_null = 0
    for arch_idx, ids in by_arch.items():
        if arch_idx == 0:
            skipped_null = len(ids)
            continue
        if arch_idx >= len(ARCHETYPE_NAMES):
            name = f"Unknown_{arch_idx}"
        else:
            name = ARCHETYPE_NAMES[arch_idx]
        archetypes[name] = sorted(set(ids))
        for cid in ids:
            by_index.setdefault(cid, []).append(name)
    for cid in by_index:
        by_index[cid].sort()

    populated = sum(1 for v in archetypes.values() if v)
    total_tags = sum(len(v) for v in archetypes.values())
    print(
        f"  archetypes with cards: {populated}/{len(archetypes)}  "
        f"total tag rows: {total_tags}  cards tagged: {len(by_index)}"
    )
    if skipped_null:
        print(f"  skipped Null bucket: {skipped_null} entries")

    payload = {
        "archetypes": archetypes,
        "by_index": {str(k): v for k, v in sorted(by_index.items())},
    }
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
