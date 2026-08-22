"""
controls.py

The scan configuration panel (Interval, Contracts, History, Analytics,
Run Scan) and the Strategy Templates section -- ONE working strategy
grid (curve positions as columns, one row per template, each row
carrying its OWN Market/Interval), with the Strategy Set selector/Save
control integrated directly into that section's header (see ui.
strategy_set_view). Renders raw Streamlit controls and returns their
current values as a plain ScanSetup -- it builds no StrategyDefinition/
ScanRequest itself; that translation belongs to ui.scan_view +
ui.formatting, unchanged regardless of whether a row was typed manually
or loaded from a saved Strategy Set.

Single Run Scan path (Task 1 simplification): there is exactly one way
to execute a scan -- the "▶ Run Scan" button at the bottom of Scan
Configuration, handled by ui.scan_view.handle_run_scan(). The separate
"Run '<Strategy Set>'" button that used to render above the grid (with
its own, independently-editable interval selector) is gone, along with
the global Market dropdown Scan Configuration used to carry -- a
Strategy Set's markets are exactly the markets its rows carry (the
grid's own per-row Market column), so there is nothing left for a
global Market selector to do. Scan Configuration's one Interval
selector (`interval` on ScanSetup) is the single runtime interval for
every leg of a scan -- see ui.formatting.apply_interval_override(),
applied by handle_run_scan() -- so a saved Strategy Set's own persisted
per-row interval (still shown/edited in the grid, still what gets
saved) can never silently conflict with what a scan actually runs at.

The grid's position-column headers are bare curve-position numbers,
not real contract codes: template_from_dense_weights() +
generate_instances() roll a position-relative shape across every
eligible starting point in the contract universe, so "position 1" is a
different real RIC for each rolled instance -- there is no single
fixed contract per column to show truthfully. See ui.formatting.
CURVE_POSITION_HELP for the caption that explains this once, rather
than repeating it per column.

Per-row Market/Interval (multi-market fix): the grid has its own
Market/Interval SelectboxColumns, defaulting new rows to a fixed
default market plus whatever the scan bar's own Interval selector
currently shows, but otherwise fully independent per row. This is what
lets a Strategy Set mixing markets (e.g. "Intermarket Churning": SOFR +
SONIA + CORRA entries) round-trip through load -> edit -> save ->
reload without any entry's market/interval silently changing -- see
ui.strategy_set_formatting's module docstring for the full rationale.
Task 1 simplification: the scan bar no longer has a Market selector at
all (removed -- see the module docstring's "Single Run Scan path"
note), and its Interval selector is no longer merely a per-row default/
fallback -- ui.scan_view.handle_run_scan() now forces every row's
Interval to it at RUN time via ui.formatting.apply_interval_override(),
after build_definitions_from_grid() has resolved each row's OWN,
persisted Market/Interval (Market is never overridden).

Render order (UI/UX redesign pass): the Strategy Workspace (this
section) now renders ABOVE Scan Configuration (_render_scan_bar()) in
render_scan_setup(), matching the spec's "what am I scanning, then how
should it be measured" hierarchy -- reversed from the original Module
6A order, where the scan bar rendered first specifically to hand this
section its Market/Interval defaults. Since the grid's own per-row
Market/Interval is what actually matters for correctness (see above),
_peek_current_interval() reads the scan bar's own Interval widget key
as it stood after the PREVIOUS rerun (Streamlit session_state persists
a widget's key across reruns) purely to seed a brand-new blank row's
default Interval cell -- a cosmetic seed value only, never
authoritative; a blank row's default Market is simply the first
configured market (see _render_strategy_templates()), since there is no
scan-bar Market widget left to peek. The Strategy Set selector's Save/+New/Delete controls
(ui.strategy_set_view.render_save_button()/render_new_button()/
render_delete_control()/process_save()) are split the same way: the
buttons render in this section's header (next to the selector), but
Save's actual persistence logic runs after the grid below it, since it
needs that rerun's just-edited grid_rows.

Universe (Module 7B UX correction): no longer a user-entered date
range. Oscill8 scans the CURRENTLY active contract curve, not an
arbitrary historical contract universe -- contract_start is always
today, contract_end today plus a fixed forward horizon (see
_default_universe_window()), computed automatically and shown as a
compact "Active contracts -- Automatic" indicator rather than editable
date inputs. "Today" is not an arbitrary date plugged in here: it is
exactly the boundary core.futures_calendar.generate_contracts() (the
same function every rolling scan already calls) uses to decide which
contract-months are still eligible -- passing it `today` as `start`
already excludes every already-elapsed month for that market's own
listing cycle, which is precisely what "first active contract" means
in a codebase with no separate expiry calendar (see CLAUDE.md). See
_first_active_contract() below, used only to surface that resolved
contract in the Universe indicator for transparency -- the scan
pipeline itself only ever needs the date window, never a specific RIC.

History (how much PRICE data feeds the analytics) is a completely
separate concept and stays user-editable, defaulting to the last six
months rather than three years (see _default_history_window()).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import futures_calendar
from core.config import MARKETS, BarInterval

from strategy_sets.repository import StrategySetRepository

from ui import state
from ui import strategy_import_view
from ui import strategy_set_state as ss_state
from ui import strategy_set_view
from ui.formatting import (
    CURVE_POSITION_HELP,
    CURVE_POSITION_HELP_COMPACT,
    HISTORY_HELP,
    INTERVAL_COLUMN,
    LABEL_COLUMN,
    MARKET_COLUMN,
    PERCENTILE_RANGE_HELP,
    PRIMARY_LOOKBACK_HELP,
    UNIVERSE_HELP,
    position_column,
)

_INTERVALS: tuple[BarInterval, ...] = (BarInterval.DAILY, BarInterval.HOURLY, BarInterval.FOUR_HOUR)
_LOOKBACK_OPTIONS: tuple[int, ...] = (20, 40, 60, 90, 120)

_DEFAULT_POSITIONS = 6
_MIN_POSITIONS = 2
_MAX_POSITIONS = 12

_DEFAULT_LOWER_PERCENTILE = 5
_DEFAULT_UPPER_PERCENTILE = 95

# How far forward the automatic active-contract Universe reaches --
# matches the old manual default's forward reach (~2 years), just no
# longer user-editable or backward-looking (see the module docstring).
_UNIVERSE_FORWARD_DAYS = 730

# Default History window: the last six months, not three years -- see
# the module docstring's "History" paragraph.
_HISTORY_LOOKBACK_DAYS = 182

_RUNTIME_INTERVAL_HELP = (
    "Applied to every strategy in the scan -- the grid's own per-row Interval (and a loaded "
    "Strategy Set's saved interval) stays what gets persisted, but this is the single runtime "
    "interval every leg actually prices and analyzes at."
)


@dataclass(frozen=True)
class ScanSetup:
    """Everything the scan panel and strategy grid currently hold, read
    live from widget state -- not yet validated or translated into
    backend objects. contract_start/contract_end are computed
    automatically (see _default_universe_window()), never user-entered.

    No market_key field: Scan Configuration has no global Market
    selector (removed -- see the module docstring's "Single Run Scan
    path" note). `interval` is the one runtime interval every leg of
    the scan is forced to via ui.formatting.apply_interval_override(),
    regardless of what the grid's own per-row Interval column (or a
    loaded Strategy Set's persisted interval) says.
    """

    interval: BarInterval
    contract_start: date
    contract_end: date
    price_start: date
    price_end: date
    lookbacks: tuple[int, ...]
    display_lookback: int | None
    lower_percentile: float
    upper_percentile: float
    grid_rows: list[dict]
    position_columns: tuple[str, ...]
    run_clicked: bool


def _clamp_session_value(key: str, valid_options: tuple, fallback) -> None:
    """Reset a widget's persisted session value if it's no longer among
    valid_options (e.g. the user deselected a lookback that was
    previously chosen as the primary lookback) -- avoids Streamlit
    raising on a selectbox whose stored value isn't in its options."""
    if st.session_state.get(key) not in valid_options:
        st.session_state[key] = fallback


def _default_universe_window(today: date) -> tuple[date, date]:
    """The automatically-determined active-contract Universe: from
    today (generate_contracts() already excludes any month before its
    start date, so "today" alone naturally drops already-elapsed
    contract months without needing an explicit expiry calendar) out to
    a fixed forward horizon. Pure and unit-testable -- no Streamlit."""
    return today, today + timedelta(days=_UNIVERSE_FORWARD_DAYS)


def _first_active_contract(market_key: str, today: date) -> str | None:
    """The actual first currently-active listed contract for
    `market_key`, per the SAME source of truth every rolling scan
    already uses: core.futures_calendar.generate_contracts(). Not a
    guess or a separately-maintained calendar -- this calls the real
    function with the real automatic Universe window and returns its
    first result, purely for display (the scan pipeline itself passes
    the date window straight through and never needs this RIC).
    Returns None if that market has no contract listed within the
    forward horizon (shouldn't happen for any market in core.config.
    MARKETS at today's practical horizon, but never raises either way).
    """
    start, end = _default_universe_window(today)
    contracts = futures_calendar.generate_contracts(market_key, start, end)
    return contracts[0] if contracts else None


def _default_history_window(today: date) -> tuple[date, date]:
    """The default History window: the last ~6 months up to today."""
    return today - timedelta(days=_HISTORY_LOOKBACK_DAYS), today


def render_scan_setup() -> ScanSetup:
    """Renders the Strategy Workspace (Strategy Set controls + grid)
    ABOVE Scan Configuration (Interval/Contracts/History/Analytics/Run
    Scan), per the UI/UX spec's workflow hierarchy ("what am I
    scanning?" before "how should it be measured?"). The Strategy
    Workspace no longer needs a completed scan-bar Interval selection to
    render first -- _render_strategy_templates() peeks the scan bar's
    own Interval widget key from the PREVIOUS rerun (see
    _peek_current_interval()) purely to seed a brand-new blank row's
    default Interval cell; every already-populated grid row keeps
    carrying its own Market/Interval regardless of render order.
    """
    main, _ = st.columns([5, 1])
    with main:
        st.subheader("Oscill8 — Range-Bound Scanner")

        with st.container(border=True):
            grid_rows, position_columns = _render_strategy_templates()

        with st.container(border=True):
            setup_values = _render_scan_bar()

    return ScanSetup(grid_rows=grid_rows, position_columns=position_columns, **setup_values)


def _peek_current_interval() -> BarInterval:
    """Best-effort read of the scan bar's Interval widget value as it
    stood after the PREVIOUS rerun (st.session_state persists a widget's
    key across reruns even before that widget is re-instantiated later
    in the current one) -- used only to seed a brand-new "+ New Strategy
    Set" blank row's default Interval cell. Falls back to DAILY on the
    very first render, before that widget key exists at all. Market has
    no equivalent peek: Scan Configuration has no global Market selector
    (see the module docstring), so a blank row's default Market is
    always simply the first configured market (see
    _render_strategy_templates())."""
    interval = st.session_state.get("oscill8_interval")
    if not isinstance(interval, BarInterval):
        interval = BarInterval.DAILY
    return interval


def _render_scan_bar() -> dict:
    st.caption("SCAN CONFIGURATION")
    today = date.today()
    universe_start, universe_end = _default_universe_window(today)
    history_start, history_end = _default_history_window(today)

    col_interval, col_universe, col_history, col_analytics = st.columns([1.1, 1.4, 1.8, 2.6])

    with col_interval:
        st.caption("INTERVAL", help=_RUNTIME_INTERVAL_HELP)
        interval = st.selectbox(
            "Interval", _INTERVALS, format_func=lambda i: i.value, key="oscill8_interval",
            help=_RUNTIME_INTERVAL_HELP,
        )

    with col_universe:
        st.caption("CONTRACTS", help=UNIVERSE_HELP)
        # A disabled text_input, not st.info: it renders with the exact
        # same label-row + control-row height as Interval/Price History
        # Start/Lookbacks (bars) in the other three columns, so this
        # row's boxes line up with theirs instead of an st.info banner's
        # own (taller, label-less) height sitting a row higher.
        st.text_input(
            "Active Contracts", value="📈 Automatic", disabled=True, key="oscill8_universe_display",
        )
        st.caption(f"{universe_start:%Y/%m/%d} → {universe_end:%Y/%m/%d}", help=UNIVERSE_HELP)

    with col_history:
        st.caption("HISTORY", help=HISTORY_HELP)
        price_start = st.date_input(
            "Price History Start", value=history_start, key="oscill8_price_start", help=HISTORY_HELP,
        )
        price_end = st.date_input(
            "Price History End", value=history_end, key="oscill8_price_end", help=HISTORY_HELP,
        )

    with col_analytics:
        st.caption("ANALYTICS")
        lookbacks = st.multiselect(
            "Lookbacks (bars)", _LOOKBACK_OPTIONS, default=list(_LOOKBACK_OPTIONS), key="oscill8_lookbacks"
        )
        lookbacks_sorted = tuple(sorted(set(lookbacks)))
        if lookbacks_sorted:
            _clamp_session_value("oscill8_display_lookback", lookbacks_sorted, lookbacks_sorted[0])
            display_lookback = st.selectbox(
                "Primary Lookback",
                lookbacks_sorted,
                format_func=lambda n: f"{n} bars",
                help=PRIMARY_LOOKBACK_HELP,
                key="oscill8_display_lookback",
            )
        else:
            st.warning("Select a lookback")
            display_lookback = None

        p1, p2 = st.columns(2)
        with p1:
            lower_percentile = st.number_input(
                "Lower %ile", min_value=0, max_value=100, value=_DEFAULT_LOWER_PERCENTILE, step=1,
                key="oscill8_lower_percentile", help=PERCENTILE_RANGE_HELP,
            )
        with p2:
            upper_percentile = st.number_input(
                "Upper %ile", min_value=0, max_value=100, value=_DEFAULT_UPPER_PERCENTILE, step=1,
                key="oscill8_upper_percentile", help=PERCENTILE_RANGE_HELP,
            )

    st.divider()
    status_col, button_col = st.columns([3, 1])
    with button_col:
        run_clicked = st.button("▶ Run Scan", type="primary", width="stretch")
    with status_col:
        # Do not invent progress percentages (spec section 12) -- the
        # spinner in ui.scan_view.handle_run_scan() already covers the
        # "⟳ SCANNING..." state during execution; this only reflects
        # that a completed scan result is currently on display.
        if not run_clicked and state.get_scan_report() is not None:
            st.caption("✓ Scan complete")

    return {
        "interval": interval,
        "contract_start": universe_start,
        "contract_end": universe_end,
        "price_start": price_start,
        "price_end": price_end,
        "lookbacks": lookbacks_sorted,
        "display_lookback": display_lookback,
        "lower_percentile": float(lower_percentile),
        "upper_percentile": float(upper_percentile),
        "run_clicked": run_clicked,
    }


def _render_strategy_templates() -> tuple[list[dict], tuple[str, ...]]:
    repo = StrategySetRepository()
    default_market_key = next(iter(MARKETS))
    default_interval = _peek_current_interval()

    st.subheader("Strategy Workspace")
    st.caption("STRATEGY SET")

    # ONE flat row (not columns-inside-a-column, which left the button
    # trio floating a half-row above the dropdown/Positions boxes) so
    # every control shares the same label-row + control-row baseline --
    # the selector's "Strategy Set" label, Positions' own "Positions"
    # label, and the small captions above Save/+ New/Delete all render
    # at the same height, and their boxes below therefore line up too.
    sel_col, save_col, new_col, delete_col, import_col, positions_col = st.columns(
        [2.6, 0.9, 0.9, 0.9, 1.0, 1.1], vertical_alignment="bottom"
    )
    with sel_col:
        selected_name = strategy_set_view.render_selector(repo)
    with save_col:
        st.caption("Save")
        save_clicked = strategy_set_view.render_save_button()
    with new_col:
        # Escaped leading "+": Streamlit captions render as Markdown,
        # where a bare "+ " at the start of a line is a bullet-list
        # marker -- without the escape this rendered as "• New".
        st.caption("\\+ New")
        strategy_set_view.render_new_button()
    with delete_col:
        st.caption("Delete")
        strategy_set_view.render_delete_control(repo, selected_name)
    with import_col:
        st.caption("Import")
        strategy_import_view.render_import_button()
    with positions_col:
        n_positions = st.number_input(
            "Positions",
            min_value=_MIN_POSITIONS,
            max_value=_MAX_POSITIONS,
            value=_DEFAULT_POSITIONS,
            step=1,
            key="oscill8_positions",
            help="How many curve positions to show. Changing this resets the grid below.",
        )

    # Import Strategies (additive, separate from every other control in
    # this row): renders its own panel (upload/preview/Import All) only
    # when opened -- see ui.strategy_import_view.
    strategy_import_view.render_import_panel(repo)

    message = ss_state.pop_message()
    if message is not None:
        level, text = message
        {"success": st.success, "error": st.error, "info": st.info}.get(level, st.info)(text)

    position_columns = tuple(position_column(i) for i in range(1, n_positions + 1))
    seed_df = strategy_set_view.resolve_grid_seed(
        selected_name, repo, position_columns, default_market_key, default_interval
    )

    st.caption("STRATEGY")
    grid_rows = _render_strategy_grid(seed_df, position_columns, selected_name, n_positions)
    strategy_set_view.process_save(
        repo, selected_name, save_clicked, grid_rows, position_columns,
        default_market_key, default_interval,
    )

    return grid_rows, position_columns


def _render_strategy_grid(
    seed_df: pd.DataFrame, position_columns: tuple[str, ...], selected_name: str | None, n_positions: int
) -> list[dict]:
    st.caption(CURVE_POSITION_HELP_COMPACT, help=CURVE_POSITION_HELP)

    column_config = {
        LABEL_COLUMN: st.column_config.TextColumn("Label", width="small"),
        MARKET_COLUMN: st.column_config.SelectboxColumn(
            "Market", options=list(MARKETS.keys()), width="small", required=True,
            help="Each row has its own market -- a set can mix markets (e.g. SOFR + SONIA + CORRA).",
        ),
        INTERVAL_COLUMN: st.column_config.SelectboxColumn(
            "Interval", options=[i.value for i in _INTERVALS], width="small", required=True,
        ),
    }
    for i, col in enumerate(position_columns, start=1):
        # TextColumn, not NumberColumn: verified empirically that this
        # Streamlit build renders an unpopulated/NaN NumberColumn cell as
        # the literal text "None" regardless of dtype, while a TextColumn
        # with an empty string renders correctly blank. The `validate`
        # regex still constrains committed input to a numeric-looking
        # pattern (optional sign, digits, optional decimal) or blank.
        column_config[col] = st.column_config.TextColumn(
            str(i), width="small", validate=r"^-?\d*\.?\d*$"
        )

    # Keyed by selection + position count so the widget's own cached
    # edit state resets whenever a different Strategy Set (or "+ New
    # Strategy Set") is loaded, or the position count changes --
    # otherwise Streamlit would keep showing a stale, previously-edited
    # grid instead of the newly-selected set's rows.
    editor_key = f"oscill8_template_grid_{selected_name or 'new'}_{n_positions}"
    column_order = [LABEL_COLUMN, MARKET_COLUMN, INTERVAL_COLUMN, *position_columns]
    edited = st.data_editor(
        seed_df,
        num_rows="dynamic",
        key=editor_key,
        column_config=column_config,
        column_order=column_order,
    )
    # Escaped leading "+" -- see the "+ New" caption above for why.
    st.caption("\\+ Add strategy", help="Use the + row at the bottom of the grid to add a new strategy.")
    return edited.to_dict("records")
