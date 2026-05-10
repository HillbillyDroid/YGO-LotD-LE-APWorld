"""Parse debug_tools/card_to_pack_map.json and emit cleaned views.

Input: a single-line JSON array of {"Name": str, "Location": str} objects.
"Location" values have a trailing space ("Tetsu Trudge ") — stripped here.

Outputs (next to the input):
- card_to_pack.clean.json: pretty-printed [{name, pack}] sorted by name.
- pack_to_cards.json: {pack_name: [card_name, ...]} sorted by pack then card.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "card_to_pack_map.json"
OUT_FLAT = HERE / "card_to_pack.clean.json"
OUT_BY_PACK = HERE / "pack_to_cards.json"


def main() -> int:
    if not SRC.exists():
        print(f"missing: {SRC}", file=sys.stderr)
        return 1

    raw = SRC.read_text(encoding="utf-8")
    # Source is double-escaped: card names like "A" Cell ... were serialized as
    # \"A\" then re-escaped into \\\"A\\\". Strip one layer of backslashes so
    # the result is valid JSON. Apostrophes were also escaped to \' (invalid
    # JSON), so drop those backslashes too.
    fixed = raw.replace("\\\\", "\\").replace("\\'", "'")
    # Non-ASCII chars were Python-repr'd as \xNN (invalid in JSON). Convert to
    # the actual character.
    fixed = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), fixed)
    try:
        data = json.loads(fixed)
    except json.JSONDecodeError as e:
        print(f"json parse failed after unescape: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print(f"expected top-level list, got {type(data).__name__}", file=sys.stderr)
        return 1

    flat: list[dict[str, str]] = []
    by_pack: dict[str, list[str]] = defaultdict(list)
    skipped = 0

    for entry in data:
        name = entry.get("Name")
        loc = entry.get("Location")
        if not isinstance(name, str) or not isinstance(loc, str):
            skipped += 1
            continue
        name = name.strip()
        pack = loc.strip()
        if not name or not pack:
            skipped += 1
            continue
        flat.append({"name": name, "pack": pack})
        by_pack[pack].append(name)

    flat.sort(key=lambda r: r["name"].lower())
    by_pack_sorted = {
        pack: sorted(set(cards), key=str.lower)
        for pack, cards in sorted(by_pack.items())
    }

    OUT_FLAT.write_text(
        json.dumps(flat, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    OUT_BY_PACK.write_text(
        json.dumps(by_pack_sorted, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"parsed {len(data)} rows, kept {len(flat)}, skipped {skipped}")
    print(f"packs: {len(by_pack_sorted)}")
    for pack, cards in by_pack_sorted.items():
        print(f"  {pack}: {len(cards)}")
    print(f"wrote {OUT_FLAT.name}, {OUT_BY_PACK.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
