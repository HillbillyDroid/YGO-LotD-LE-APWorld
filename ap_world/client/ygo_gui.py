"""Read-only Kivy GUI for the YGO LotD-LE client.

Tabs:
  - Status: connection state, owned packs, current DP, available next duels.
  - Crafting: spend DP to grant individual cards (search + buy).

All action is server-driven for Status; Crafting is local DP -> in-memory write.
Refresh is thread-safe via Clock.schedule_once."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from kivy.clock import Clock
from kivy.properties import StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kvui import GameManager

from ..data.card_list import BY_INDEX, CARDS
from ..data.pack_archetypes import ARCHETYPE_CARDS
from ..data.duel_table import DUELS, SERIES_NAMES, is_active_duel
from ..locations import DUEL_TO_LOCATIONS
from .item_applier import DUEL_NAME_TO_KEY
from .ygo_client import _PROCESS_GONE_ERRORS, crafting_cost_for_card

if TYPE_CHECKING:
    from .ygo_client import YGOLotDContext


# Index lookups built once at import time. Tutorials + VRAINS gaps excluded.
_DUELS_BY_KEY: dict[tuple[int, int], dict] = {
    (e["series"], e["slot"]): e for e in DUELS if is_active_duel(e)
}
_DUEL_NAMES_BY_SERIES: dict[int, list[tuple[int, str]]] = {i: [] for i in range(6)}
for _e in DUELS:
    if not is_active_duel(_e):
        continue
    _DUEL_NAMES_BY_SERIES[_e["series"]].append((_e["slot"], _e["name"]))
for _series_list in _DUEL_NAMES_BY_SERIES.values():
    _series_list.sort()
del _e, _series_list


_MAX_RESULTS = 100


class DuelChip(Label):
    pass


class SeriesPanel(BoxLayout):
    title = StringProperty("")


class YGOLotDView(BoxLayout):
    pass


class YGOLotDManager(GameManager):
    logging_pairs = [("Client", "Archipelago")]
    base_title = "YGO LotD-LE Archipelago Client"

    _bind_ctx: Callable[["YGOLotDManager"], None] | None = None

    def __init__(self, ctx: "YGOLotDContext") -> None:
        self.ctx: "YGOLotDContext" = ctx  # type: ignore[assignment]
        self._status_label: Label | None = None
        self._summary_label: Label | None = None
        self._series_view: YGOLotDView | None = None
        # Crafting tab state.
        self._search_input: TextInput | None = None
        self._results_view: YGOLotDView | None = None
        self._dp_label: Label | None = None
        self._search_text: str = ""
        self._search_entries: list[tuple[str, int, int, bool]] | None = None
        # Starter-archetype modal. Opened on demand by _refresh_impl when the
        # context flags a pending pick; cleared on dismiss.
        self._archetype_popup: Popup | None = None
        # Cloud-off gate modal. Opened on demand by _refresh_impl when
        # ctx.cloud_off_required is set; cleared on dismiss after a Verify
        # success. Holds a reference to its inline error label so the Verify
        # callback can surface failure messages without tearing down the popup.
        self._cloud_popup: Popup | None = None
        self._cloud_popup_error: Label | None = None
        # Save-swap modal. Opened on demand by _refresh_impl when
        # ctx.swap_pending is set (Step 6 sets this from Stage B). Holds a
        # reference to its status label + polling event so the swap-in-progress
        # countdown can be torn down cleanly on dismiss.
        self._swap_popup: Popup | None = None
        self._swap_popup_status: Label | None = None
        self._swap_popup_swap_btn: Button | None = None
        self._swap_poll_event: object | None = None
        # Saves tab. Re-rendered each tick (list_worlds is a cheap dir-scan
        # + json reads, fine at 1 Hz with the small N of AP worlds a player
        # accumulates).
        self._saves_status_label: Label | None = None
        self._saves_table_view: YGOLotDView | None = None
        # Restore-confirmation modal (used by both "Restore" row buttons and
        # "Restore pre-AP save"). Same close-game polling shape as the swap
        # modal but with separate state so the two can never collide.
        self._restore_popup: Popup | None = None
        self._restore_popup_status: Label | None = None
        self._restore_popup_action_btn: Button | None = None
        self._restore_poll_event: object | None = None
        # Launch-game button (Status tab). Driven by ctx.launch_required +
        # ctx.game_must_close — disabled when neither user-actionable.
        self._launch_button: Button | None = None
        super().__init__(ctx)

        if YGOLotDManager._bind_ctx is not None:
            YGOLotDManager._bind_ctx(self)

    def build(self):
        container = super().build()

        # --- Status tab ----------------------------------------------------
        status_panel = BoxLayout(orientation="vertical")

        self._status_label = Label(
            text="Not connected.",
            size_hint_y=None,
            height=32,
            halign="left",
            valign="middle",
        )
        self._status_label.bind(size=self._status_label.setter("text_size"))
        status_panel.add_widget(self._status_label)

        self._summary_label = Label(
            text="",
            size_hint_y=None,
            height=80,
            halign="left",
            valign="top",
        )
        self._summary_label.bind(size=self._summary_label.setter("text_size"))
        status_panel.add_widget(self._summary_label)

        # Launch-game button row. Disabled by default; enabled by
        # _refresh_impl when ctx.launch_required is True. Label updates
        # to reflect why it's disabled (game running externally, no
        # connection, already attached, etc.).
        launch_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                               height=40, padding=4, spacing=4)
        self._launch_button = Button(text="Launch game", disabled=True)
        self._launch_button.bind(on_press=lambda _b: self._on_launch_clicked())
        launch_row.add_widget(self._launch_button)
        status_panel.add_widget(launch_row)

        scroll = ScrollView(size_hint=(1, 1))
        self._series_view = YGOLotDView()
        scroll.add_widget(self._series_view)
        status_panel.add_widget(scroll)

        self.add_client_tab("Status", status_panel)

        # --- Crafting tab --------------------------------------------------
        crafting_panel = BoxLayout(orientation="vertical")

        top_bar = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=4, padding=4)
        top_bar.add_widget(Label(text="Search:", size_hint_x=None, width=70, halign="right", valign="middle"))
        self._search_input = TextInput(multiline=False)
        self._search_input.bind(on_text_validate=lambda _i: self._on_search_clicked())
        top_bar.add_widget(self._search_input)
        search_btn = Button(text="Search", size_hint_x=None, width=90)
        search_btn.bind(on_press=lambda _b: self._on_search_clicked())
        top_bar.add_widget(search_btn)
        self._dp_label = Label(text="DP: -", size_hint_x=None, width=160, halign="right", valign="middle")
        self._dp_label.bind(size=self._dp_label.setter("text_size"))
        top_bar.add_widget(self._dp_label)
        crafting_panel.add_widget(top_bar)

        results_scroll = ScrollView(size_hint=(1, 1))
        self._results_view = YGOLotDView()
        results_scroll.add_widget(self._results_view)
        crafting_panel.add_widget(results_scroll)

        self.add_client_tab("Crafting", crafting_panel)

        # --- Saves tab -----------------------------------------------------
        saves_panel = BoxLayout(orientation="vertical", spacing=4, padding=4)

        self._saves_status_label = Label(
            text="Save manager not initialized.",
            size_hint_y=None, height=48,
            halign="left", valign="top",
        )
        self._saves_status_label.bind(size=self._saves_status_label.setter("text_size"))
        saves_panel.add_widget(self._saves_status_label)

        action_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                               height=36, spacing=6)
        restore_pre_ap_btn = Button(text="Restore pre-AP save")
        restore_pre_ap_btn.bind(on_press=lambda _b: self._on_restore_pre_ap_clicked())
        open_folder_btn = Button(text="Open backup folder")
        open_folder_btn.bind(on_press=lambda _b: self._on_open_backup_folder_clicked())
        action_row.add_widget(restore_pre_ap_btn)
        action_row.add_widget(open_folder_btn)
        saves_panel.add_widget(action_row)

        saves_scroll = ScrollView(size_hint=(1, 1))
        self._saves_table_view = YGOLotDView()
        saves_scroll.add_widget(self._saves_table_view)
        saves_panel.add_widget(saves_scroll)

        self.add_client_tab("Saves", saves_panel)

        # Initial paint — top-50 placeholder list so the panel isn't blank
        # before the user touches Search.
        Clock.schedule_once(lambda _dt: self._render_crafting_results(), 0)
        Clock.schedule_once(lambda _dt: self._render_saves_tab(), 0)

        return container

    # ---- thread-safe public helpers --------------------------------------

    def refresh(self) -> None:
        Clock.schedule_once(lambda _dt: self._refresh_impl(), 0)

    def on_connected(self, slot_data: dict) -> None:
        # Force a rebuild of the search index on (re)connect.
        self._search_entries = None
        Clock.schedule_once(lambda _dt: self._refresh_impl(), 0)

    # ---- render ----------------------------------------------------------

    def _refresh_impl(self) -> None:
        ctx = self.ctx
        slot_data = ctx.slot_data or {}

        ready = ctx.save_data_ready
        if self._status_label is not None:
            if not slot_data:
                self._status_label.text = ctx.status_text or "Not connected."
            else:
                attach_str = ctx.memory.process_name or "(none)"
                self._status_label.text = (
                    f"{ctx.status_text}  |  attached={attach_str}  |  save-ready={ready}"
                )

        if self._summary_label is not None:
            self._summary_label.text = self._build_summary_text()

        self._update_launch_button()

        self._render_series_panels()
        # Crafting results aren't rebuilt on every tick (full-catalog filter
        # is laggy); only the DP label + button-disabled states refresh here.
        self._update_crafting_dp()

        # Starter-archetype picker. Wait for save_data_ready before showing
        # the modal — the Pick buttons grant cards via direct memory writes
        # and need the pointer chain resolved. Close the modal once the pick
        # is recorded (covers both modal-dismiss and any external grant path).
        if (ctx.starter_archetype_pending and ready
                and self._archetype_popup is None):
            self._open_archetype_popup(list(ctx.starter_archetype_options))
        elif not ctx.starter_archetype_pending and self._archetype_popup is not None:
            self._archetype_popup.dismiss()
            self._archetype_popup = None

        # Cloud-off gate. Pops up the moment Stage A flags it; tears down once
        # Verify succeeds (ctx clears `cloud_off_required`) or some other path
        # satisfied the gate.
        if ctx.cloud_off_required and self._cloud_popup is None:
            self._open_cloud_popup()
        elif not ctx.cloud_off_required and self._cloud_popup is not None:
            self._cloud_popup.dismiss()
            self._cloud_popup = None
            self._cloud_popup_error = None

        # Saves tab — cheap re-render each tick (small N of worlds).
        self._render_saves_tab()

        # Save-swap gate. Opens the moment Stage A.1 detects a mismatch
        # (pre-attach, so save_data_ready is False here — the modal is
        # not gated on it). Tears down once `swap_pending` clears (either
        # by swap or by the user picking "Skip this session").
        if (ctx.swap_pending and ctx.swap_world_key
                and self._swap_popup is None):
            self._open_swap_popup(ctx.swap_world_key)
        elif not ctx.swap_pending and self._swap_popup is not None:
            self._dismiss_swap_popup()

    def _build_summary_text(self) -> str:
        ctx = self.ctx
        owned_names = self._owned_item_names()
        owned_packs = sorted(
            n for n in owned_names if n in (ctx.slot_data.get("pack_item_to_bit") or {})
        )
        dp = self._read_dp_safe()
        goal_mode = ctx.slot_data.get("goal_mode", "?")
        goal_count = ctx.slot_data.get("goal_duel_count", "")
        goal_str = (
            f"{goal_mode} ({goal_count})"
            if goal_mode == "duel_count" else str(goal_mode)
        )

        win_total = self._win_locations_checked()
        return (
            f"Goal: {goal_str}\n"
            f"Owned packs: {len(owned_packs)} — {', '.join(owned_packs) if owned_packs else '(none)'}\n"
            f"Current DP: {dp:,}\n"
            f"Wins recorded: {win_total} / {len(_DUELS_BY_KEY)}"
        )

    def _read_dp_safe(self) -> int:
        if not self.ctx.save_data_ready:
            return 0
        try:
            return int(self.ctx.memory.read_dp())
        except _PROCESS_GONE_ERRORS as e:
            self.ctx._handle_process_gone(e)
            return 0
        except Exception:
            return 0

    def _read_card_count_safe(self, index: int) -> int:
        """Best-effort read of the live card-list count for `index`.

        Returns 0 if save data isn't ready or the read fails. Used by the
        Crafting tab to gate the Buy button on the actual in-game owned
        count (which can include AP item-grants and starter-archetype
        cards, not just `crafted_card_counts`)."""
        if not self.ctx.save_data_ready:
            return 0
        try:
            return int(self.ctx.memory.read_card_count(index))
        except _PROCESS_GONE_ERRORS as e:
            self.ctx._handle_process_gone(e)
            return 0
        except Exception:
            return 0

    def _owned_item_names(self) -> set[str]:
        names: set[str] = set()
        for it in self.ctx.items_received:
            n = self.ctx._lookup_item_name(it.item)
            if n:
                names.add(n)
        return names

    def _win_locations_checked(self) -> int:
        from ..locations import DUEL_LOC_ID_RANGE
        lo, hi = DUEL_LOC_ID_RANGE
        return sum(
            1 for loc in self.ctx.checked_locations
            if lo <= loc <= hi and (loc - lo) % 2 == 0
        )

    def _update_launch_button(self) -> None:
        """Refresh the Launch button text + enabled state from context flags.

        Enabled iff `ctx.launch_required AND not ctx.game_must_close`. Label
        explains the disabled reason so the user isn't left guessing why
        clicking does nothing.
        """
        if self._launch_button is None:
            return
        ctx = self.ctx
        if ctx.game_must_close:
            self._launch_button.text = "Game running — close it first"
            self._launch_button.disabled = True
        elif ctx.launch_required:
            self._launch_button.text = "Launch game"
            self._launch_button.disabled = False
        elif ctx.memory.pm is not None:
            self._launch_button.text = "Game already attached"
            self._launch_button.disabled = True
        elif not ctx.slot_data:
            self._launch_button.text = "Connect to AP server first"
            self._launch_button.disabled = True
        else:
            # Connected, no game running, but no launch_required either —
            # we're between Stage A bails (e.g. cloud-off pending). Show
            # neutral text; the modal handles the actionable bit.
            self._launch_button.text = "Launch game (waiting for setup)"
            self._launch_button.disabled = True

    def _on_launch_clicked(self) -> None:
        from CommonClient import logger
        ok, msg = self.ctx.launch_game()
        logger.info(msg)
        # _refresh_impl will retitle the button + flip disabled on the next
        # tick driven by ctx.launch_required clearing.

    def _render_series_panels(self) -> None:
        if self._series_view is None:
            return
        self._series_view.clear_widgets()

        owned = self._owned_item_names()
        owned_unlocks = {
            DUEL_NAME_TO_KEY[name]
            for name in owned
            if name in DUEL_NAME_TO_KEY
        }
        checked = self.ctx.checked_locations

        for series_idx in range(6):
            sname = SERIES_NAMES[series_idx]
            entries = _DUEL_NAMES_BY_SERIES[series_idx]
            available: list[tuple[int, str]] = []
            for slot, name in entries:
                if (series_idx, slot) not in owned_unlocks:
                    continue
                win_id = DUEL_TO_LOCATIONS[(series_idx, slot)]["win"]
                if win_id in checked:
                    continue
                available.append((slot, name))

            panel = SeriesPanel()
            done_count = sum(
                1
                for slot, _ in entries
                if DUEL_TO_LOCATIONS[(series_idx, slot)]["win"] in checked
            )
            panel.title = f"{sname}  —  {done_count}/{len(entries)} cleared, {len(available)} available"

            if not available:
                panel.add_widget(DuelChip(text="(no available duels)"))
            else:
                preview = available[:10]
                for slot, name in preview:
                    chip = DuelChip(text=f"#{slot:02d}  {name}")
                    panel.add_widget(chip)
                if len(available) > len(preview):
                    panel.add_widget(DuelChip(
                        text=f"... and {len(available) - len(preview)} more"
                    ))

            self._series_view.add_widget(panel)

    # ---- Crafting --------------------------------------------------------

    def _build_search_index(self) -> list[tuple[str, int, int, bool]]:
        """(name, index, cost, is_staple) tuples; sorted alphabetically.

        Sourced from the full card catalog (not slot_data.card_item_to_index,
        which only holds AP-pool staples). Tokens are excluded — they aren't
        real cards. Crafting only requires a valid CardListSaveData index, so
        no slot-data dependency is needed."""
        entries: list[tuple[str, int, int, bool]] = []
        seen_names: set[str] = set()
        for card in CARDS:
            tags = card["tags"]
            if "token" in tags:
                continue
            name = card["name"]
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            is_staple = any(t.startswith("staple_") for t in tags)
            cost = crafting_cost_for_card(card)
            entries.append((name, int(card["index"]), cost, is_staple))
        entries.sort(key=lambda e: e[0].lower())
        return entries

    def _on_search_clicked(self) -> None:
        if self._search_input is not None:
            self._search_text = self._search_input.text.strip().lower()
        self._render_crafting_results()

    # ---- starter archetype modal ----------------------------------------

    def _open_archetype_popup(self, options: list[str]) -> None:
        """Open a modal that lets the player pick one starter archetype.
        Each option button shows the archetype name plus a sample of cards
        the pick would grant. Modal is non-dismissable to enforce the choice."""
        body = BoxLayout(orientation="vertical", spacing=6, padding=8)
        body.add_widget(Label(
            text="Pick a starter archetype. Cards will be granted immediately.",
            size_hint_y=None, height=28,
        ))

        rows_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=4)
        rows_box.bind(minimum_height=rows_box.setter("height"))

        for display in options:
            row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=56, spacing=6)
            samples = self._archetype_sample_text(display)
            text_lbl = Label(
                text=f"[b]{display}[/b]\n{samples}",
                markup=True, halign="left", valign="middle",
                size_hint_x=0.75,
            )
            text_lbl.bind(size=text_lbl.setter("text_size"))
            pick_btn = Button(text="Pick", size_hint_x=0.25)
            pick_btn.bind(on_press=lambda _btn, d=display: self._on_archetype_picked(d))
            row.add_widget(text_lbl)
            row.add_widget(pick_btn)
            rows_box.add_widget(row)

        scroll = ScrollView(size_hint=(1, 1))
        scroll.add_widget(rows_box)
        body.add_widget(scroll)

        popup = Popup(
            title="Starter archetype",
            content=body,
            size_hint=(0.8, 0.8),
            auto_dismiss=False,
        )
        self._archetype_popup = popup
        popup.open()

    def _archetype_sample_text(self, display: str) -> str:
        indices = ARCHETYPE_CARDS.get(display) or []
        names: list[str] = []
        for idx in indices[:3]:
            card = BY_INDEX.get(idx)
            if card and card.get("name"):
                names.append(card["name"])
        suffix = f" (+{max(0, len(indices) - 3)} more)" if len(indices) > 3 else ""
        return ("Sample: " + ", ".join(names) + suffix) if names else "(no cards)"

    # ---- cloud-off modal -------------------------------------------------

    def _open_cloud_popup(self) -> None:
        """Modal blocking Stage A until the user confirms Steam Cloud is off
        for LotD-LE. Mirrors the archetype-picker pattern (non-dismissable,
        scrollable in case the user's resolution is tiny). Verify button
        re-runs `is_cloud_disabled()` and either closes the modal + restarts
        Stage A on success or surfaces the failure inline."""
        body = BoxLayout(orientation="vertical", spacing=8, padding=10)
        instructions = Label(
            text=(
                "[b]Disable Steam Cloud sync for Yu-Gi-Oh! Legacy of the Duelist: "
                "Link Evolution before continuing.[/b]\n\n"
                "Steps:\n"
                "  1. Open Steam.\n"
                "  2. Right-click [b]Yu-Gi-Oh! Legacy of the Duelist: Link Evolution[/b] "
                "in your Library.\n"
                "  3. Properties -> General -> uncheck "
                "[b]Keep games saves in the Steam Cloud[/b].\n"
                "  4. Click Verify below.\n\n"
                "Why: the AP client backs up and swaps savegame.dat between worlds. "
                "Steam Cloud overwrites those swaps from the server-side copy on "
                "next launch, which would corrupt your AP world progress."
            ),
            markup=True, halign="left", valign="top",
        )
        instructions.bind(size=instructions.setter("text_size"))
        body.add_widget(instructions)

        self._cloud_popup_error = Label(
            text="", color=(1.0, 0.5, 0.5, 1.0),
            size_hint_y=None, height=24,
            halign="left", valign="middle",
        )
        self._cloud_popup_error.bind(size=self._cloud_popup_error.setter("text_size"))
        body.add_widget(self._cloud_popup_error)

        btn_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=40, spacing=8)
        verify_btn = Button(text="Verify")
        verify_btn.bind(on_press=lambda _b: self._on_cloud_verify_clicked())
        btn_row.add_widget(verify_btn)
        body.add_widget(btn_row)

        popup = Popup(
            title="Steam Cloud must be disabled",
            content=body,
            size_hint=(0.8, 0.8),
            auto_dismiss=False,
        )
        self._cloud_popup = popup
        popup.open()

    def _on_cloud_verify_clicked(self) -> None:
        from CommonClient import logger
        ok, msg = self.ctx.retry_cloud_check()
        logger.info(msg)
        if ok:
            # _refresh_impl will tear the popup down once `cloud_off_required`
            # flips False on the next refresh tick (driven by the connect
            # sequence's status_text update).
            if self._cloud_popup is not None:
                self._cloud_popup.dismiss()
                self._cloud_popup = None
                self._cloud_popup_error = None
        elif self._cloud_popup_error is not None:
            self._cloud_popup_error.text = msg

    # ---- save-swap modal -------------------------------------------------

    def _open_swap_popup(self, world_key: str) -> None:
        """Modal that surfaces a save mismatch detected at Stage B.

        The "Close game and swap" button polls until pymem can no longer
        attach to the LotD-LE process, then runs `ctx.perform_save_swap()`.
        "Skip this session" clears the pending flag without touching the
        save; reconnect re-evaluates."""
        body = BoxLayout(orientation="vertical", spacing=8, padding=10)
        instructions = Label(
            text=(
                f"[b]Save mismatch detected for AP world [color=ffcc88]{world_key}[/color].[/b]\n\n"
                "The savegame.dat in your Steam userdata does not match the "
                "backup tracked for this AP world. Click [b]Swap[/b] to:\n\n"
                "  1. Capture the current userdata save (into its matching "
                "backup if recognized, otherwise to a timestamped orphan dir).\n"
                "  2. Replace it with this world's tracked save.\n"
                "  3. Click Launch on the Status tab to start the game.\n\n"
                "Or click [b]Skip this session[/b] to leave the file alone "
                "(your AP state will not match what's loaded in-game).\n\n"
                "(If the game is currently running, the Swap button will wait "
                "for you to close it first.)"
            ),
            markup=True, halign="left", valign="top",
        )
        instructions.bind(size=instructions.setter("text_size"))
        body.add_widget(instructions)

        self._swap_popup_status = Label(
            text="", color=(1.0, 0.85, 0.5, 1.0),
            size_hint_y=None, height=24,
            halign="left", valign="middle",
        )
        self._swap_popup_status.bind(size=self._swap_popup_status.setter("text_size"))
        body.add_widget(self._swap_popup_status)

        btn_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=40, spacing=8)
        swap_btn = Button(text="Swap")
        swap_btn.bind(on_press=lambda _b, k=world_key: self._on_swap_clicked(k))
        skip_btn = Button(text="Skip this session")
        skip_btn.bind(on_press=lambda _b: self._on_swap_skipped())
        btn_row.add_widget(swap_btn)
        btn_row.add_widget(skip_btn)
        body.add_widget(btn_row)

        self._swap_popup_swap_btn = swap_btn

        popup = Popup(
            title="Save swap required",
            content=body,
            size_hint=(0.8, 0.8),
            auto_dismiss=False,
        )
        self._swap_popup = popup
        popup.open()

    def _dismiss_swap_popup(self) -> None:
        """Tear down the swap popup + cancel any in-flight game-closed poll."""
        if self._swap_poll_event is not None:
            try:
                self._swap_poll_event.cancel()
            except Exception:
                pass
            self._swap_poll_event = None
        if self._swap_popup is not None:
            self._swap_popup.dismiss()
            self._swap_popup = None
        self._swap_popup_status = None
        self._swap_popup_swap_btn = None

    def _on_swap_clicked(self, world_key: str) -> None:
        """Run the swap immediately if the game is closed; otherwise begin
        polling for it to close.

        New flow (Stage A.1): the modal is opened pre-attach, so the game
        is almost always already closed when the user clicks. Skip the poll
        loop in that case and run `perform_save_swap` inline. The polling
        path stays as a defensive fallback for any future code path that
        opens the modal mid-session."""
        if self._swap_poll_event is not None:
            return  # already polling
        if self._swap_popup_swap_btn is not None:
            self._swap_popup_swap_btn.disabled = True
        # Detach our own pymem handle so its cached process reference doesn't
        # keep a side-channel alive. Idempotent if the handle was never opened.
        try:
            self.ctx.memory.detach()
        except Exception:
            pass
        # Fast-path: game already closed → swap immediately.
        from .save_manager import _is_game_running
        if not _is_game_running():
            from CommonClient import logger
            ok, msg = self.ctx.perform_save_swap(world_key)
            logger.info(msg)
            if ok:
                self._dismiss_swap_popup()
            else:
                if self._swap_popup_status is not None:
                    self._swap_popup_status.text = msg
                if self._swap_popup_swap_btn is not None:
                    self._swap_popup_swap_btn.disabled = False
            return
        # Slow-path: game running → poll until closed.
        if self._swap_popup_status is not None:
            self._swap_popup_status.text = "Waiting for the game to close..."
        self._swap_poll_event = Clock.schedule_interval(
            lambda _dt, k=world_key: self._poll_for_game_closed(k), 1.0,
        )

    def _poll_for_game_closed(self, world_key: str) -> None:
        """Clock callback. Once the game can no longer be attached, run the
        swap and tear down the modal."""
        from .save_manager import _is_game_running
        if _is_game_running():
            return
        # Stop polling first so the swap can't be re-entered if it takes >1s.
        if self._swap_poll_event is not None:
            try:
                self._swap_poll_event.cancel()
            except Exception:
                pass
            self._swap_poll_event = None

        from CommonClient import logger
        ok, msg = self.ctx.perform_save_swap(world_key)
        logger.info(msg)
        if ok:
            self._dismiss_swap_popup()
        else:
            # Surface the failure inline; leave the modal open so the user can
            # retry (e.g. they relaunched the game between polls).
            if self._swap_popup_status is not None:
                self._swap_popup_status.text = msg
            if self._swap_popup_swap_btn is not None:
                self._swap_popup_swap_btn.disabled = False

    def _on_swap_skipped(self) -> None:
        from CommonClient import logger
        self.ctx.skip_save_swap()
        logger.info("save swap skipped for this session")
        self._dismiss_swap_popup()

    # ---- Saves tab -------------------------------------------------------

    def _render_saves_tab(self) -> None:
        if self._saves_status_label is None or self._saves_table_view is None:
            return
        sm = self.ctx.save_manager
        if sm is None:
            self._saves_status_label.text = "Save manager not initialized."
            self._saves_table_view.clear_widgets()
            return

        try:
            worlds = sm.list_worlds()
        except Exception as exc:
            self._saves_status_label.text = f"Could not list worlds: {exc}"
            self._saves_table_view.clear_widgets()
            return

        try:
            last_active = sm.get_last_active_world() or "(none)"
        except Exception:
            last_active = "(unknown)"
        cloud_status = "off" if self.ctx.cloud_off_satisfied else "on/unknown"
        self._saves_status_label.text = (
            f"Active world: {last_active}\n"
            f"Steam Cloud: {cloud_status}    Backups: {len(worlds)}"
        )

        self._saves_table_view.clear_widgets()
        if not worlds:
            self._saves_table_view.add_widget(DuelChip(text="(no AP worlds tracked yet)"))
            return

        # Header row.
        header = BoxLayout(orientation="horizontal", size_hint_y=None, height=24, spacing=4)
        for text, weight in (
            ("World key", 0.5),
            ("Created", 0.2),
            ("Last synced", 0.2),
            ("", 0.1),
        ):
            lbl = Label(text=f"[b]{text}[/b]", markup=True,
                        size_hint_x=weight, halign="left", valign="middle")
            lbl.bind(size=lbl.setter("text_size"))
            header.add_widget(lbl)
        self._saves_table_view.add_widget(header)

        for meta in worlds:
            row = BoxLayout(orientation="horizontal", size_hint_y=None, height=28, spacing=4)
            is_active = meta.world_key == sm.get_last_active_world()
            key_text = f"[b]{meta.world_key}[/b]  (active)" if is_active else meta.world_key
            key_lbl = Label(text=key_text, markup=True,
                            size_hint_x=0.5, halign="left", valign="middle")
            key_lbl.bind(size=key_lbl.setter("text_size"))
            row.add_widget(key_lbl)
            row.add_widget(self._timestamp_label(meta.created_at, 0.2))
            row.add_widget(self._timestamp_label(meta.last_synced_at, 0.2))
            restore_btn = Button(text="Restore", size_hint_x=0.1, disabled=is_active)
            restore_btn.bind(
                on_press=lambda _b, k=meta.world_key: self._on_restore_world_clicked(k)
            )
            row.add_widget(restore_btn)
            self._saves_table_view.add_widget(row)

    @staticmethod
    def _timestamp_label(iso_value: str, weight: float) -> Label:
        # ISO-8601 with microseconds is too dense for the row; clip to date+time.
        text = (iso_value or "").split(".")[0].replace("T", " ")
        lbl = Label(text=text, size_hint_x=weight, halign="left", valign="middle")
        lbl.bind(size=lbl.setter("text_size"))
        return lbl

    def _on_open_backup_folder_clicked(self) -> None:
        import os
        sm = self.ctx.save_manager
        if sm is None:
            return
        backups_dir = sm.paths.backups
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(backups_dir))  # type: ignore[attr-defined]
        except Exception as exc:
            from CommonClient import logger
            logger.warning(f"Could not open backup folder {backups_dir}: {exc}")

    # ---- restore-confirmation modal (used by row Restore + pre-AP) ------

    def _on_restore_world_clicked(self, world_key: str) -> None:
        self._open_restore_popup(
            title=f"Restore world: {world_key}",
            body_text=(
                f"[b]Restore the AP world [color=ffcc88]{world_key}[/color]?[/b]\n\n"
                "The current savegame.dat will be captured (into its matching "
                "backup if recognized, otherwise to a timestamped orphan dir) "
                "and replaced with this world's tracked save. Then click "
                "Launch on the Status tab to start the game.\n\n"
                "(If the game is currently running, the Restore button will "
                "wait for you to close it first.)"
            ),
            action_text="Restore",
            action=lambda: self.ctx.perform_save_swap(world_key),
        )

    def _on_restore_pre_ap_clicked(self) -> None:
        sm = self.ctx.save_manager
        if sm is None:
            return
        if not sm.paths.pre_ap_save.exists():
            from CommonClient import logger
            logger.warning("Pre-AP snapshot does not exist; nothing to restore.")
            return
        self._open_restore_popup(
            title="Restore pre-AP save",
            body_text=(
                "[b]Restore the original pre-AP savegame.dat?[/b]\n\n"
                "The current save will be captured (into its matching AP "
                "world backup if recognized, otherwise to a timestamped "
                "orphan dir) and the snapshot taken the very first time you "
                "ran the AP client will be put back in place. AP will not "
                "reconnect to a world until you load a connected slot.\n\n"
                "(If the game is currently running, the Restore button will "
                "wait for you to close it first.)"
            ),
            action_text="Restore",
            action=lambda: self.ctx.perform_pre_ap_restore(),
        )

    def _open_restore_popup(
        self,
        *,
        title: str,
        body_text: str,
        action_text: str,
        action: Callable[[], tuple[bool, str]],
    ) -> None:
        if self._restore_popup is not None:
            return  # already open
        body = BoxLayout(orientation="vertical", spacing=8, padding=10)
        instructions = Label(
            text=body_text, markup=True, halign="left", valign="top",
        )
        instructions.bind(size=instructions.setter("text_size"))
        body.add_widget(instructions)

        self._restore_popup_status = Label(
            text="", color=(1.0, 0.85, 0.5, 1.0),
            size_hint_y=None, height=24,
            halign="left", valign="middle",
        )
        self._restore_popup_status.bind(size=self._restore_popup_status.setter("text_size"))
        body.add_widget(self._restore_popup_status)

        btn_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=40, spacing=8)
        action_btn = Button(text=action_text)
        action_btn.bind(on_press=lambda _b: self._on_restore_clicked(action))
        cancel_btn = Button(text="Cancel")
        cancel_btn.bind(on_press=lambda _b: self._dismiss_restore_popup())
        btn_row.add_widget(action_btn)
        btn_row.add_widget(cancel_btn)
        body.add_widget(btn_row)

        self._restore_popup_action_btn = action_btn

        popup = Popup(
            title=title,
            content=body,
            size_hint=(0.8, 0.8),
            auto_dismiss=False,
        )
        self._restore_popup = popup
        popup.open()

    def _on_restore_clicked(self, action: Callable[[], tuple[bool, str]]) -> None:
        if self._restore_poll_event is not None:
            return  # already polling
        if self._restore_popup_action_btn is not None:
            self._restore_popup_action_btn.disabled = True
        try:
            self.ctx.memory.detach()
        except Exception:
            pass
        # Fast-path: game already closed → run the action inline. The Saves
        # tab is the typical entry point and the user almost always opens it
        # with the game closed (per the new Stage A.-1 model).
        from .save_manager import _is_game_running
        if not _is_game_running():
            from CommonClient import logger
            ok, msg = action()
            logger.info(msg)
            if ok:
                self._dismiss_restore_popup()
            else:
                if self._restore_popup_status is not None:
                    self._restore_popup_status.text = msg
                if self._restore_popup_action_btn is not None:
                    self._restore_popup_action_btn.disabled = False
            return
        # Slow-path: game running → poll until closed.
        if self._restore_popup_status is not None:
            self._restore_popup_status.text = "Waiting for the game to close..."
        self._restore_poll_event = Clock.schedule_interval(
            lambda _dt, a=action: self._poll_for_restore_game_closed(a), 1.0,
        )

    def _poll_for_restore_game_closed(
        self, action: Callable[[], tuple[bool, str]],
    ) -> None:
        from .save_manager import _is_game_running
        if _is_game_running():
            return
        if self._restore_poll_event is not None:
            try:
                self._restore_poll_event.cancel()
            except Exception:
                pass
            self._restore_poll_event = None

        from CommonClient import logger
        ok, msg = action()
        logger.info(msg)
        if ok:
            self._dismiss_restore_popup()
        else:
            if self._restore_popup_status is not None:
                self._restore_popup_status.text = msg
            if self._restore_popup_action_btn is not None:
                self._restore_popup_action_btn.disabled = False

    def _dismiss_restore_popup(self) -> None:
        if self._restore_poll_event is not None:
            try:
                self._restore_poll_event.cancel()
            except Exception:
                pass
            self._restore_poll_event = None
        if self._restore_popup is not None:
            self._restore_popup.dismiss()
            self._restore_popup = None
        self._restore_popup_status = None
        self._restore_popup_action_btn = None

    def _on_archetype_picked(self, display: str) -> None:
        from CommonClient import logger
        ok, msg = self.ctx._grant_starter_archetype(display)
        logger.info(msg)
        if ok and self._archetype_popup is not None:
            self._archetype_popup.dismiss()
            self._archetype_popup = None
            # Refresh main panel so any starter cards visible in the trunk
            # status update immediately.
            self._refresh_impl()

    def _on_buy_clicked(self, index: int) -> None:
        from CommonClient import logger
        ok, msg = self.ctx.craft_card(index)
        logger.info(msg)
        # Refresh DP label + button states; re-render results so the cost row
        # for this card reflects the new count and any cap-reached state.
        self._update_crafting_dp()
        self._render_crafting_results()

    @staticmethod
    def _cost_label_text(cost: int, owned: int) -> str:
        if owned >= 3:
            return "Cap (3/3)"
        return f"{cost:,} DP"

    def _update_crafting_dp(self) -> None:
        """Cheap update — DP label, per-row Buy disabled state, per-row cost
        label (which flips to "Cap (3/3)" once the player owns 3 copies of
        that card via any path: AP grant, archetype starter, or crafting).
        Does NOT rebuild result rows."""
        if self._dp_label is None or self._results_view is None:
            return
        dp = self._read_dp_safe()
        self._dp_label.text = f"DP: {dp:,}"
        ready = self.ctx.save_data_ready
        for row in self._results_view.children:
            cost = getattr(row, "_buy_cost", None)
            btn = getattr(row, "_buy_btn", None)
            idx = getattr(row, "_buy_idx", None)
            cost_lbl = getattr(row, "_cost_label", None)
            if cost is None or btn is None or idx is None:
                continue
            owned = self._read_card_count_safe(idx)
            btn.disabled = (not ready) or (dp < cost) or (owned >= 3)
            if cost_lbl is not None:
                cost_lbl.text = self._cost_label_text(cost, owned)

    def _render_crafting_results(self) -> None:
        if self._results_view is None or self._dp_label is None:
            return

        dp = self._read_dp_safe()
        self._dp_label.text = f"DP: {dp:,}"

        self._results_view.clear_widgets()

        if self._search_entries is None:
            self._search_entries = self._build_search_index()

        if not self._search_entries:
            self._results_view.add_widget(DuelChip(text="(no craftable cards)"))
            return

        query = self._search_text
        if not query:
            matches = self._search_entries[:50]
            truncated_total = 0
            if not matches:
                self._results_view.add_widget(DuelChip(text="(type a query and click Search)"))
                return
        else:
            filtered = [e for e in self._search_entries if query in e[0].lower()]
            truncated_total = max(0, len(filtered) - _MAX_RESULTS)
            matches = filtered[:_MAX_RESULTS]
            if not matches:
                self._results_view.add_widget(DuelChip(text="(no matches)"))
                return

        ready = self.ctx.save_data_ready
        for name, idx, cost, is_staple in matches:
            row = BoxLayout(orientation="horizontal", size_hint_y=None, height=28, spacing=4)
            label_text = f"{name}  [staple]" if is_staple else name
            name_lbl = Label(text=label_text, size_hint_x=0.6, halign="left", valign="middle")
            name_lbl.bind(size=name_lbl.setter("text_size"))
            owned = self._read_card_count_safe(idx)
            cost_lbl = Label(
                text=self._cost_label_text(cost, owned),
                size_hint_x=0.2, halign="right", valign="middle",
            )
            cost_lbl.bind(size=cost_lbl.setter("text_size"))
            buy_btn = Button(text="Buy", size_hint_x=0.2)
            buy_btn.disabled = (not ready) or (dp < cost) or (owned >= 3)
            buy_btn.bind(on_press=lambda _btn, i=idx: self._on_buy_clicked(i))
            row.add_widget(name_lbl)
            row.add_widget(cost_lbl)
            row.add_widget(buy_btn)
            row._buy_cost = cost
            row._buy_btn = buy_btn
            row._buy_idx = idx
            row._cost_label = cost_lbl
            self._results_view.add_widget(row)

        if truncated_total > 0:
            self._results_view.add_widget(DuelChip(
                text=f"... and {truncated_total} more — refine your search"
            ))
