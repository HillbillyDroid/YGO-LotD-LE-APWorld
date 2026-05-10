# -*- coding: utf-8 -*-
"""Dump CARD_Prop.bin from running Lotd LE process + scrape wiki for names.

Writes intermediate `card_props_raw.json` keyed by internal index. Does NOT do
archetype tagging — that's done offline by `build_card_list.py`.

Source: pixeltris/Lotd `MemTools.Structures.cs::CardProps` (48-byte struct, in
memory it is unpacked — disk file is bitfield-packed via LoadCardProp).
Names: scraped from MoonlitDeath wiki Card-ID page (the "Card ID" column there
is the internal index, matching `cardPropsBinAddress` array index).

Run with the game already loaded to a save (any screen — props live in
.rdata-adjacent memory, no save-data pointer chain needed).

  python dump_card_props.py
  python dump_card_props.py --no-wiki   # dump props only, no name scrape
  python dump_card_props.py --wiki-cache wiki_cardid.html   # offline reuse
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import urllib.request
from pathlib import Path

import pymem


# --- Lotd LE v2 absolute addresses ---
CARD_PROPS_BIN_ADDRESS = 0x142847E50
MAX_CARD_ID_LE2 = 14969           # Constants.GetMaxCardId(LinkEvolution2)
NUM_RECORDS = MAX_CARD_ID_LE2 + 1 # 14970
CARD_PROPS_RECORD_SIZE = 48       # Marshal.SizeOf(CardProps)

CANDIDATE_EXE_NAMES = ["Lotd.exe", "LotdLE.exe", "YuGiOh.exe", "Yu-Gi-Oh!.exe"]

WIKI_URL = "https://github.com/MoonlitDeath/Link-Evolution-Editing-Guide/wiki/Card-ID"


# --- Enum decoders (from FileFormats/bin/CardManager.cs) ---
CARD_TYPE = {
    0: "default", 1: "effect", 2: "fusion", 3: "fusion_effect", 4: "ritual",
    5: "ritual_effect", 6: "toon_effect", 7: "spirit_effect", 8: "union_effect",
    9: "gemini_effect", 10: "token", 13: "spell", 14: "trap", 15: "tuner",
    16: "tuner_effect", 17: "synchro", 18: "synchro_effect",
    19: "synchro_tuner_effect", 20: "dark_tuner_effect", 21: "dark_synchro_effect",
    22: "xyz", 23: "xyz_effect", 24: "flip_effect", 25: "pendulum",
    26: "pendulum_effect", 27: "effect_sp", 28: "toon_effect_sp",
    29: "spirit_effect_sp", 30: "tuner_effect_sp", 31: "dark_tuner_effect_sp",
    32: "flip_tuner_effect", 33: "pendulum_tuner_effect",
    34: "xyz_pendulum_effect", 35: "pendulum_flip_effect", 43: "link",
}

ATTRIBUTE = {
    0: None, 1: "light", 2: "dark", 3: "water", 4: "fire", 5: "earth",
    6: "wind", 7: "divine", 8: "spell", 9: "trap",
}

MONSTER_TYPE = {
    0: None, 1: "dragon", 2: "zombie", 3: "fiend", 4: "pyro", 5: "sea_serpent",
    6: "rock", 7: "machine", 8: "fish", 9: "dinosaur", 10: "insect", 11: "beast",
    12: "beast_warrior", 13: "plant", 14: "aqua", 15: "warrior",
    16: "winged_beast", 17: "fairy", 18: "spellcaster", 19: "thunder",
    20: "reptile", 21: "psychic", 22: "wyrm", 23: "divine_beast",
    24: "creator_god", 25: "spell", 26: "trap",
}

SPELL_TYPE = {
    0: "normal", 1: "counter", 2: "field", 3: "equip", 4: "continuous",
    5: "quick_play", 6: "ritual",
}

# CardType values that are spells / traps (not monsters).
NON_MONSTER_TYPES = {13, 14}  # Spell, Trap


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


def read_card_props_array(pm: pymem.Pymem) -> list[dict]:
    total_bytes = NUM_RECORDS * CARD_PROPS_RECORD_SIZE
    print(f"reading {NUM_RECORDS} records ({total_bytes} bytes) from 0x{CARD_PROPS_BIN_ADDRESS:X}")
    raw = pm.read_bytes(CARD_PROPS_BIN_ADDRESS, total_bytes)
    out = []
    for idx in range(NUM_RECORDS):
        off = idx * CARD_PROPS_RECORD_SIZE
        # 12 int32s, see CardProps struct.
        fields = struct.unpack_from("<12i", raw, off)
        (cardId, atk_raw, def_raw, cardType, monsterType, attribute,
         level, spellType, _unk1, pendScale1, pendScale2, _unk2) = fields
        # cardId is short — high bits of int32 are noise; mask.
        passcode = cardId & 0xFFFF
        # ATK/DEF stored as value/10 in memory.
        atk = atk_raw * 10
        defense = def_raw * 10
        out.append({
            "index": idx,
            "passcode": passcode,
            "card_type_raw": cardType,
            "card_type": CARD_TYPE.get(cardType),
            "monster_type_raw": monsterType,
            "monster_type": MONSTER_TYPE.get(monsterType),
            "attribute_raw": attribute,
            "attribute": ATTRIBUTE.get(attribute),
            "spell_type_raw": spellType,
            "spell_type": SPELL_TYPE.get(spellType),
            "level": level,
            "atk": atk,
            "def": defense,
            "pendulum_scale_left": pendScale1,
            "pendulum_scale_right": pendScale2,
        })
    return out


def is_empty_record(rec: dict) -> bool:
    """Most slots in 0..14969 are empty (no card)."""
    return (rec["passcode"] == 0 and rec["card_type_raw"] == 0
            and rec["attribute_raw"] == 0 and rec["monster_type_raw"] == 0
            and rec["atk"] == 0 and rec["def"] == 0 and rec["level"] == 0)


# --- Wiki name scraping ---
# Lines after tag-stripping look like: `Name,12345` (each entry was one <br>-
# terminated line). Names may contain commas (e.g. "Castel, the Skyblaster
# Musketeer") and curly quotes (e.g. `"Maxx ""C"""`), so we anchor on a
# trailing `,<digits>` and treat everything before as the name.
_NAME_ID_RE = re.compile(r"^(.{2,200}?),\s*(\d{2,5})\s*$", re.M)


def fetch_wiki_html(cache: Path | None) -> str:
    if cache and cache.exists():
        print(f"using cached wiki html: {cache}")
        return cache.read_text(encoding="utf-8")
    print(f"fetching {WIKI_URL}")
    req = urllib.request.Request(WIKI_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        # Strict utf-8 — if it fails we want to know rather than silently
        # mangle smart-quotes into U+FFFD.
        html = r.read().decode("utf-8")
    if cache:
        cache.write_text(html, encoding="utf-8")
        print(f"wrote cache: {cache} ({len(html)} bytes)")
    return html


def _clean_wiki_name(name: str) -> str:
    # Normalize curly quotes; collapse CSV-escaped doubled inner quotes.
    # Examples:
    #   <curly>Castel, the Skyblaster Musketeer<curly>  -> Castel, the Skyblaster Musketeer
    #   <curly>Maxx ""C""<curly>                        -> Maxx "C"
    name = name.strip()
    # Curly -> straight (use codepoint escapes to keep this file ASCII-safe
    # under Windows' default cp1252 source decoding).
    name = (name.replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'"))
    # If the whole field is wrapped in quotes (CSV-style), strip them and
    # collapse doubled inner quotes (CSV escape for embedded quote).
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('""', '"')
    # Some entries had only the leading curly-quote stripped, leaving stray
    # `""C""` in the middle — collapse any remaining doubled-quote runs.
    name = name.replace('""', '"')
    return name.strip()


def parse_wiki_names(html: str) -> dict[int, str]:
    """Extract name,id pairs from the wiki body.

    GitHub renders the wiki as `Name<br>` separated lines inside <p> blocks.
    We strip tags then regex per-line. Trailing comma+digits anchors the id.
    """
    # Convert <br> (with any attrs/whitespace) to a newline FIRST so it
    # remains a row separator after tag stripping.
    text = re.sub(r"<br\s*/?\s*>", "\n", html, flags=re.IGNORECASE)
    # Then strip all other tags inline (no newline) — inline tags like
    # <span>PSY</span> appear *inside* names and must not split the row.
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities AFTER tag-stripping.
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    names: dict[int, str] = {}
    for m in _NAME_ID_RE.finditer(text):
        name = _clean_wiki_name(m.group(1))
        idx = int(m.group(2))
        if not name or len(name) < 2:
            continue
        # Skip obvious junk.
        if name in names.values() and idx in names:
            continue
        # Wiki ids range ~3000-15000 (matches CardProps array index).
        if 1 <= idx <= 16000:
            # Last write wins; entries are unique-by-id in practice.
            names[idx] = name
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    # Defaults colocate intermediate artifacts next to this script.
    _here = Path(__file__).resolve().parent
    ap.add_argument("--out", default=str(_here / "card_props_raw.json"))
    ap.add_argument("--no-wiki", action="store_true",
                    help="skip wiki name scrape")
    ap.add_argument("--wiki-cache", type=Path,
                    default=_here / "wiki_cardid.html",
                    help="path to cache wiki html (read if exists, else fetch+write)")
    args = ap.parse_args()

    pm = attach()
    records = read_card_props_array(pm)
    populated = sum(1 for r in records if not is_empty_record(r))
    print(f"populated records: {populated} / {NUM_RECORDS}")

    names: dict[int, str] = {}
    if not args.no_wiki:
        try:
            html = fetch_wiki_html(args.wiki_cache)
            names = parse_wiki_names(html)
            print(f"scraped {len(names)} name->id pairs from wiki")
        except Exception as e:
            print(f"WARN: wiki scrape failed: {e}", file=sys.stderr)

    # Merge.
    matched = 0
    for rec in records:
        nm = names.get(rec["index"])
        if nm:
            rec["name"] = nm
            matched += 1
        else:
            rec["name"] = None
    print(f"name-matched: {matched} records")

    out_path = Path(args.out)
    payload = {
        "source_address": f"0x{CARD_PROPS_BIN_ADDRESS:X}",
        "max_card_id": MAX_CARD_ID_LE2,
        "record_size": CARD_PROPS_RECORD_SIZE,
        "populated_count": populated,
        "name_matched_count": matched,
        "records": records,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
