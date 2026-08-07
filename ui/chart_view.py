"""
chart_view.py

Module 6B v1: the Selected Strategy History chart.

Sources the raw price series via strategy_engine.pricing.build_history()
for the SELECTED candidate's already-known StrategyInstance and the
ORIGINAL scan's price_start/price_end. That call goes through
database.get_history(), which is cache-first (Module 2) -- since the
exact same RIC(s)/interval/price window were already fetched during the
scan just run, this is a SQLite cache hit, never a fresh LSEG request.
The fetched history is cached in session state (ui.state) per selection
so it is fetched at most once per row click, not on every rerun.

The chart-horizon overlay statistics (robust low/median/high, and
everything the Selected Strategy panel shows) are never recomputed here
-- they're read directly from the candidate's already-computed
MultiLookbackAnalytics.per_lookback via template_scanner.metrics.
at_lookback(), exactly as the result table and Selected Strategy panel
already do. Switching the chart-horizon selector only re-reads a
different already-computed RangeAnalytics and re-slices the
already-cached history -- no new backend call of any kind.

The plotted window itself is sliced with range_analytics.lookback.
resolve_window() -- the SAME function Module 4A uses internally to
resolve a lookback -- so the plotted observations are always exactly
the ones the displayed overlay levels were computed from.

Trading-day chart axis (data-integrity phase): `window["Date"]` never
contains a weekend/holiday row to begin with (see the pipeline-wide
"valid observations, not calendar days" invariant documented in
strategy_engine.pricing and range_analytics.lookback) -- so the line
trace itself already connects Friday directly to Monday. What Plotly's
default continuous date axis gets wrong on its own is spacing: it
reserves real calendar width for the non-trading days in between,
which reads visually as a gap even though no data is missing. `_build_
rangebreaks()` collapses that unused axis space via Plotly
`rangebreaks` -- weekends via the fixed `bounds=["sat", "mon"]` rule,
and any OTHER missing weekday (a holiday, or any date this particular
strategy's own valid-observation series happens to lack) derived
dynamically from `window["Date"]` itself, never from a maintained
per-market holiday calendar. This is deliberate: it makes the fix
automatically correct for every current market and for a future
intermarket strategy's own combined valid-date set, since it only ever
reads off the already-correct series, the same source of truth the
rest of the pipeline uses. DAILY only -- see _build_rangebreaks for why
HOURLY/4H intraday gaps are explicitly out of scope here.
"""

from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from range_analytics.lookback import resolve_window
from range_analytics.results import RangeAnalytics

from strategy_engine.pricing import build_history

from template_scanner.metrics import at_lookback as metrics_at_lookback
from template_scanner.scan_results import ScanCandidateResult
from template_scanner.scanner import ScanRequest

from ui import state
from ui.formatting import format_percentile_range

_LINE_COLOR = "#4C9AFF"
_MARKER_COLOR = "#FF6B6B"
_BAND_COLOR = "#4C9AFF"
_LEVEL_COLOR = "#8B93A7"
_MEAN_COLOR = "#5A6274"
_CHART_HEIGHT = 420


def get_selected_history(candidate: ScanCandidateResult, scan_request: ScanRequest) -> pd.DataFrame:
    """The selected candidate's full raw Strategy price series, cached
    in session state per selection -- see ui.state.set_selected_candidate,
    which invalidates this cache only when the selection actually
    changes identity.
    """
    cached = state.get_cached_history()
    if cached is not None:
        return cached
    history = build_history(candidate.instance, scan_request.price_start, scan_request.price_end).history
    state.cache_history(history)
    return history


def _missing_weekdays(dates: pd.Series) -> list[pd.Timestamp]:
    """Weekdays (Mon-Fri) within [dates.min(), dates.max()] that have no
    observation in `dates` -- i.e. holidays or any other date this
    strategy's own valid-observation series lacks. Deliberately excludes
    Saturday/Sunday: weekends are already collapsed by the fixed
    `bounds=["sat", "mon"]` rangebreak in _build_rangebreaks, and listing
    them again here would be redundant.

    `dates` is normalized to midnight before comparison (`.normalize()`)
    so a DAILY timestamp's time-of-day component -- which need not be
    identical across rows -- can never cause a real trading date to be
    misclassified as missing.

    Returns [] for an empty input.
    """
    if dates.empty:
        return []
    normalized = pd.DatetimeIndex(dates).normalize()
    full_range = pd.date_range(normalized.min(), normalized.max(), freq="D")
    present = set(normalized)
    return [d for d in full_range if d.weekday() < 5 and d not in present]


