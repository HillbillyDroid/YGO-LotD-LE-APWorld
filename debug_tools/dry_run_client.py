"""Dry-run harness for Step 4: exercise the AP client's memory/applier/
sync/watcher modules against a running game without an AP server.

Designed for verification step described in PLAN.md Step 4:
  - Wait-for-save-data refuses to write before save data is resolved.
  - Apply a few sample item names through ItemApplier and confirm the
    in-game effects.
  - Re-applying does the right thing (idempotent for bits/counts; DP
    credit is gated by --credit-dp).
  - Run the duel watcher in a polling loop and report `2 -> 3` events.

Run from the repo root:

  # 1. While the game is on the title screen — confirm wait-loop refuses.
  python debug_tools/dry_run_client.py wait --timeout 5

  # 2. After loading a save — apply a sample item.
  python debug_tools/dry_run_client.py apply "Yugi Pack"
  python debug_tools/dry_run_client.py apply "The Heart of the Cards Unlock"
  python debug_tools/dry_run_client.py apply "1000 DP" --credit-dp

  # 3. Sync enforcer — re-assert every named item from a list.
  python debug_tools/dry_run_client.py sync "Yugi Pack" "The Heart of the Cards Unlock"

  # 4. Live duel watcher — prints location ids on every 2->3 transition.
  python debug_tools/dry_run_client.py watch
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow `python debug_tools/dry_run_client.py ...` from repo root by adding
# the parent dir to sys.path so `import ap_world` works without installing
# anything as a package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The client modules themselves only depend on pymem + the data/ dicts.
# The AP-world wiring (items.py, locations.py) imports BaseClasses, which is
# only available when AP is on the path. The dry-runner runs *without* AP, so
# we load `ap_world.client.*` directly via importlib (skipping the
# `ap_world/__init__.py` that pulls in `world.py -> worlds.AutoWorld`) and
# rebuild the small catalog dicts inline from the AP-free data tables.
import importlib.util  # noqa: E402


def _load(name: str, path: str):
    """Load a module by file path, registering it in sys.modules so
    @dataclass and relative imports resolve."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Pure-data modules — no AP dependency.
_duel_table = _load("duel_table", str(ROOT / "ap_world" / "data" / "duel_table.py"))
_card_list = _load("card_list", str(ROOT / "ap_world" / "data" / "card_list.py"))

# Fake `ap_world.data.duel_table` so the client modules' relative imports
# (`from ..data.duel_table import DUELS`) resolve. We construct the parent
# packages as bare module objects with __path__ set so importlib treats them
# as packages.
import types  # noqa: E402

_apw_pkg = types.ModuleType("ap_world")
_apw_pkg.__path__ = [str(ROOT / "ap_world")]
sys.modules["ap_world"] = _apw_pkg

_apw_data_pkg = types.ModuleType("ap_world.data")
_apw_data_pkg.__path__ = [str(ROOT / "ap_world" / "data")]
sys.modules["ap_world.data"] = _apw_data_pkg
sys.modules["ap_world.data.duel_table"] = _duel_table
sys.modules["ap_world.data.card_list"] = _duel_table  # placeholder, overwrite below
sys.modules["ap_world.data.card_list"] = _card_list

_apw_client_pkg = types.ModuleType("ap_world.client")
_apw_client_pkg.__path__ = [str(ROOT / "ap_world" / "client")]
sys.modules["ap_world.client"] = _apw_client_pkg

# Stub out the AP-dependent siblings so `from ..locations import ...` in
# duel_watcher resolves. The dry-runner builds LOC_ID_TO_DUEL itself below
# and patches it onto this stub before importing duel_watcher.
_apw_locations_stub = types.ModuleType("ap_world.locations")
sys.modules["ap_world.locations"] = _apw_locations_stub

# Build LOC_ID_TO_DUEL + DUEL_TO_LOCATIONS from duel_table directly,
# mirroring the id-allocation rule in ap_world/locations.py
# (BASE=0, DUEL_LOC_ID_RANGE = (20001, 20800), 2 ids per named duel).
DUEL_LOC_ID_BASE = 20001
LOC_ID_TO_DUEL: dict[int, tuple[int, int, str]] = {}
DUEL_TO_LOCATIONS: dict[tuple[int, int], dict] = {}
_loc_offset = 0
for _entry in _duel_table.DUELS:
    if _entry["name"] is None:
        continue
    _win_id = DUEL_LOC_ID_BASE + _loc_offset * 2
    _bonus_id = _win_id + 1
    LOC_ID_TO_DUEL[_win_id] = (_entry["series"], _entry["slot"], "win")
    LOC_ID_TO_DUEL[_bonus_id] = (_entry["series"], _entry["slot"], "bonus")
    DUEL_TO_LOCATIONS[(_entry["series"], _entry["slot"])] = {
        "win": _win_id,
        "bonus": _bonus_id,
        "name": _entry["name"],
    }
    _loc_offset += 1
