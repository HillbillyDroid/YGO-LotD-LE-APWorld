"""Interactive TUI to build pack_archetype_map.json from extracted data.

Sources cross-referenced:
- old_archetypes.json     (enum-name -> [card_index]) -- from old_archetype_extractor.py
- ap_world/data/card_list.py::BY_INDEX  (card_index -> {name, tags})
- pack_to_cards.json      (pack_short_name -> [card_name])  -- from clean_card_to_pack.py

For each populated archetype:
  1. Walk its card indices, resolve to card names via BY_INDEX.
  2. Tally those card names against each pack's card list.
  3. Pick the dominant pack (highest count, must clear MIN_HITS and DOMINANCE).
  4. Auto-suggest a display name by finding the longest substring shared by
     >= 50% of the archetype's card names. Fall back to enum name.
  5. Show TUI: enum name, suggested display name, dominant pack + count, top
     cards. User can [a]ccept / [s]kip / [r]ename / [p]ick-different-pack /
     [d]rop / [q]uit-and-save.

Output: pack_archetype_map.json with the curated structure
{
  "packs": [
    {"name": "<Pack Name with Pack suffix>", "type": "shop"|"dlc", "id": int,
     "archetypes": [{"enum": str, "display": str}, ...]}
  ]
}

Resumable: re-running reads existing pack_archetype_map.json and skips any
(enum, pack) pair already present. To re-curate an archetype, delete its entry
from the JSON first.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
OLD_ARCH = HERE / "old_archetypes.json"
PACK_TO_CARDS = HERE / "pack_to_cards.json"
OUT_PATH = HERE / "pack_archetype_map.json"

# Load card_list as a standalone module (skip ap_world __init__ which pulls in
# Archipelago's `worlds` package).
import importlib.util as _ilu  # noqa: E402

_CARD_LIST_PATH = REPO / "ap_world" / "data" / "card_list.py"
_spec = _ilu.spec_from_file_location("card_list", _CARD_LIST_PATH)
_card_list = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_card_list)
BY_INDEX: dict[int, dict] = _card_list.BY_INDEX

# pack_to_cards.json uses short character names ("Leo/Luna"), but the AP
# canonical form (PACK_ITEM_TO_BIT keys in ap_world/items.py) uses
# "<Display Name> Pack". This map covers every pack that needs special handling
# beyond "<short> + ' Pack'". DLC entries also need a type tag.
PACK_META: list[dict] = [
    {"short": "Grandpa Muto",     "name": "Grandpa Muto Pack",     "type": "shop", "id": 1},
    {"short": "Mai Valentine",    "name": "Mai Valentine Pack",    "type": "shop", "id": 2},
    {"short": "Bakura",           "name": "Bakura Pack",           "type": "shop", "id": 3},
    {"short": "Joey Wheeler",     "name": "Joey Wheeler Pack",     "type": "shop", "id": 4},
    {"short": "Seto Kaiba",       "name": "Seto Kaiba Pack",       "type": "shop", "id": 5},
    {"short": "Yugi",             "name": "Yugi Pack",             "type": "shop", "id": 6},
    {"short": "Alexis Rhodes",    "name": "Alexis Rhodes Pack",    "type": "shop", "id": 7},
    {"short": "Bastion",          "name": "Bastion Misawa Pack",   "type": "shop", "id": 8},
    {"short": "Chazz Princeton",  "name": "Chazz Princeton Pack",  "type": "shop", "id": 9},
    {"short": "Syrus Truesdale",  "name": "Syrus Truesdale Pack",  "type": "shop", "id": 10},
    {"short": "Jesse Anderson",   "name": "Jesse Anderson Pack",   "type": "shop", "id": 11},
    {"short": "Jaden Yuki",       "name": "Jaden Yuki Pack",       "type": "shop", "id": 12},
    {"short": "Tetsu Trudge",     "name": "Tetsu Trudge Pack",     "type": "shop", "id": 13},
    {"short": "Leo/Luna",         "name": "Leo & Luna Pack",       "type": "shop", "id": 14},
    {"short": "Akiza Izinski",    "name": "Akiza Izinski Pack",    "type": "shop", "id": 15},
    {"short": "Jack Atlas",       "name": "Jack Atlas Pack",       "type": "shop", "id": 16},
    {"short": "Crow",             "name": "Crow Pack",             "type": "shop", "id": 17},
    {"short": "Yusei Fudo",       "name": "Yusei Fudo Pack",       "type": "shop", "id": 18},
    {"short": "Cathy Katherine",  "name": "Cathy Katherine Pack",  "type": "shop", "id": 19},
    {"short": "Quinton",          "name": "Quinton Pack",          "type": "shop", "id": 20},
    {"short": "Kite Tenjo",       "name": "Kite Tenjo Pack",       "type": "shop", "id": 21},
    {"short": "Shark",            "name": "Shark Pack",            "type": "shop", "id": 22},
    {"short": "Yuma Tsukumo",     "name": "Yuma Tsukumo Pack",     "type": "shop", "id": 23},
    {"short": "Gong Strong",      "name": "Gong Strong Pack",      "type": "shop", "id": 25},
    {"short": "Zuzu Boyle",       "name": "Zuzu Boyle Pack",       "type": "shop", "id": 26},
    {"short": "Shay",             "name": "Shay Pack",             "type": "shop", "id": 27},
    {"short": "Declan Akaba",     "name": "Declan Akaba Pack",     "type": "shop", "id": 28},
    {"short": "Yuya Sakaki",      "name": "Yuya Sakaki Pack",      "type": "shop", "id": 29},
    {"short": "Playmaker",        "name": "Playmaker Pack",        "type": "shop", "id": 30},
    {"short": "Blue Angel",       "name": "Blue Angel Pack",       "type": "dlc",  "id": 2},
    {"short": "Soulburner",       "name": "Soulburner Pack",       "type": "dlc",  "id": 3},
    {"short": "Varis",            "name": "Varis Pack",            "type": "dlc",  "id": 4},
    {"short": "Ai",               "name": "Ai Pack",               "type": "dlc",  "id": 5},
]

MIN_HITS = 2         # archetype must place >= this many cards in winning pack
DOMINANCE = 0.5      # winning pack must hold >= this fraction of placeable cards
SOLE_PACK_MIN_HITS = 1  # if all placed cards are in one pack, accept with >= this many
TOP_PACK_DISPLAY = 4 # how many runner-up packs to show in the TUI
TOP_CARD_DISPLAY = 6 # how many sample card names to show

# Skip these enum entries — placeholders/sentinels with no real archetype.
SKIP_ENUMS = {
    "Null", "Unknown",
}


def load_existing_map() -> dict:
    """Return the existing pack_archetype_map.json shape, or a fresh skeleton.

    Skeleton uses the new schema (archetypes is a list of {enum, display} dicts)
    so the curator can append without losing prior choices on resume.
    """
    if not OUT_PATH.exists():
        return {
            "packs": [
                {**meta, "archetypes": []}
                for meta in PACK_META
            ],
        }
    raw = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    # Migrate legacy shape: archetypes was list[str] -> list[{enum, display}]
    by_short = {m["short"]: m for m in PACK_META}
    by_name = {m["name"]: m for m in PACK_META}
    out_packs = []
    for entry in raw.get("packs", []):
        name = entry.get("name")
        meta = by_name.get(name)
        if meta is None:
            # Unknown pack name — pass through unchanged.
            out_packs.append(entry)
            continue
        archs = entry.get("archetypes", []) or []
        normalized = []
        for a in archs:
            if isinstance(a, str):
                normalized.append({"enum": a, "display": a})
            elif isinstance(a, dict) and "enum" in a:
                normalized.append({"enum": a["enum"], "display": a.get("display", a["enum"])})
        out_packs.append({**meta, "archetypes": normalized})
    # Add any pack from PACK_META that's missing in the file.
    seen = {p["name"] for p in out_packs}
    for meta in PACK_META:
        if meta["name"] not in seen:
            out_packs.append({**meta, "archetypes": []})
    # Sort to match PACK_META order.
    order = {m["name"]: i for i, m in enumerate(PACK_META)}
    out_packs.sort(key=lambda p: order.get(p["name"], 999))
    return {"packs": out_packs}


def already_curated_enums(state: dict) -> set[str]:
    out = set()
    for pack in state["packs"]:
        for a in pack.get("archetypes", []):
            out.add(a["enum"])
    return out


def save_state(state: dict) -> None:
    OUT_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalize_for_match(s: str) -> str:
    return s.lower().replace("-", "").replace("'", "").replace("’", "")


def build_pack_card_index() -> dict[str, set[str]]:
    """{pack_short_name: {normalized_card_name, ...}}."""
    raw = json.loads(PACK_TO_CARDS.read_text(encoding="utf-8"))
    return {pack: {normalize_for_match(n) for n in cards} for pack, cards in raw.items()}


def tally_archetype(card_indices: list[int],
                    pack_card_index: dict[str, set[str]],
                    ) -> tuple[Counter, list[str], list[str]]:
    """Return (pack_hits, hit_card_names, missing_card_names).

    pack_hits is a Counter {pack_short_name: # of archetype cards in that pack}.
    hit/missing track card *names* (not indices) so the TUI can show samples.
    """
    pack_hits: Counter = Counter()
    hit_names: list[str] = []
    missing_names: list[str] = []
    for cid in card_indices:
        card = BY_INDEX.get(cid)
        if card is None:
            continue
        cname = card["name"]
        norm = normalize_for_match(cname)
        placed = False
        for pack, names in pack_card_index.items():
            if norm in names:
                pack_hits[pack] += 1
                placed = True
                break  # each card lives in exactly one pack
        if placed:
            hit_names.append(cname)
        else:
            missing_names.append(cname)
    return pack_hits, hit_names, missing_names


def suggest_display_name(enum_name: str, card_names: list[str]) -> str:
    """Find the longest substring present in >= 50% of card names.

    Searches words AND multi-word phrases inside each card name. Falls back to
    the enum name if no good substring emerges (short archetypes, fancy names).
    """
    if len(card_names) < 2:
        return enum_name
    threshold = max(2, len(card_names) // 2)
    # Build candidate phrases: every contiguous 1..3-word slice of every name.
    candidates: Counter = Counter()
    for name in card_names:
        words = name.split()
        for span in (1, 2, 3):
            for i in range(len(words) - span + 1):
                phrase = " ".join(words[i:i + span])
                # Filter out tiny / common noise.
                if len(phrase) < 4:
                    continue
                if phrase.lower() in {"the", "of the", "of", "and"}:
                    continue
                candidates[phrase] += 1
    # Pick the longest phrase whose count clears the threshold.
    best = enum_name
    best_score = (0, 0)  # (length, count)
    for phrase, count in candidates.items():
        if count < threshold:
            continue
        score = (len(phrase), count)
        if score > best_score:
            best_score = score
            best = phrase
    return best


def render_archetype(enum_name: str, display: str, pack_hits: Counter,
                     hit_names: list[str], missing_names: list[str],
                     total: int) -> None:
    print()
    print("=" * 70)
    print(f"Archetype enum:  {enum_name}")
    print(f"Suggested name:  {display}")
    print(f"Cards (total):   {total}  placed={sum(pack_hits.values())}  missing={len(missing_names)}")
    if pack_hits:
        print("Top packs:")
        for pack, count in pack_hits.most_common(TOP_PACK_DISPLAY):
            bar = "#" * min(20, count)
            print(f"  {pack:20s} {count:3d}  {bar}")
    else:
        print("  (no cards mapped to any pack)")
    if hit_names:
        sample = hit_names[:TOP_CARD_DISPLAY]
        print("Sample placed cards:")
        for n in sample:
            print(f"  - {n}")
    if missing_names and len(missing_names) <= 5:
        print(f"Missing from pack data: {', '.join(missing_names)}")


def pick_pack_prompt() -> str | None:
    """List PACK_META and let the user pick one by index. None to cancel."""
    print()
    print("Pick a pack:")
    for i, meta in enumerate(PACK_META):
        print(f"  [{i:2d}] {meta['name']}")
    raw = input("Pack number (blank to cancel): ").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        print("not a number")
        return None
    if not (0 <= idx < len(PACK_META)):
        print("out of range")
        return None
    return PACK_META[idx]["name"]


def add_to_pack(state: dict, pack_name: str, enum_name: str, display: str) -> None:
    for pack in state["packs"]:
        if pack["name"] != pack_name:
            continue
        # Replace any existing entry with the same enum.
        pack["archetypes"] = [a for a in pack["archetypes"] if a["enum"] != enum_name]
        pack["archetypes"].append({"enum": enum_name, "display": display})
        return
    print(f"WARN: pack {pack_name!r} not in PACK_META; not saved")


def main() -> int:
    if not OLD_ARCH.exists():
        print(f"missing {OLD_ARCH} -- run old_archetype_extractor.py first", file=sys.stderr)
        return 1
    if not PACK_TO_CARDS.exists():
        print(f"missing {PACK_TO_CARDS} -- run clean_card_to_pack.py first", file=sys.stderr)
        return 1

    archs = json.loads(OLD_ARCH.read_text(encoding="utf-8"))["archetypes"]
    pack_card_index = build_pack_card_index()
    state = load_existing_map()
    done = already_curated_enums(state)

    by_short = {m["short"]: m["name"] for m in PACK_META}

    # Order: largest archetypes first (most signal, least ambiguity).
    ordered = sorted(
        ((name, ids) for name, ids in archs.items() if ids and name not in SKIP_ENUMS),
        key=lambda kv: -len(kv[1]),
    )

    for enum_name, card_indices in ordered:
        if enum_name in done:
            continue
        if enum_name.startswith("Empty"):
            continue

        pack_hits, hit_names, missing_names = tally_archetype(
            card_indices, pack_card_index,
        )
        suggested = suggest_display_name(enum_name, hit_names or [
            BY_INDEX[i]["name"] for i in card_indices if i in BY_INDEX
        ])

        # Auto-decide if dominance is unambiguous.
        winning_pack_short = None
        if pack_hits:
            top_pack, top_count = pack_hits.most_common(1)[0]
            placed = sum(pack_hits.values())
            sole_pack = len(pack_hits) == 1
            if sole_pack and top_count >= SOLE_PACK_MIN_HITS:
                winning_pack_short = top_pack
            elif top_count >= MIN_HITS and (top_count / max(placed, 1)) >= DOMINANCE:
                winning_pack_short = top_pack

        suggested_pack = by_short.get(winning_pack_short) if winning_pack_short else None
        render_archetype(enum_name, suggested, pack_hits, hit_names, missing_names,
                         len(card_indices))
        if suggested_pack:
            print(f"Suggested pack:  {suggested_pack}")
        else:
            print("Suggested pack:  (no clear winner -- pick or skip)")

        while True:
            prompt = "[a]ccept  [r]ename  [p]ick pack  [s]kip  [d]rop  [q]uit&save: "
            choice = input(prompt).strip().lower()
            if choice == "a":
                if not suggested_pack:
                    print("no suggested pack to accept; use [p]")
                    continue
                add_to_pack(state, suggested_pack, enum_name, suggested)
                save_state(state)
                print(f"  -> {suggested_pack}: {suggested}")
                break
            if choice == "r":
                new = input(f"new display name (blank = keep {suggested!r}): ").strip()
                if new:
                    suggested = new
                print(f"  display = {suggested!r}")
                continue
            if choice == "p":
                picked = pick_pack_prompt()
                if picked is None:
                    continue
                suggested_pack = picked
                print(f"  pack    = {suggested_pack!r}")
                continue
            if choice == "s":
                print("  skipped (will reappear on next run)")
                break
            if choice == "d":
                # Mark as done by adding to a hidden bucket so we don't see it
                # again. Use the special pack name "__dropped__" stored in state.
                state.setdefault("dropped", []).append(enum_name)
                save_state(state)
                print("  dropped (won't reappear)")
                break
            if choice == "q":
                save_state(state)
                print("saved; bye")
                return 0

    save_state(state)
    print("All archetypes processed. Saved.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\ninterrupted; partial state saved on last [a]/[d].")
        sys.exit(130)