def _build_rangebreaks(dates: pd.Series) -> list[dict]:
    """Plotly xaxis `rangebreaks` that remove non-trading calendar days
    from the chart's horizontal space without any static holiday
    calendar: weekends via `bounds=["sat", "mon"]` (always present,
    covers every market identically), plus a `values` entry for any
    missing weekday actually found in `dates` (see _missing_weekdays) --
    omitted entirely when there is none, so a purely weekend-only gap
    never gets a redundant empty/explicit weekend `values` list.

    Chronological date labels are unaffected -- rangebreaks only close
    up unused axis space, they never relabel, reorder, or drop a plotted
    point.
    """
    breaks: list[dict] = [dict(bounds=["sat", "mon"])]
    missing = _missing_weekdays(dates)
    if missing:
        breaks.append(dict(values=missing))
    return breaks


def build_strategy_chart(history: pd.DataFrame, lookback: int, analytics: RangeAnalytics) -> go.Figure:
    """Pure figure builder: the plotted window (resolve_window) and the
    overlay levels (analytics, already computed for this exact
    lookback) are guaranteed to describe the same set of observations.
    """
    window = resolve_window(history, lookback=lookback)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=window["Date"],
            y=window["Strategy"],
            mode="lines",
            line=dict(color=_LINE_COLOR, width=1.5),
            name="Strategy",
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y:.4f}<extra></extra>",
        )
    )

    if not window.empty:
        last = window.iloc[-1]
        fig.add_trace(
            go.Scatter(
                x=[last["Date"]],
                y=[last["Strategy"]],
                mode="markers",
                marker=dict(color=_MARKER_COLOR, size=8),
                name="Current",
                hovertemplate="Current<br>%{x|%Y-%m-%d %H:%M}<br>%{y:.4f}<extra></extra>",
            )
        )

    low, median, high = analytics.range_low_robust, analytics.median, analytics.range_high_robust
    if not (math.isnan(low) or math.isnan(high)):
        fig.add_hrect(y0=low, y1=high, fillcolor=_BAND_COLOR, opacity=0.12, line_width=0)
    for value, label, dash in ((low, "Robust Low", "dot"), (median, "Median", "dash"), (high, "Robust High", "dot")):
        if not math.isnan(value):
            fig.add_hline(
                y=value,
                line=dict(color=_LEVEL_COLOR, width=1, dash=dash),
                annotation_text=label,
                annotation_position="right",
                annotation_font_size=11,
            )

    # Mean: a subtle reference line only -- thinner, more transparent, and
    # distinct (dashdot, left-side label) from the Low/Median/High trio
    # above so it reads as a secondary reference, not a fourth band edge.
    if not math.isnan(analytics.mean):
        fig.add_hline(
            y=analytics.mean,
            line=dict(color=_MEAN_COLOR, width=1, dash="dashdot"),
            opacity=0.6,
            annotation_text="Mean",
            annotation_position="left",
            annotation_font_size=10,
        )

    percentile_label = format_percentile_range(analytics.lower_percentile, analytics.upper_percentile)
    fig.update_layout(
        template="plotly_dark",
        title=f"Selected Strategy History — {lookback} bars · {percentile_label}",
        showlegend=False,
        height=_CHART_HEIGHT,
        margin=dict(l=40, r=110, t=50, b=30),
        yaxis_title="Strategy Value",
        hovermode="x unified",
    )
    fig.update_xaxes(rangebreaks=_build_rangebreaks(window["Date"]))
    return fig


def render_chart(display_lookback: int, scan_request: ScanRequest | None) -> None:
    candidate = state.get_selected_candidate()
    if candidate is None or scan_request is None:
        return

    lookbacks = scan_request.lookbacks
    default_lookback = display_lookback if display_lookback in lookbacks else lookbacks[0]
    chart_lookback = st.segmented_control(
        "Chart Horizon",
        options=lookbacks,
        default=default_lookback,
        format_func=lambda n: f"{n} bars",
        key="oscill8_chart_lookback",
    ) or default_lookback

    history = get_selected_history(candidate, scan_request)
    analytics = metrics_at_lookback(candidate.multi_lookback, chart_lookback)

    fig = build_strategy_chart(history, chart_lookback, analytics)
    st.plotly_chart(fig, config={"displayModeBar": False})