del _loc_offset, _entry, _win_id, _bonus_id

_apw_locations_stub.LOC_ID_TO_DUEL = LOC_ID_TO_DUEL
_apw_locations_stub.DUEL_TO_LOCATIONS = DUEL_TO_LOCATIONS

# Now load the client modules. Their relative imports resolve via the
# stubs above, and they themselves only need pymem.
_memory = _load("ap_world.client.memory", str(ROOT / "ap_world" / "client" / "memory.py"))
_duel_watcher = _load(
    "ap_world.client.duel_watcher",
    str(ROOT / "ap_world" / "client" / "duel_watcher.py"),
)
_item_applier = _load(
    "ap_world.client.item_applier",
    str(ROOT / "ap_world" / "client" / "item_applier.py"),
)
_sync_enforcer = _load(
    "ap_world.client.sync_enforcer",
    str(ROOT / "ap_world" / "client" / "sync_enforcer.py"),
)

MemoryHandle = _memory.MemoryHandle
SaveDataNotReady = _memory.SaveDataNotReady
DuelWatcher = _duel_watcher.DuelWatcher
ItemApplier = _item_applier.ItemApplier
SyncEnforcer = _sync_enforcer.SyncEnforcer

# Inline-rebuild the catalog dicts that ap_world/items.py exposes. The
# dry-runner only needs the three lookup tables consumed by ItemApplier;
# we don't need the full id allocation.
SHOP_PACKS_CATALOG = [
    ("Grandpa Muto Pack", "shop", 1), ("Mai Valentine Pack", "shop", 2),
    ("Bakura Pack", "shop", 3), ("Joey Wheeler Pack", "shop", 4),
    ("Seto Kaiba Pack", "shop", 5), ("Yugi Pack", "shop", 6),
    ("Alexis Rhodes Pack", "shop", 7), ("Bastion Misawa Pack", "shop", 8),
    ("Chazz Princeton Pack", "shop", 9), ("Syrus Truesdale Pack", "shop", 10),
    ("Jesse Anderson Pack", "shop", 11), ("Jaden Yuki Pack", "shop", 12),
    ("Tetsu Trudge Pack", "shop", 13), ("Leo & Luna Pack", "shop", 14),
    ("Akiza Izinski Pack", "shop", 15), ("Jack Atlas Pack", "shop", 16),
    ("Crow Pack", "shop", 17), ("Yusei Fudo Pack", "shop", 18),
    ("Cathy Katherine Pack", "shop", 19), ("Quinton Pack", "shop", 20),
    ("Kite Tenjo Pack", "shop", 21), ("Shark Pack", "shop", 22),
    ("Yuma Tsukumo Pack", "shop", 23),
    # bit 24 (Pendulum) disabled in LE-v2 — produces no shop pack.
    ("Gong Strong Pack", "shop", 25), ("Zuzu Boyle Pack", "shop", 26),
    ("Shay Pack", "shop", 27), ("Declan Akaba Pack", "shop", 28),
    ("Yuya Sakaki Pack", "shop", 29), ("Playmaker Pack", "shop", 30),
    ("Blue Angel Pack", "dlc", 2), ("Soulburner Pack", "dlc", 3),
    ("Varis Pack", "dlc", 4), ("Ai Pack", "dlc", 5),
]
PACK_ITEM_TO_BIT: dict[str, tuple[str, int]] = {
    n: (f, b) for n, f, b in SHOP_PACKS_CATALOG
}
DP_ITEM_AMOUNTS: dict[str, int] = {
    "1000 DP": 1000, "5000 DP": 5000, "10000 DP": 10000,
}
CARD_ITEM_TO_INDEX: dict[str, int] = {}
for _card in _card_list.BY_INDEX.values():
    if not any(t.startswith("staple_") for t in _card["tags"]):
        continue
    if _card["name"] in CARD_ITEM_TO_INDEX:
        continue
    CARD_ITEM_TO_INDEX[_card["name"]] = _card["index"]
