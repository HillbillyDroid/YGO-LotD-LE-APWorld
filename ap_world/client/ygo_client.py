"""Headless Archipelago client for Yu-Gi-Oh! Legacy of the Duelist Link Evolution.

Reuses the four Step-4 backend modules:
  - memory.MemoryHandle       — pymem wrapper, save-data pointer chain.
  - duel_watcher.DuelWatcher  — emits Win+Bonus loc ids on Duel.State -> 3.
  - item_applier.ItemApplier  — receives item names, writes the matching
                                in-memory bit/count/state. DP gated.
  - sync_enforcer.SyncEnforcer — re-asserts every owned item every tick.

Per PLAN §Initial connect sequence:
  Stage A:  await save-data ready (no writes before pointer chain resolves).
  Stage B:  one-shot reconcile — content unlock, hide-default-cards, replay
            entire item history through applier (DP credit gated by
            `applied_dp_count` slot-storage value to avoid double-spending
            on reconnect / save-flush-desync).
  Stage C:  start ygo_loop (pump new items, run sync enforcer, run duel
            watcher, send goal status when reached).

Slot-storage keys:
  ygo_lotd_<team>_<slot>_applied_dp_count   int — # of DP items already credited.
  ygo_lotd_<team>_<slot>_starter_granted    bool — content-unlock + hide-default
                                                  cards already done this slot.
                                                  (currently only used to skip the
                                                  "log starter granted" message;
                                                  the underlying writes are
                                                  idempotent so it's safe to
                                                  re-run on reconnect.)
"""
from __future__ import annotations

import asyncio
import random
import sys
from argparse import Namespace
from enum import Enum
from typing import TYPE_CHECKING, Any

from CommonClient import (
    ClientCommandProcessor,
    CommonContext,
    logger,
    server_loop,
)
from NetUtils import ClientStatus
from Utils import gui_enabled

import pymem.exception

from ..data.card_list import BY_INDEX
from ..data.pack_archetypes import ARCHETYPE_CARDS, PACK_ARCHETYPES
from .duel_watcher import DuelWatcher
from .item_applier import ItemApplier
from .memory import MemoryHandle
from .save_manager import SaveManager, SaveManagerError
from .sync_enforcer import SyncEnforcer

if TYPE_CHECKING:
    import kvui
    from .ygo_gui import YGOLotDManager


GAME_NAME = "YGO LotD-LE"
POLL_INTERVAL_SECONDS = 1.0
SAVE_DATA_WAIT_TIMEOUT_SECONDS = 60.0
SAVE_DATA_RETRY_COOLDOWN_SECONDS = 5.0

# Errors raised by pymem when the game process has exited or its memory
# is otherwise unreadable. We treat any of these as "process gone" and
# trigger a reattach cycle rather than letting the live loop spam tracebacks.
_PROCESS_GONE_ERRORS: tuple[type[BaseException], ...] = (
    pymem.exception.MemoryReadError,
    pymem.exception.MemoryWriteError,
    pymem.exception.WinAPIError,
    pymem.exception.ProcessError,
    OSError,
)

# Starter-archetype grant: pick this many unique cards from the archetype pool
# and give the player a full playset (3 copies) of each. Tunable per playtest.
STARTER_ARCHETYPE_BUNDLE_SIZE = 8
STARTER_ARCHETYPE_COPIES = 3

# Crafting prices (DP per copy). Staples cost more to slow progression.
CRAFTING_BASE_COST = 600
CRAFTING_STAPLE_COST = 3000


def crafting_cost_for_card(card: dict) -> int:
    """Return the DP cost to craft one copy of `card`. Staple-tagged cards
    are priced higher to slow the staple-rush a v1 playtest exposed."""
    is_staple = any(t.startswith("staple_") for t in card["tags"])
    return CRAFTING_STAPLE_COST if is_staple else CRAFTING_BASE_COST


class ConnectionStatus(Enum):
    NOT_CONNECTED = 0
    CONNECTED = 1


def _slot_key(ctx: "YGOLotDContext", suffix: str) -> str:
    """Namespace slot-storage keys by team+slot — AP keys are global."""
    return f"ygo_lotd_{ctx.team}_{ctx.slot}_{suffix}"


class YGOLotDCommandProcessor(ClientCommandProcessor):
    ctx: "YGOLotDContext"

    def _cmd_attached(self) -> bool:
        """Show pymem attach + save-data resolution status."""
        ctx = self.ctx
        if ctx.memory.pm is None:
            logger.info("not attached to game process")
        else:
            logger.info(f"attached to {ctx.memory.process_name} (pid={ctx.memory.pm.process_id})")
        logger.info(f"save data ready: {ctx.save_data_ready}")
        return True


