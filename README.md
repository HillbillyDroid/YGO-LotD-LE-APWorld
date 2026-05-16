# Yu-Gi-Oh! LotD Link Evolution Archipelago Randomizer

An [Archipelago](https://archipelago.gg/) (AP) multiworld randomizer
for **Yu-Gi-Oh! Legacy of the Duelist: Link Evolution**.

The game's 175 named campaign duels are gated behind **Unlock**
items scattered across the multiworld. Winning a duel sends two
checks (Win + First Clear Bonus) which release more items. The
game's default starter decks and unlocks are suppressed — your card
collection is rebuilt entirely from AP items: character **shop pack**
unlocks, individual staple **cards**, **Duel Points** filler, plus a
client-side **Crafting** tab for spending earned DP on any card in
the LE-v2 pool.

## Project layout

The project ships as **a single distributable**: `ygo_lotd.apworld`.
Both the seed host and every player drop it into their Archipelago
install. The in-game client (a pymem-based runtime that attaches to
the game and reconciles AP state with the save data each tick) is
bundled inside the apworld and launched from the Archipelago
Launcher.

- **`ap_world/`** — Python Archipelago world. Generates the
  multiworld and contains the runtime client. Built into
  `ygo_lotd.apworld` via `python ap_world/build_apworld.py`.
  **Requires Archipelago 0.6.7 or higher.**
- **`debug_tools/`** — standalone pymem probes and offline data
  extractors used to derive the card list, archetype map, duel
  table, and pack catalog. Not shipped with the apworld.

## Getting started

Player setup, including Steam Cloud requirements, save backup,
client install, and the in-client connect flow, lives in
[ap_world/docs/setup_en.md](ap_world/docs/setup_en.md). That file is
also rendered as the official setup guide on archipelago.gg.

A high-level overview of how randomization affects the game lives
in [ap_world/docs/en_YGO LotD-LE.md](ap_world/docs/en_YGO%20LotD-LE.md)
(also rendered on archipelago.gg).

## Disclaimer

Online ranked duels and shared leaderboards with this mod installed
are **discouraged** — stick to offline / solo sessions. The client
swaps `savegame.dat` between AP worlds, so **Steam Cloud sync for
the game must be disabled** before connecting; the client refuses
to attach until it is. Back up your save manually before playing —
see [ap_world/docs/setup_en.md](ap_world/docs/setup_en.md) for the
full disclaimer.

## License

[MIT](LICENSE.md).
