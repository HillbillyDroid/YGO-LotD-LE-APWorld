# Battle pack unlock field — investigation notes (paused 2026-05-02)

## Summary

The `UnlockedBattlePacks` field named in pixeltris/Lotd source (at `MiscSaveData + 2948`, file offset `0x1B5C`) is **NOT** where LE-v2 stores battle pack unlock state. Empirically it holds VRAINS shop packs — Blue Angel, Soulburner, Varis, Ai (bits 2-5). Source `Constants.NumBattlePacks = 5` is misleading for LE-v2.

The actual battle pack unlock flag location is **unknown**. Investigation paused — battle packs are sealed/draft mode, separate from the card shop, and may not be needed for the AP randomizer scope.

## Evidence

- A save with Epic Dawn battle pack unlocked reads `UnlockedBattlePacks = 0x00`. So bits 0-1 of that field are not legit battle packs either.
- Diff between `early save game.dat` and a save after unlocking one battle pack (`diff2.txt` in `C:\Users\Sam\Desktop\SS save backup\ygo legacy of the duelist\binarydiff\`) shows many byte changes — the unlock event was bundled with story progression, so the diff is noisy.

## Candidates ruled out

In-game tested by writing `0x00` to each byte and checking if the battle pack disappeared from the menu:

- `0x90BE` — no effect
- `0x90C0` — no effect
- `0x90C5` — no effect

All three of these flipped `00 → 01` in the diff but none gate the unlock. Likely related metadata (tutorial seen, "new" badge, etc.) rather than the unlock flag itself.

## Candidates still worth probing

Other `00 → 01` bool-shaped writes from `diff2.txt`:

- `0x7372`, `0x75AE`, `0x82C9`, `0x8D57`, `0x9192`, `0x9238`, `0x90E3`, `0x90E4`

Plus structured writes that might be a per-pack record:

- `0x33D8`–`0x33F0` cluster (small int writes including `0x7B = 123`)
- `0x1B6C` (`04 → 07`) — adjacent to `UnlockedContent` in MiscSaveData
- `0x1B70` (`00 → 05`) — labelled "padding" in CLAUDE.md but evidently not

## Source hint

`GameSaveData.BattlePacksOffset = 836` for LinkEvolution (vs. 380 for Lotd). That's an offset into `GameSaveData` — a different region from `MiscSaveData`. The *unlocked* flag may live near the per-pack draft state at that offset. Untested.

## How to resume

1. Try the remaining bool candidates one at a time with `--write-byte OFFSET 0x00`, re-enter battle pack menu, check.
2. If none work, scan around `saveData + 836` (the `GameSaveData.BattlePacksOffset` region).
3. If desperate, make a fresh save with zero battle packs unlocked, unlock just one in-game with no other progression, diff — should give a much cleaner signal than `diff2.txt`.