class YGOLotDContext(CommonContext):
    game = GAME_NAME
    items_handling = 0b111  # full remote: receive own + others' items
    command_processor = YGOLotDCommandProcessor

    # Connect lifecycle
    connection_status: ConnectionStatus = ConnectionStatus.NOT_CONNECTED
    slot_data: dict[str, Any]
    save_data_ready: bool = False

    # Receive / send dedup. Reset on Connected (server's checked_locations +
    # items_received are the source of truth).
    highest_processed_item_index: int = 0
    sent_location_ids: set[int]
    applied_dp_indices: set[int]
    applied_dp_count: int = 0
    starter_granted: bool = False
    crafted_card_counts: dict[int, int]

    # Starter archetype pick (one-shot per slot). Cards are stored as a
    # {card_index: count} dict with the same shape as crafted_card_counts so
    # the per-tick re-assertion path can treat them identically. See
    # ARCHETYPE_PLAN.md.
    starter_archetype: str | None = None       # picked archetype display name
    starter_archetype_card_counts: dict[int, int]  # granted cards (count always 1)
    starter_archetype_pending: bool = False    # pack detected, awaiting GUI pick
    starter_archetype_options: list[str]       # candidate display names for GUI

    # Backend modules
    memory: MemoryHandle
    watcher: DuelWatcher
    applier: ItemApplier | None = None
    enforcer: SyncEnforcer | None = None
    save_manager: SaveManager | None = None

    # Cloud-off gate. Set True between Stage A's `is_cloud_disabled()`
    # returning False and the user clicking "Verify" in the GUI modal.
    # Stage A bails when this flips True; the modal's Verify callback
    # restarts the connect sequence on success.
    cloud_off_required: bool = False
    # Set when the cloud-off override flag was passed at launch (or the
    # save-manager-side detection succeeded with override). Surfaced on
    # status line so the user can see the gate is open.
    cloud_off_satisfied: bool = False

    # Async tasks
    connect_task: asyncio.Task[None] | None = None
    loop_task: asyncio.Task[None] | None = None
    asyncio_loop: asyncio.AbstractEventLoop | None = None

    # The user-facing Kivy GUI hooks here. None until make_gui runs.
    ui_manager: "YGOLotDManager | None" = None
    status_text: str = "Not connected."

    def __init__(self, server_address: str | None = None, password: str | None = None) -> None:
        super().__init__(server_address, password)
        self.slot_data = {}
        self.sent_location_ids = set()
        self.applied_dp_indices = set()
        self.crafted_card_counts = {}
        self.starter_archetype = None
        self.starter_archetype_card_counts = {}
        self.starter_archetype_pending = False
        self.starter_archetype_options = []
        self._pending_loc_ids: set[int] = set()
        self.memory = MemoryHandle()
        self.watcher = DuelWatcher(self.memory)

    # ---- Archipelago hooks ------------------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect(game=self.game)

    def on_package(self, cmd: str, args: dict[str, Any]) -> None:
        if cmd == "RoomInfo":
            # CommonContext.process_server_cmd validates seed_name against
            # ctx.seed_name but never assigns it; we capture it here so
            # SaveManager can key per-world backups by seed_name + slot name.
            seed = args.get("seed_name")
            if seed:
                self.seed_name = str(seed)
        elif cmd == "Connected":
            self._handle_connected(args)
        elif cmd == "Retrieved":
            self._handle_retrieved(args.get("keys") or {})
        elif cmd == "SetReply":
            # Echo of our own Set or another client's update — refresh stored values.
            key = args.get("key")
            value = args.get("value")
            if key is not None:
                self.stored_data[key] = value
                self._handle_retrieved({key: value})

    def _handle_connected(self, args: dict[str, Any]) -> None:
        self.slot_data = args.get("slot_data") or {}
        # Server is authoritative on what's already been checked.
        self.sent_location_ids = set(self.checked_locations)
        self.highest_processed_item_index = 0
        self.applied_dp_indices = set()
        self.applied_dp_count = 0
        self.starter_granted = False
        self.crafted_card_counts = {}
        # Per-slot starter archetype state. Cleared on (re)connect; rehydrated
        # from slot-storage by _handle_retrieved if a prior pick exists.
        self.starter_archetype = None
        self.starter_archetype_card_counts = {}
        self.starter_archetype_pending = False
        self.starter_archetype_options = []
        self.save_data_ready = False
        self.connection_status = ConnectionStatus.CONNECTED
        self.status_text = "Connected — waiting for save data..."

        # Build the applier now that we have slot_data.
        pack_item_to_bit_raw = self.slot_data.get("pack_item_to_bit") or {}
        # slot_data round-trips tuples as lists; normalize back.
        pack_item_to_bit: dict[str, tuple[str, int]] = {
            name: (str(field_bit[0]), int(field_bit[1]))
            for name, field_bit in pack_item_to_bit_raw.items()
        }
        card_item_to_index = {
            name: int(idx)
            for name, idx in (self.slot_data.get("card_item_to_index") or {}).items()
        }
        dp_item_amounts = {
            name: int(amount)
            for name, amount in (self.slot_data.get("dp_item_amounts") or {}).items()
        }
        self.applier = ItemApplier(
            self.memory,
            pack_item_to_bit=pack_item_to_bit,
            card_item_to_index=card_item_to_index,
            dp_item_amounts=dp_item_amounts,
        )
        self.enforcer = SyncEnforcer(self.memory, self.applier)

        # Subscribe to slot-scoped persistent state.
        self.set_notify(
            _slot_key(self, "applied_dp_count"),
            _slot_key(self, "starter_granted"),
            _slot_key(self, "crafted_card_indices"),
            _slot_key(self, "starter_archetype"),
            _slot_key(self, "starter_archetype_cards"),
        )

        # Kick off connect sequence (Stages A -> B -> C).
        if self.connect_task is not None and not self.connect_task.done():
            self.connect_task.cancel()
        self.connect_task = asyncio.create_task(
            self._connect_sequence(), name="ygo connect-sequence"
        )
        if self.ui_manager is not None:
            self.ui_manager.on_connected(self.slot_data)

    def _handle_retrieved(self, keys: dict[str, Any]) -> None:
        adp_key = _slot_key(self, "applied_dp_count")
        sg_key = _slot_key(self, "starter_granted")
        if adp_key in keys and keys[adp_key] is not None:
            try:
                self.applied_dp_count = int(keys[adp_key])
            except (TypeError, ValueError):
                logger.warning(f"unexpected applied_dp_count value: {keys[adp_key]!r}")
        if sg_key in keys and keys[sg_key] is not None:
            self.starter_granted = bool(keys[sg_key])
        ci_key = _slot_key(self, "crafted_card_indices")
        if ci_key in keys and keys[ci_key] is not None:
            raw = keys[ci_key]
            try:
                if isinstance(raw, dict):
                    # New schema: {index_str: count}.
                    self.crafted_card_counts = {
                        int(k): max(1, min(3, int(v))) for k, v in raw.items()
                    }
                else:
                    # Legacy schema: list[int] — treat each entry as count=1.
                    self.crafted_card_counts = {int(i): 1 for i in raw}
            except (TypeError, ValueError):
                logger.warning(f"unexpected crafted_card_indices value: {raw!r}")
        sa_key = _slot_key(self, "starter_archetype")
        if sa_key in keys and keys[sa_key] is not None:
            self.starter_archetype = str(keys[sa_key]) or None
        sac_key = _slot_key(self, "starter_archetype_cards")
        if sac_key in keys and keys[sac_key] is not None:
            raw = keys[sac_key]
            try:
                if isinstance(raw, dict):
                    self.starter_archetype_card_counts = {
                        int(k): max(1, min(3, int(v))) for k, v in raw.items()
                    }
                else:
                    # Tolerate a bare list[int] (one copy each).
                    self.starter_archetype_card_counts = {int(i): 1 for i in raw}
            except (TypeError, ValueError):
                logger.warning(f"unexpected starter_archetype_cards value: {raw!r}")

    async def disconnect(self, *args: Any, **kwargs: Any) -> None:
        for task in (self.connect_task, self.loop_task):
            if task is not None and not task.done():
                task.cancel()
        self.connect_task = None
        self.loop_task = None
        self.connection_status = ConnectionStatus.NOT_CONNECTED
        self.save_data_ready = False
        self.status_text = "Not connected."
        self.watcher.reset()
        self._refresh_ui()
        await super().disconnect(*args, **kwargs)

    # ---- Connect sequence (Stages A / B / C) ------------------------------

    async def _connect_sequence(self) -> None:
        # Stage A: attach + wait for save data ----------------------------
        loop = asyncio.get_running_loop()

        # Stage A.0: SaveManager setup + Steam Cloud off gate.
        # Must run BEFORE memory.attach() so the cloud-off modal can block
        # progress on a fresh launch. If `save_manager` was never injected
        # by main() (e.g. some test harness), skip the gate entirely.
        if self.save_manager is not None:
            try:
                await loop.run_in_executor(None, self.save_manager.ensure_setup)
            except SaveManagerError as exc:
                self.status_text = f"Save manager setup failed: {exc}"
                logger.error(self.status_text)
                # Without setup we can't verify cloud state; bail and let the
                # user fix (e.g. install game, run it once) and reconnect.
                return
            except Exception as exc:
                logger.exception(f"save_manager.ensure_setup failed: {exc}")
                self.status_text = "Save manager setup failed (see log)."
                return

            cloud_disabled = await loop.run_in_executor(
                None, self.save_manager.is_cloud_disabled,
            )
            if not cloud_disabled:
                self.cloud_off_required = True
                self.cloud_off_satisfied = False
                self.status_text = (
                    "Steam Cloud must be disabled for LotD-LE before connecting "
                    "(see Saves tab)."
                )
                logger.warning(self.status_text)
                self._refresh_ui()
                return
            self.cloud_off_required = False
            self.cloud_off_satisfied = True

        if self.memory.pm is None:
            attached = await loop.run_in_executor(None, self.memory.attach)
            if not attached:
                self.status_text = "Game process not found — start Lotd."
                logger.warning(self.status_text)
                # Retry attach periodically rather than giving up.
                while not self.exit_event.is_set():
                    await asyncio.sleep(SAVE_DATA_RETRY_COOLDOWN_SECONDS)
                    if await loop.run_in_executor(None, self.memory.attach):
                        break

        logger.info(f"attached to {self.memory.process_name}")
        self.status_text = "Waiting for save data..."

        ready = await loop.run_in_executor(
            None,
            self.memory.wait_for_save_data,
            SAVE_DATA_WAIT_TIMEOUT_SECONDS,
            0.5,
        )
        while not ready:
            if self.exit_event.is_set():
                return
            logger.info("save data not ready — load a save in the game.")
            await asyncio.sleep(SAVE_DATA_RETRY_COOLDOWN_SECONDS)
            ready = await loop.run_in_executor(
                None,
                self.memory.wait_for_save_data,
                SAVE_DATA_WAIT_TIMEOUT_SECONDS,
                0.5,
            )
        self.save_data_ready = True
        logger.info(f"save data resolved at 0x{self.memory.save_data:X}")

        # Stage B: one-shot reconciliation --------------------------------
        try:
            await loop.run_in_executor(None, self._reconcile)
        except _PROCESS_GONE_ERRORS as e:
            # Game died between Stage A and Stage B. Drop and restart.
            self._handle_process_gone(e)
            return

        # Stage C: start the poll loop ------------------------------------
        if self.loop_task is not None and not self.loop_task.done():
            self.loop_task.cancel()
        self.loop_task = asyncio.create_task(self._ygo_loop(), name="ygo poll-loop")
        self.status_text = "Live."
        self._refresh_ui()

    def _reconcile(self) -> None:
        """Stage B body — runs in a thread (not the asyncio loop).

        Process-gone errors propagate out so the caller can drop the handle
        and restart Stage A; other exceptions on individual writes are
        logged but don't abort the rest of the reconcile."""
        try:
            self.memory.write_unlocked_content_all()
        except _PROCESS_GONE_ERRORS:
            raise
        except Exception as e:
            logger.exception(f"write_unlocked_content_all failed: {e}")

        if self.slot_data.get("hide_default_cards"):
            try:
                self.memory.hide_default_cards()
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"hide_default_cards failed: {e}")

        # Force every series's Duel[0] to State >= 1 — UI prerequisite for
        # the series tab to be clickable. (Duel[0] is the unnamed placeholder
        # slot; we use slot=0 here intentionally.)
        for series_idx in range(6):
            try:
                self.memory.raise_duel_state(series_idx, slot=0, min_state=1)
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"raise_duel_state(series={series_idx}, slot=0) failed: {e}")

        # Replay every received item idempotently. DP is *not* credited here;
        # we use the slot-storage applied_dp_count + items_received DP-tally
        # to compute a delta separately.
        owned_names = [self._lookup_item_name(it.item) for it in self.items_received]
        if self.enforcer is not None:
            self.enforcer.tick([n for n in owned_names if n])

        for idx, count in self.crafted_card_counts.items():
            try:
                self.memory.raise_card_count(idx, max(1, min(3, count)))
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"replay crafted card {idx}: {e}")

        for idx, count in self.starter_archetype_card_counts.items():
            try:
                self.memory.raise_card_count(idx, max(1, min(3, count)))
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"replay starter card {idx}: {e}")

        # Starter-archetype gate. If a pack is precollected and we haven't
        # picked yet, either auto-grant (1 candidate) or surface options to
        # the GUI (2+ candidates). Skipped on every reconnect once a pick
        # is persisted in slot-storage.
        if self.starter_archetype is None and not self.starter_archetype_pending:
            try:
                self._evaluate_starter_archetype([n for n in owned_names if n])
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"_evaluate_starter_archetype failed: {e}")

        # DP reconcile. Apply (received_dp_count - applied_dp_count) items;
        # then persist the new applied_dp_count back to slot storage.
        dp_amounts = self.applier.dp_item_amounts if self.applier else {}
        received_dp_items = [n for n in owned_names if n in dp_amounts]
        delta = len(received_dp_items) - self.applied_dp_count
        if delta > 0:
            for name in received_dp_items[-delta:]:
                self.memory.add_dp(int(dp_amounts[name]))
            self.applied_dp_count = len(received_dp_items)
            self._persist_int(_slot_key(self, "applied_dp_count"), self.applied_dp_count)
        elif delta < 0:
            # Server has fewer DP items than we've applied — should never
            # happen unless the seed was rerolled. Don't subtract; just align.
            self.applied_dp_count = len(received_dp_items)
            self._persist_int(_slot_key(self, "applied_dp_count"), self.applied_dp_count)

        # Mark every applied item index so the live loop doesn't credit DP again.
        for idx, name in enumerate(owned_names):
            if name in dp_amounts:
                self.applied_dp_indices.add(idx)

        self.highest_processed_item_index = len(self.items_received)

        if not self.starter_granted:
            self.starter_granted = True
            self._persist_bool(_slot_key(self, "starter_granted"), True)

    def _persist_int(self, key: str, value: int) -> None:
        # CommonContext loops are async, but we may be called from a thread.
        loop = self.asyncio_loop
        if loop is None or self.exit_event.is_set():
            return
        asyncio.run_coroutine_threadsafe(
            self.send_msgs(
                [{"cmd": "Set", "key": key, "default": 0,
                  "operations": [{"operation": "replace", "value": int(value)}]}]
            ),
            loop,
        )

    def _persist_bool(self, key: str, value: bool) -> None:
        loop = self.asyncio_loop
        if loop is None or self.exit_event.is_set():
            return
        asyncio.run_coroutine_threadsafe(
            self.send_msgs(
                [{"cmd": "Set", "key": key, "default": False,
                  "operations": [{"operation": "replace", "value": bool(value)}]}]
            ),
            loop,
        )

    def _persist_json(self, key: str, value: Any) -> None:
        loop = self.asyncio_loop
        if loop is None or self.exit_event.is_set():
            return
        asyncio.run_coroutine_threadsafe(
            self.send_msgs(
                [{"cmd": "Set", "key": key, "default": [],
                  "operations": [{"operation": "replace", "value": value}]}]
            ),
            loop,
        )

    def _persist_crafted_indices(self) -> None:
        # Slot storage takes plain JSON, so str-keyed dict.
        self._persist_json(
            _slot_key(self, "crafted_card_indices"),
            {str(k): v for k, v in sorted(self.crafted_card_counts.items())},
        )

    def _persist_str(self, key: str, value: str) -> None:
        loop = self.asyncio_loop
        if loop is None or self.exit_event.is_set():
            return
        asyncio.run_coroutine_threadsafe(
            self.send_msgs(
                [{"cmd": "Set", "key": key, "default": "",
                  "operations": [{"operation": "replace", "value": str(value)}]}]
            ),
            loop,
        )

    def _persist_starter_archetype_cards(self) -> None:
        # Same shape as crafted_card_indices for consistency.
        self._persist_json(
            _slot_key(self, "starter_archetype_cards"),
            {str(k): v for k, v in sorted(self.starter_archetype_card_counts.items())},
        )

    # ---- Crafting -------------------------------------------------------

    def craft_card(self, index: int) -> tuple[bool, str]:
        """Spend DP to grant one more copy of a card index. Cap 3 per card.
        Persists to slot-storage."""
        if not self.save_data_ready:
            return (False, "not connected")
        card = BY_INDEX.get(index)
        if card is None:
            return (False, f"unknown card index {index}")
        cur_count = self.crafted_card_counts.get(index, 0)
        if cur_count >= 3:
            return (False, f"already at cap (3) for {card['name']}")
        cost = crafting_cost_for_card(card)
        try:
            cur_dp = self.memory.read_dp()
        except _PROCESS_GONE_ERRORS as e:
            self._handle_process_gone(e)
            return (False, "game process gone — wait for relaunch")
        except Exception as e:
            logger.exception(f"read_dp failed: {e}")
            return (False, "DP read failed")
        if cur_dp < cost:
            return (False, f"need {cost:,} DP (have {cur_dp:,})")
        try:
            self.memory.add_dp(-cost)
            new_count = self.memory.increment_card_count(index)
        except _PROCESS_GONE_ERRORS as e:
            self._handle_process_gone(e)
            return (False, "game process gone — wait for relaunch")
        except Exception as e:
            logger.exception(f"craft_card({index}) write failed: {e}")
            return (False, "memory write failed")
        self.crafted_card_counts[index] = max(cur_count + 1, new_count)
        self._persist_crafted_indices()
        return (True, f"bought {card['name']} (x{self.crafted_card_counts[index]}) for {cost:,} DP")

    # ---- Starter archetype pick ------------------------------------------

    def _evaluate_starter_archetype(self, owned_names: list[str]) -> None:
        """Detect the precollected pack in items_received and either auto-grant
        a starter archetype (1 candidate) or surface options for the GUI to
        prompt with (2+ candidates).

        Called from Stage B reconcile in a thread; safe because the only
        side-effects are setting context state and (in the auto-grant case)
        a small batch of card-list byte writes."""
        starter_pack = next(
            (n for n in owned_names if n in PACK_ARCHETYPES),
            None,
        )
        if starter_pack is None:
            # No pack item received yet — gate stays closed; will retry next
            # reconcile if the connection re-runs Stage B.
            return
        candidates = [
            d for d in PACK_ARCHETYPES[starter_pack]
            if ARCHETYPE_CARDS.get(d)
        ]
        if not candidates:
            logger.warning(
                f"starter pack {starter_pack!r} has no archetypes with cards; "
                "skipping starter grant"
            )
            return
        if len(candidates) == 1:
            self._grant_starter_archetype(candidates[0])
            return
        # 2+: defer to the GUI. The Kivy modal will call back into
        # _grant_starter_archetype on user pick.
        self.starter_archetype_options = list(candidates)
        self.starter_archetype_pending = True
        logger.info(
            f"starter pack {starter_pack!r}: awaiting archetype pick from "
            f"{candidates}"
        )
        self._refresh_ui()

    def _grant_starter_archetype(self, display: str) -> tuple[bool, str]:
        """Grant STARTER_ARCHETYPE_BUNDLE_SIZE random cards from the picked
        archetype's card pool, persist the pick, clear the pending gate.

        Safe to call from either the Stage-B thread (auto-grant path) or the
        Kivy main thread (modal callback). pymem byte writes are atomic per
        call; existing craft_card path already writes from the Kivy thread."""
        if not self.save_data_ready:
            return (False, "not connected")
        pool = ARCHETYPE_CARDS.get(display)
        if not pool:
            return (False, f"unknown archetype {display!r}")
        n = min(STARTER_ARCHETYPE_BUNDLE_SIZE, len(pool))
        picked = random.sample(pool, n)
        copies = max(1, min(3, STARTER_ARCHETYPE_COPIES))
        granted: dict[int, int] = {}
        for idx in picked:
            try:
                self.memory.raise_card_count(idx, copies)
                granted[idx] = copies
            except _PROCESS_GONE_ERRORS as e:
                self._handle_process_gone(e)
                return (False, "game process gone — wait for relaunch")
            except Exception as e:
                logger.exception(f"grant starter card {idx}: {e}")
        self.starter_archetype = display
        self.starter_archetype_card_counts = granted
        self.starter_archetype_pending = False
        self.starter_archetype_options = []
        self._persist_str(_slot_key(self, "starter_archetype"), display)
        self._persist_starter_archetype_cards()
        sample = [BY_INDEX[i]["name"] for i in picked if i in BY_INDEX][:3]
        logger.info(
            f"starter archetype {display!r}: granted {len(granted)} cards "
            f"(e.g. {sample})"
        )
        self._refresh_ui()
        return (True, f"granted {len(granted)} {display} cards")

    # ---- Process-gone handling -------------------------------------------

    def _handle_process_gone(self, exc: BaseException) -> None:
        """Game process exited (or memory became unreadable). Drop the pymem
        handle, clear the save-data ready flag, reset the watcher, and kick
        off Stage A again so we reattach when the game comes back. Idempotent
        — repeat calls during the reattach window are no-ops."""
        if not self.save_data_ready and self.memory.pm is None:
            return
        logger.warning(f"game process unreachable ({exc!r}); waiting for relaunch")
        self.save_data_ready = False
        self.watcher.reset()
        self.memory.detach()
        self.status_text = "Game process gone — waiting for relaunch..."
        self._refresh_ui()
        # Restart Stage A from the asyncio loop thread. _connect_sequence is
        # idempotent: it'll reattach, rewalk the pointer chain, replay
        # items_received, then resume the live loop.
        loop = self.asyncio_loop
        if loop is not None and not self.exit_event.is_set():
            loop.call_soon_threadsafe(self._restart_connect_sequence)

    def _restart_connect_sequence(self) -> None:
        """Spawn a fresh _connect_sequence task. Runs on the asyncio loop."""
        if self.connect_task is not None and not self.connect_task.done():
            return
        self.connect_task = asyncio.create_task(
            self._connect_sequence(), name="ygo connect-sequence (reattach)"
        )

    def retry_cloud_check(self) -> tuple[bool, str]:
        """GUI hook: re-run `is_cloud_disabled()` and, on success, restart
        Stage A. Returns (now_disabled, message) so the modal can either
        close itself or surface the failure inline.

        Safe to call from the Kivy main thread; the actual filesystem read
        is cheap (small VDF parse) so we run it inline rather than dispatch
        to an executor."""
        if self.save_manager is None:
            return (False, "Save manager not initialized")
        try:
            ok = self.save_manager.is_cloud_disabled()
        except Exception as exc:
            logger.exception(f"is_cloud_disabled() failed: {exc}")
            return (False, f"Cloud check errored: {exc}")
        if not ok:
            return (False, "Steam Cloud still appears enabled — disable in Steam, then click Verify again.")
        self.cloud_off_required = False
        self.cloud_off_satisfied = True
        self.status_text = "Cloud disabled — reconnecting..."
        self._refresh_ui()
        # Kick a fresh connect sequence on the asyncio loop.
        loop = self.asyncio_loop
        if loop is not None and not self.exit_event.is_set():
            loop.call_soon_threadsafe(self._restart_connect_sequence)
        return (True, "Cloud sync confirmed off — reconnecting.")

    # ---- Live poll loop ---------------------------------------------------

    async def _ygo_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.exit_event.is_set():
            try:
                if self.connection_status != ConnectionStatus.CONNECTED:
                    await asyncio.sleep(0.5)
                    continue
                if not self.save_data_ready:
                    await asyncio.sleep(0.5)
                    continue

                await loop.run_in_executor(None, self._tick_body)
                await self._send_pending_checks()
                await self._maybe_send_goal()
                self._refresh_ui()

            except asyncio.CancelledError:
                raise
            except _PROCESS_GONE_ERRORS as e:
                self._handle_process_gone(e)
            except Exception as e:
                logger.exception(e)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _tick_body(self) -> None:
        """Synchronous per-tick: pump new items, sync, watch. Runs in thread.

        Process-gone errors (game exited) propagate out so the caller can
        flip save_data_ready off and trigger a reattach. Other unexpected
        exceptions in inner loops are caught + logged so one bad item doesn't
        break the rest of the tick."""
        # 1) Pump new items.
        new_items = self.items_received[self.highest_processed_item_index:]
        if new_items and self.applier is not None:
            dp_names = self.applier.dp_item_amounts
            for offset, network_item in enumerate(new_items):
                idx = self.highest_processed_item_index + offset
                name = self._lookup_item_name(network_item.item)
                if not name:
                    continue
                is_dp = name in dp_names
                credit = is_dp and idx not in self.applied_dp_indices
                try:
                    self.applier.apply(name, credit_dp=credit)
                except KeyError:
                    logger.warning(f"unknown item name {name!r}")
                    continue
                if is_dp and credit:
                    self.applied_dp_indices.add(idx)
                    self.applied_dp_count += 1
            self.highest_processed_item_index = len(self.items_received)
            self._persist_int(_slot_key(self, "applied_dp_count"), self.applied_dp_count)

        # 2) Re-assert every owned item (SyncEnforcer; never credits DP).
        if self.enforcer is not None:
            owned_names = [
                n for n in (self._lookup_item_name(it.item) for it in self.items_received)
                if n
            ]
            self.enforcer.tick(owned_names)

        for idx, count in self.crafted_card_counts.items():
            try:
                self.memory.raise_card_count(idx, max(1, min(3, count)))
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"reassert crafted card {idx}: {e}")

        for idx, count in self.starter_archetype_card_counts.items():
            try:
                self.memory.raise_card_count(idx, max(1, min(3, count)))
            except _PROCESS_GONE_ERRORS:
                raise
            except Exception as e:
                logger.exception(f"reassert starter card {idx}: {e}")

        # 3) Watch for duel completions.
        new_locs = self.watcher.tick()
        if new_locs:
            self._pending_loc_ids.update(new_locs)

    async def _send_pending_checks(self) -> None:
        if not getattr(self, "_pending_loc_ids", None):
            return
        fresh = self._pending_loc_ids - self.sent_location_ids
        self._pending_loc_ids.clear()
        if not fresh:
            return
        self.sent_location_ids.update(fresh)
        await self.check_locations(fresh)
        logger.info(f"sent {len(fresh)} location checks: {sorted(fresh)}")

    async def _maybe_send_goal(self) -> None:
        if self.finished_game:
            return
        goal_mode = self.slot_data.get("goal_mode")
        if goal_mode == "any_series_finale":
            done = self._goal_any_series_finale()
        elif goal_mode == "duel_count":
            done = self._goal_duel_count()
        else:
            done = False
        if done:
            await self.send_msgs(
                [{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}]
            )
            self.finished_game = True
            logger.info("goal reached — sent CLIENT_GOAL")

    def _goal_any_series_finale(self) -> bool:
        # Goal location was pre-placed with the Victory item. We've reached
        # the goal when any "<finale> Win" loc is checked. Easier: check
        # whether the player has the Victory item.
        return self._has_victory_item()

    def _goal_duel_count(self) -> bool:
        target = int(self.slot_data.get("goal_duel_count", 0))
        if target <= 0:
            return False
        # Count "<duel> Win" loc ids checked. The win+bonus loc ids are
        # paired: every Win loc has an even offset in DUEL_LOC_ID_RANGE
        # (BASE+20001 is the first Win, +20003 the next, etc.). Cheaper
        # path: count Win locs by parity within the duel-loc range.
        from ..locations import DUEL_LOC_ID_RANGE
        lo, hi = DUEL_LOC_ID_RANGE
        wins = sum(
            1 for loc in self.checked_locations
            if lo <= loc <= hi and (loc - lo) % 2 == 0
        )
        return wins >= target

    def _has_victory_item(self) -> bool:
        for it in self.items_received:
            if self._lookup_item_name(it.item) == "Victory":
                return True
        return False

    def _lookup_item_name(self, item_id: int) -> str | None:
        try:
            return self.item_names.lookup_in_game(item_id, self.game)
        except Exception:
            pass
        try:
            return self.item_names[item_id]
        except Exception:
            return None

    # ---- GUI wiring -------------------------------------------------------

    def make_gui(self) -> "type[kvui.GameManager]":
        self.load_kv()
        from .ygo_gui import YGOLotDManager

        def _bind(manager_instance: "YGOLotDManager") -> None:
            self.ui_manager = manager_instance

        YGOLotDManager._bind_ctx = _bind  # type: ignore[attr-defined]
        return YGOLotDManager

    def load_kv(self) -> None:
        import pkgutil

        from kivy.lang import Builder

        data = pkgutil.get_data(__name__, "ygo_client.kv")
        if data is None:
            raise RuntimeError("ygo_client.kv could not be loaded.")
        Builder.load_string(data.decode())

    def _refresh_ui(self) -> None:
        if self.ui_manager is not None:
            self.ui_manager.refresh()


async def main(args: Namespace) -> None:
    if not gui_enabled:
        raise RuntimeError("YGO LotD-LE client requires a GUI; run with kvui enabled.")

    ctx = YGOLotDContext(args.connect, args.password)
    ctx.auth = args.name
    ctx.asyncio_loop = asyncio.get_running_loop()
    # Wire up the save manager. The override flag (--ygo-cloud-disabled-confirmed)
    # bypasses the heuristic remotecache.vdf detection for users whose Steam
    # config doesn't expose a parseable syncstate. SaveManager construction is
    # cheap; ensure_setup() runs lazily inside Stage A.
    cloud_override = bool(getattr(args, "ygo_cloud_disabled_confirmed", False))
    ctx.save_manager = SaveManager(cloud_disabled_override=cloud_override)
    ctx.server_task = asyncio.create_task(server_loop(ctx), name="ygo server loop")

    ctx.run_gui()
    ctx.run_cli()

    await ctx.exit_event.wait()
    await ctx.shutdown()


def launch(*args: str) -> None:
    from .launch import launch_ygo_client
    launch_ygo_client(*args)


if __name__ == "__main__":
    launch(*sys.argv[1:])