del _card


def make_handles() -> tuple[MemoryHandle, ItemApplier]:
    mem = MemoryHandle()
    if not mem.attach():
        print("could not attach to Lotd process — is the game running?")
        sys.exit(1)
    print(f"attached to {mem.process_name}")
    applier = ItemApplier(
        mem,
        pack_item_to_bit=PACK_ITEM_TO_BIT,
        card_item_to_index=CARD_ITEM_TO_INDEX,
        dp_item_amounts=DP_ITEM_AMOUNTS,
    )
    return mem, applier


def cmd_wait(args: argparse.Namespace) -> int:
    mem, _ = make_handles()
    print(f"waiting up to {args.timeout}s for save data...")
    ok = mem.wait_for_save_data(timeout_seconds=args.timeout, poll_interval=0.5)
    if not ok:
        print("save data not ready — confirms wait-loop is gating writes.")
        # Demonstrate that writes refuse before resolution.
        try:
            mem.write_unlocked_content_all()
        except SaveDataNotReady as e:
            print(f"write refused as expected: {e}")
            return 0
        print("UNEXPECTED: write did not raise SaveDataNotReady.")
        return 1
    print(f"save data resolved at 0x{mem.save_data:X}")
    return 0


def _ensure_ready(mem: MemoryHandle, timeout: float = 30.0) -> None:
    if not mem.wait_for_save_data(timeout_seconds=timeout):
        print("save data did not become ready — load a save and retry.")
        sys.exit(1)
    print(f"save data resolved at 0x{mem.save_data:X}")


def cmd_apply(args: argparse.Namespace) -> int:
    mem, applier = make_handles()
    _ensure_ready(mem)
    name = args.name
    print(f"\napplying {name!r} (credit_dp={args.credit_dp})")
    before_dp = mem.read_dp()
    before_packs = mem.read_unlocked_shop_packs()
    before_dlc = mem.read_unlocked_dlc_shop_packs()
    try:
        applier.apply(name, credit_dp=args.credit_dp)
    except KeyError:
        print(f"unknown item name {name!r}")
        return 1
    after_dp = mem.read_dp()
    after_packs = mem.read_unlocked_shop_packs()
    after_dlc = mem.read_unlocked_dlc_shop_packs()
    print(f"  DP             : {before_dp:,} -> {after_dp:,}")
    print(f"  ShopPacks      : 0x{before_packs:08X} -> 0x{after_packs:08X}")
    print(f"  DlcShopPacks   : 0x{before_dlc:08X} -> 0x{after_dlc:08X}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    mem, applier = make_handles()
    _ensure_ready(mem)
    enforcer = SyncEnforcer(mem, applier)
    print(f"\nsync-enforcing {len(args.names)} items: {args.names}")
    enforcer.tick(args.names)
    print("done.")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    mem, _ = make_handles()
    _ensure_ready(mem)
    watcher = DuelWatcher(mem)
    print(f"\npolling every {args.interval}s — Ctrl-C to stop")
    print("(first tick is silent — establishes baseline state)")
    try:
        while True:
            new_locs = watcher.tick()
            if new_locs:
                print(f"\n[{time.strftime('%H:%M:%S')}] new location ids: {new_locs}")
                for loc_id in new_locs:
                    if loc_id in LOC_ID_TO_DUEL:
                        s, slot, kind = LOC_ID_TO_DUEL[loc_id]
                        print(f"   loc {loc_id}: series={s} slot={slot} kind={kind}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Dry-run the AP client modules.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pw = sub.add_parser("wait", help="test wait-for-save-data + write-refusal")
    pw.add_argument("--timeout", type=float, default=10.0)
    pw.set_defaults(func=cmd_wait)

    pa = sub.add_parser("apply", help="apply a single item by AP name")
    pa.add_argument("name")
    pa.add_argument("--credit-dp", action="store_true",
                    help="actually credit DP for DP items (otherwise simulated as a no-op)")
    pa.set_defaults(func=cmd_apply)

    ps = sub.add_parser("sync", help="run SyncEnforcer.tick on a name list")
    ps.add_argument("names", nargs="+")
    ps.set_defaults(func=cmd_sync)

    pwt = sub.add_parser("watch", help="poll campaign duels for 2->3 transitions")
    pwt.add_argument("--interval", type=float, default=1.0)
    pwt.set_defaults(func=cmd_watch)

    args = p.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
