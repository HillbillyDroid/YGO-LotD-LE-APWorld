"""Offline: read card_props_raw.json, emit card_list.py.

Tags per card (flat list of lowercase strings):
  - card category: "monster" | "spell" | "trap" | "token"
  - attribute (monsters): "dark" | "light" | "earth" | ...
  - race (monsters): "dragon" | "warrior" | ...
  - subtype flags from CardType: "effect", "fusion", "ritual", "synchro",
    "xyz", "pendulum", "link", "tuner", "toon", "spirit", "union", "gemini",
    "flip", "special_summon", "normal" (for vanilla normal monsters)
  - spell/trap subtype: "quick_play", "continuous", "field", "equip",
    "ritual", "counter", "normal" (for plain normals)
  - level/rank/link rating: "level-N" / "rank-N" / "link-N"
  - archetype: canonical display name (e.g. "Dark Magician", "Blue-Eyes")
    sourced from old_archetypes.json (enum -> card indices) cross-referenced
    with pack_archetype_map.json (enum -> display name). Multiple enums that
    share a display name are unioned, so e.g. RedDemon + Resonator both emit
    "Red Dragon Archfiend".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Map raw CardType ids -> set of subtype tags.
# (Mirrors CardTypeFlags decomposition from CardManager.cs.)
CARD_TYPE_TAGS: dict[int, list[str]] = {
    0: ["normal"],                                       # Default / vanilla normal monster
    1: ["effect"],                                       # Effect
    2: ["fusion"],                                       # Fusion (vanilla)
    3: ["fusion", "effect"],                             # FusionEffect
    4: ["ritual"],                                       # Ritual (vanilla)
    5: ["ritual", "effect"],                             # RitualEffect
    6: ["toon", "effect"],                               # ToonEffect
    7: ["spirit", "effect"],                             # SpiritEffect
    8: ["union", "effect"],                              # UnionEffect
    9: ["gemini", "effect"],                             # GeminiEffect
    10: ["token"],                                       # Token
    13: ["spell"],                                       # Spell
    14: ["trap"],                                        # Trap
    15: ["tuner"],                                       # Tuner (vanilla)
    16: ["tuner", "effect"],                             # TunerEffect
    17: ["synchro"],                                     # Synchro (vanilla)
    18: ["synchro", "effect"],                           # SynchroEffect
    19: ["synchro", "tuner", "effect"],                  # SynchroTunerEffect
    20: ["dark_tuner", "effect"],                        # DarkTunerEffect
    21: ["dark_synchro", "effect"],                      # DarkSynchroEffect
    22: ["xyz"],                                         # Xyz (vanilla)
    23: ["xyz", "effect"],                               # XyzEffect
    24: ["flip", "effect"],                              # FlipEffect
    25: ["pendulum"],                                    # Pendulum (vanilla)
    26: ["pendulum", "effect"],                          # PendulumEffect
    27: ["effect", "special_summon"],                    # EffectSp
    28: ["toon", "effect", "special_summon"],            # ToonEffectSp
    29: ["spirit", "effect", "special_summon"],          # SpiritEffectSp
    30: ["tuner", "effect", "special_summon"],           # TunerEffectSp
    31: ["dark_tuner", "effect", "special_summon"],      # DarkTunerEffectSp
    32: ["flip", "tuner", "effect"],                     # FlipTunerEffect
    33: ["pendulum", "tuner", "effect"],                 # PendulumTunerEffect
    34: ["xyz", "pendulum", "effect"],                   # XyzPendulumEffect
    35: ["pendulum", "flip", "effect"],                  # PendulumFlipEffect
    43: ["link", "effect"],                              # Link
}

# CardType ids that are spells (13) or traps (14) — for spell_type tag emission.
SPELL_TRAP_TYPES = {13, 14}

# CardType ids whose primary category is monster.
MONSTER_TYPES = (set(CARD_TYPE_TAGS) - SPELL_TRAP_TYPES) - {10}  # exclude tokens too

# CardType ids that are extra-deck monsters (use rank/link, not level).
XYZ_TYPES = {22, 23, 34}
LINK_TYPES = {43}


# Path to the archetype data sources (relative to this script). Resolved at
# runtime in load_archetype_index().
OLD_ARCHETYPES_JSON = "old_archetypes.json"
PACK_ARCHETYPE_MAP_JSON = "pack_archetype_map.json"




# --- Staples ---
# Exact-name (case-insensitive) staple lists. A card may legitimately appear in
# multiple buckets in real YGO discourse (Ash = handtrap AND negation), but we
# pick the *primary* bucket to keep tag noise low.
#
# Names that don't exist in the LOTD-LE cardpool (post-2018 cards, banned
# cards never added) simply won't match — `build_card_list.py` will print
# unmatched staple names at the end so we can fix typos.

STAPLE_HANDTRAP = [
    "Ash Blossom & Joyous Spring",
    "D.D. Crow",
    "Effect Veiler",
    "Ghost Ogre & Snow Rabbit",
    "Ghost Belle & Haunted Mansion",
    "Infinite Impermanence",
    "Nibiru, the Primal Being",
    "PSY-Framegear Delta",
    "PSY-Framegear Gamma",
    "Retaliating \"C\"",
    "Skull Meister",
    "Droll & Lock Bird",
    "Maxx \"C\"",
    "Gorz the Emissary of Darkness",
    "Battle Fader",
    "Tragoedia"
]

STAPLE_NEGATION = [
    # Spells/traps that negate or interrupt (non-handtrap).
    "Called by the Grave",
    "Dark Ruler No More",
    "Forbidden Chalice",
    "Lost Wind",
    "Solemn Judgment",
    "Solemn Strike",
    "Solemn Warning",
]

STAPLE_FLOODGATE = [
    # One-turn floodgates.
    "Artifact Lancea",
    "Different Dimension Ground",
    "Dimensional Barrier",
    "Dimension Shifter",
    "Mistaken Arrest",
    "Cold Wave",
    # Continuous floodgates.
    "Anti-Spell Fragrance",
    "Deck Lockdown",
    "Denko Sekka",
    "Grave of the Super Ancient Organism",
    "Gozen Match",
    "Inspector Boarder",
    "Necrovalley",
    "Rivalry of Warlords",
    "Secret Village of the Spellcasters",
    "Skill Drain",
    "There Can Be Only One",
    "Vanity's Fiend",
    "Vanity's Emptiness",
    "Imperial Order",
    "Macro Cosmos",
]

STAPLE_REMOVAL = [
    # Monster removal.
    "The Winged Dragon of Ra - Sphere Mode",
    "Gameciel, the Sea Turtle Kaiju",
    "Gadarla, the Mystery Dust Kaiju",
    "Jizukiru, the Star Destroying Kaiju",
    "Radian, the Multidimensional Kaiju",
    "Lava Golem",
    "Santa Claws",
    "Enemy Controller",
    "Super Polymerization",
    "Change of Heart",
    "Dark Hole",
    "Mind Control",
    "Raigeki",
    "Snatch Steal",
    "Herald of the Abyss",
    "Crackdown",
    "Compulsory Evacuation Device",
    "Karma Cut",
    "Phoenix Wing Wind Blast",
    "Breakthrough Skill",
    "Torrential Tribute",
    "Mirror Force",
    "Drowning Mirror Force",
    "Trap Hole",
    "Bottomless Trap Hole",
    "Man-Eater Bug",
    "Ring of Destruction",
    "Black Luster Soldier - Envoy of the Beginning",
    "Tribe-Infecting Virus",
    "The Monarchs Stormforth",
    # Spell/Trap removal.
    "Harpie's Feather Duster",
    "Twin Twisters",
    "Cosmic Cyclone",
    "Galaxy Cyclone",
    "Eradicator Epidemic Virus",
    "Mystical Space Typhoon",
    "Heavy Storm",
    "Giant Trunade",
    # Mixed removal.
    "Dinowrestler Pankratops",
    "Evenly Matched",
    "Lightning Storm",
    "Ryko, Lightsworn Hunter",
    "Chaos Emperor Dragon - Envoy of the End",
    "Dark Armed Dragon",
]

STAPLE_SEARCH = [
    # Draw / dig / search.
    "Allure of Darkness",
    "Card Destruction",
    "Card of Demise",
    "Fantastical Dragon Phantazmay",
    "Graceful Charity",
    "Pot of Desires",
    "Pot of Duality",
    "Pot of Extravagance",
    "Pot of Greed",
    "Trade-In",
    "Upstart Goblin",
    "Foolish Burial",
    "Foolish Burial Goods",
    "Reinforcement of the Army",
    "Terraforming",
    "Witch of the Black Forest",
    "Magician of Faith",
    "Sangan",
]

STAPLE_RECOVERY = [
    # Protection / recursion / utility (non-removal, non-negation).
    "Monster Reborn",
    "Premature Burial",
    "Call of the Haunted",
    "Book of Moon",
    "Book of Eclipse",
    "Forbidden Lance",
    "One for One",
    "Soul Release",
    "Foolish Return",
    "Instant Fusion",
    "Future Fusion",
    "Lullaby of Obedience",
    "Set Rotation",
    "Where Arf Thou?",
    "Trap Trick",
    "Fairy Tail - Snow",
    "Scapegoat",
    "Metamorphosis",
    "Delinquent Duo",
    "Dandylion",
]

STAPLE_EXTRA_DECK = [
    # Link 1.
    "Gravity Controller",
    "Linguriboh",
    "Link Spider",
    "Relinquished Anima",
    "Salamangreat Almiraj",
    "Secure Gardna",
    # Link 2.
    "Cross-Sheep",
    "Hiita the Fire Charmer, Ablaze",
    "I:P Masquerena",
    "Knightmare Cerberus",
    "Knightmare Phoenix",
    "Salamangreat Sunlight Wolf",
    # Link 3.
    "Black Luster Soldier - Soldier of Chaos",
    "Knightmare Unicorn",
    "Topologic Trisbaena",
    "Unchained Soul of Anguish",
    # Link 4.
    "Amphibious Swarmship Amblowhale",
    "Borrelsword Dragon",
    "Borreload Dragon",
    "Knightmare Gryphon",
    "Mekk-Knight Crusadia Avramax",
    "Salamangreat Heatleo",  # LOTD-LE-era boss; Raging Phoenix is post-2018
    "Saryuja Skull Dread",
    "Topologic Bomber Dragon",
    "Topologic Zeroboros",
    "Unchained Abomination",
    # Xyz flexible / generic.
    "Downerd Magician",
    "Number F0: Utopic Future",
    # Rank 1.
    "Kikinagashi Fucho",
    "Lyrilusc - Assembled Nightingale",
    # Rank 2.
    "Number 29: Mannequin Cat",
    "Toadally Awesome",
    # Rank 3.
    "The Phantom Knights of Break Sword",
    # Rank 4.
    "Abyss Dweller",
    "Evilswarm Exciton Knight",
    "Evilswarm Nightmare",
    "Gagaga Cowboy",
    "Gallant Granite",
    "Number 60: Dugares the Timeless",
    "Number 41: Bagooska the Terribly Tired Tapir",
    "Time Thief Redoer",
    "Tornado Dragon",
    "Castel, the Skyblaster Musketeer",
    # Rank 7.
    "Number 11: Big Eye",
    "Number 76: Harmonizer Gradielle",
    # Rank 8.
    "Coach King Giantrainer",
    "Number 38: Hope Harbinger Dragon Titanic Galaxy",
    "Number 68: Sanaphond the Sky Prison",
    "Number 90: Galaxy-Eyes Photon Lord",
    "Number 97: Draglubion",
    # Fusions (generic).
    "Elder Entity N'tss",
    "Earth Golem @Ignister",
    "Starving Venom Fusion Dragon",
    "Predaplant Dragostapelia",
    "Mudragon of the Swamp",
    "Millennium-Eyes Restrict",
    # Synchros (generic).
    "Coral Dragon",
    "F.A. Dawn Dragster",
    "Formula Synchron",
    "Herald of the Arc Light",
    "Crystron Halqifibrax",
    "Goyo Guardian",
    "Goyo Predator",
    "Naturia Beast",
]

STAPLE_OTHER = [
    # Other monsters / spells / traps that don't fit the buckets above.
    "Absolute King Back Jack",
    "Danger!? Jackalope?",
    "Danger! Nessie!",
    "Danger!? Tsuchinoko?",
    "Danger! Mothman!",
    "Danger! Bigfoot!",
    "Gizmek Orochi, the Serpentron Sky Slasher",
    "Volcanic Scattershot",
    "Morphing Jar",
    "Chicken Game",
    "Yata-Garasu",
    "Fairy Box",
    "Time Seal",
]

STAPLE_BUCKETS: dict[str, list[str]] = {
    "staple_handtrap":   STAPLE_HANDTRAP,
    "staple_negation":   STAPLE_NEGATION,
    "staple_floodgate":  STAPLE_FLOODGATE,
    "staple_removal":    STAPLE_REMOVAL,
    "staple_search":     STAPLE_SEARCH,
    "staple_recovery":   STAPLE_RECOVERY,
    "staple_extra_deck": STAPLE_EXTRA_DECK,
    "staple_other":      STAPLE_OTHER,
}


def build_staple_lookup() -> dict[str, list[str]]:
    """Lowercase-name -> list of staple tags."""
    out: dict[str, list[str]] = {}
    for tag, names in STAPLE_BUCKETS.items():
        for n in names:
            key = n.lower().strip()
            out.setdefault(key, []).append(tag)
            # Also add a variant with curly-quote and ASCII-dash normalization.
            alt = (key.replace("’", "'").replace("—", "-")
                       .replace("–", "-").replace("“", '"').replace("”", '"'))
            if alt != key:
                out.setdefault(alt, []).append(tag)
    return out


def normalize_name(name: str) -> str:
    return (name.lower().strip()
            .replace("’", "'").replace("—", "-")
            .replace("–", "-").replace("“", '"').replace("”", '"'))


def load_card_to_archetypes(here: Path) -> dict[int, list[str]]:
    """Build {card_index: [canonical_display_name, ...]} from the JSON sources.

    Walks every (pack, archetype-entry) in pack_archetype_map.json, resolves
    enum -> card-index list via old_archetypes.json, then assigns the entry's
    display name to each of those card indices. Multiple enums sharing a
    display name (e.g. RedDemon and Resonator both -> "Red Dragon Archfiend")
    are unioned automatically because the loop assigns the same string to the
    same set of indices; we dedupe per-card at the end.

    Card indices that appear in old_archetypes.json but whose enum isn't in
    pack_archetype_map.json contribute nothing — the curated map is the
    authority for which archetypes are user-visible.
    """
    arch_path = here / OLD_ARCHETYPES_JSON
    map_path = here / PACK_ARCHETYPE_MAP_JSON
    if not arch_path.is_file():
        raise FileNotFoundError(f"missing {arch_path}")
    if not map_path.is_file():
        raise FileNotFoundError(f"missing {map_path}")

    enum_to_cards: dict[str, list[int]] = (
        json.loads(arch_path.read_text(encoding="utf-8"))["archetypes"]
    )
    pack_map = json.loads(map_path.read_text(encoding="utf-8"))

    by_card: dict[int, list[str]] = {}
    seen_per_card: dict[int, set[str]] = {}
    missing_enums: set[str] = set()

    for pack in pack_map.get("packs", []):
        for entry in pack.get("archetypes", []):
            # Tolerate legacy str entries; new schema is {"enum", "display"}.
            if isinstance(entry, str):
                enum_name, display = entry, entry
            else:
                enum_name = entry["enum"]
                display = entry.get("display") or enum_name
            cards = enum_to_cards.get(enum_name)
            if cards is None:
                missing_enums.add(enum_name)
                continue
            for cid in cards:
                seen = seen_per_card.setdefault(cid, set())
                if display in seen:
                    continue
                seen.add(display)
                by_card.setdefault(cid, []).append(display)

    if missing_enums:
        # Surface any enum names in the curated map that don't exist in the
        # extracted bucket file — usually a typo or a stale entry.
        print(
            f"WARN: {len(missing_enums)} enum(s) in pack_archetype_map.json "
            f"have no entry in {OLD_ARCHETYPES_JSON}: "
            f"{sorted(missing_enums)[:10]}{'...' if len(missing_enums) > 10 else ''}"
        )
    return by_card


def derive_tags(rec: dict, card_to_archs: dict[int, list[str]],
                staples: dict[str, list[str]] | None = None) -> list[str]:
    tags: list[str] = []
    raw_ct = rec["card_type_raw"]
    sub = CARD_TYPE_TAGS.get(raw_ct, [])

    # Top-level category.
    if raw_ct == 13:
        tags.append("spell")
    elif raw_ct == 14:
        tags.append("trap")
    elif raw_ct == 10:
        tags.append("token")
    else:
        tags.append("monster")

    # Subtype flags (excluding the redundant "spell"/"trap"/"token" already in sub).
    for s in sub:
        if s in ("spell", "trap", "token"):
            continue
        tags.append(s)

    # Attribute (monsters only — for spells/traps the attribute field is 8/9 redundantly).
    if raw_ct not in SPELL_TRAP_TYPES and raw_ct != 10:
        attr = rec.get("attribute")
        if attr and attr not in ("spell", "trap"):
            tags.append(attr)

    # Race (monster type).
    if raw_ct not in SPELL_TRAP_TYPES and raw_ct != 10:
        mt = rec.get("monster_type")
        if mt and mt not in ("spell", "trap"):
            tags.append(mt)

    # Spell/trap property.
    if raw_ct in SPELL_TRAP_TYPES:
        st = rec.get("spell_type")
        if st:
            tags.append(st)

    # Level / Rank / Link rating.
    lvl = rec.get("level", 0)
    if lvl > 0:
        if raw_ct in LINK_TYPES:
            tags.append(f"link-{lvl}")
        elif raw_ct in XYZ_TYPES:
            tags.append(f"rank-{lvl}")
        elif raw_ct not in SPELL_TRAP_TYPES and raw_ct != 10:
            tags.append(f"level-{lvl}")

    # Canonical archetype display names (sourced from pack_archetype_map.json
    # crossed with old_archetypes.json — see load_card_to_archetypes).
    for arch in card_to_archs.get(rec["index"], ()):
        if arch not in tags:
            tags.append(arch)

    name = rec.get("name") or ""

    # Staples (exact-match, case/punctuation-normalized).
    if name and staples:
        for stag in staples.get(normalize_name(name), []):
            if stag not in tags:
                tags.append(stag)

    return tags


def is_empty(rec: dict) -> bool:
    return (rec["card_type_raw"] == 0 and rec["attribute_raw"] == 0
            and rec["monster_type_raw"] == 0 and rec["atk"] == 0
            and rec["def"] == 0 and rec["level"] == 0)


def emit_python(cards: list[dict], out: Path) -> None:
    lines: list[str] = []
    lines.append('"""Auto-generated by build_card_list.py from card_props_raw.json.')
    lines.append("")
    lines.append("Each entry: {'index', 'name', 'tags'}.")
    lines.append("- index: internal LOTD-LE card index (matches CardListSaveData byte index)")
    lines.append("- name: card name (from MoonlitDeath wiki Card-ID page)")
    lines.append('- tags: list[str], see build_card_list.py for taxonomy')
    lines.append('"""')
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("CARDS: list[dict] = [")
    for c in cards:
        name_repr = repr(c["name"]) if c["name"] is not None else "None"
        tags_repr = "[" + ", ".join(repr(t) for t in c["tags"]) + "]"
        lines.append(
            f'    {{"index": {c["index"]}, '
            f'"name": {name_repr}, "tags": {tags_repr}}},'
        )
    lines.append("]")
    lines.append("")
    lines.append("BY_INDEX: dict[int, dict] = {c['index']: c for c in CARDS}")
    lines.append("")
    lines.append("# Name -> first matching card dict. Many tokens and reprints share names; for AP")
    lines.append("# grant purposes the first occurrence is fine (granting one token is the same")
    lines.append("# regardless of duplicate index). For staple cards used as filler items there")
    lines.append("# are no dup-name conflicts in practice.")
    lines.append("BY_NAME: dict[str, dict] = {}")
    lines.append("for _c in CARDS:")
    lines.append("    BY_NAME.setdefault(_c['name'], _c)")
    lines.append("del _c")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    # Defaults assume the script runs from `debug_tools/` and emits to the
    # repo's `ap_world/data/card_list.py`. The intermediate `card_props_raw.json`
    # is produced by `dump_card_props.py` (also in this directory).
    _here = Path(__file__).resolve().parent
    _repo_root = _here.parent
    ap.add_argument("--in", dest="in_path",
                    default=str(_here / "card_props_raw.json"))
    ap.add_argument("--out",
                    default=str(_repo_root / "ap_world" / "data" / "card_list.py"))
    ap.add_argument("--include-empty", action="store_true",
                    help="include empty array slots (off by default)")
    ap.add_argument("--require-name", action="store_true",
                    help="skip cards with no name match (off by default)")
    args = ap.parse_args()

    raw = json.loads(Path(args.in_path).read_text(encoding="utf-8"))
    records: list[dict] = raw["records"]
    card_to_archs = load_card_to_archetypes(_here)
    staples = build_staple_lookup()

    cards: list[dict] = []
    for rec in records:
        if not args.include_empty and is_empty(rec):
            continue
        if args.require_name and not rec.get("name"):
            continue
        tags = derive_tags(rec, card_to_archs, staples)
        cards.append({
            "index": rec["index"],
            "name": rec.get("name"),
            "tags": tags,
        })

    emit_python(cards, Path(args.out))
    print(f"wrote {args.out} ({len(cards)} cards)")
    # Quick stats.
    from collections import Counter
    tag_counts = Counter(t for c in cards for t in c["tags"])
    print("top 25 tags:")
    for tag, n in tag_counts.most_common(25):
        print(f"  {tag:24s} {n}")
    no_name = sum(1 for c in cards if not c["name"])
    print(f"cards with no wiki name match: {no_name}")

    # Report unmatched staple names so misspellings / missing-from-LE cards
    # surface clearly.
    matched_names = {normalize_name(c["name"]) for c in cards if c["name"]}
    print("\nstaple bucket coverage:")
    for tag, names in STAPLE_BUCKETS.items():
        unmatched = [n for n in names if normalize_name(n) not in matched_names]
        hit = len(names) - len(unmatched)
        print(f"  {tag:18s} {hit}/{len(names)} matched")
        for n in unmatched:
            print(f"      MISS: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
