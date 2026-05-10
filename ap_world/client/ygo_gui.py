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

        # Initial paint — top-50 placeholder list so the panel isn't blank
        # before the user touches Search.
        Clock.schedule_once(lambda _dt: self._render_crafting_results(), 0)

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

    def _update_crafting_dp(self) -> None:
        """Cheap update — just the DP label and per-row button disabled state.
        Does NOT rebuild result rows."""
        if self._dp_label is None or self._results_view is None:
            return
        dp = self._read_dp_safe()
        self._dp_label.text = f"DP: {dp:,}"
        ready = self.ctx.save_data_ready
        for row in self._results_view.children:
            cost = getattr(row, "_buy_cost", None)
            btn = getattr(row, "_buy_btn", None)
            if cost is None or btn is None:
                continue
            btn.disabled = (not ready) or (dp < cost)

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
            cost_lbl = Label(text=f"{cost:,} DP", size_hint_x=0.2, halign="right", valign="middle")
            cost_lbl.bind(size=cost_lbl.setter("text_size"))
            buy_btn = Button(text="Buy", size_hint_x=0.2)
            buy_btn.disabled = (not ready) or (dp < cost)
            buy_btn.bind(on_press=lambda _btn, i=idx: self._on_buy_clicked(i))
            row.add_widget(name_lbl)
            row.add_widget(cost_lbl)
            row.add_widget(buy_btn)
            row._buy_cost = cost
            row._buy_btn = buy_btn
            self._results_view.add_widget(row)

        if truncated_total > 0:
            self._results_view.add_widget(DuelChip(
                text=f"... and {truncated_total} more — refine your search"
            ))
