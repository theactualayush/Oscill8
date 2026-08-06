"""
oscillation.py

Zone-transition primitives for measuring oscillation around/between
price levels. Two canonical metrics share one state-machine core
(_count_zone_transitions):

- count_crossings: directional crossings of a single equilibrium level,
  with an optional symmetric hysteresis band so tick-level noise around
  that level doesn't inflate the count. A "crossing" here is a HALF
  traversal (e.g. median -> top, or top -> median -> bottom) -- it does
  NOT require returning to the side it started from.
- count_oscillations: completed Top<->Bottom traversal-and-return
  cycles between two independent, caller-supplied boundaries (the
  window's configured Robust Low/Robust High) -- a materially different,
  trader-defined concept ("one complete oscillation" = reach one
  boundary, then the opposite boundary, then return to the original
  boundary). Never counts a lone half-traversal, and ignores any number
  of intermediate/"middle" observations between boundary touches.

Neither function replaces the other; both remain first-class, separate
canonical metrics (see range_analytics.results.RangeAnalytics).
"""

from __future__ import annotations

import pandas as pd


def _count_zone_transitions(series: pd.Series, lower: float, upper: float, *, inclusive: bool) -> int:
    """Shared state machine: classify each observation into "above"
    `upper`, "below" `lower`, or "inside" (neutral), then count
    confirmed side-to-side transitions -- a run of "inside" or
    same-side observations in between changes nothing, and the first
    non-"inside" observation only establishes the starting side (never
    itself counted as a transition).

    `inclusive` controls whether touching a boundary exactly counts as
    reaching it:
        inclusive=False: value must be STRICTLY beyond the edge to
            register a side (value == upper or value == lower is
            "inside") -- count_crossings' hysteresis-band convention,
            so tick-level noise sitting exactly on a band edge never
            itself starts/ends/counts as part of a crossing.
        inclusive=True: value >= upper is "above" (Top reached),
            value <= lower is "below" (Bottom reached) -- count_
            oscillations' boundary-touch convention, where touching a
            Robust Low/Robust High boundary counts as reaching it.

    Args:
        series: chronologically ordered values, NaN-free.
        lower: the lower boundary.
        upper: the upper boundary (must be >= lower).

    Returns:
        Number of confirmed side-to-side transitions. 0 for an empty
        series.
    """
    state: str | None = None
    transitions = 0
    for value in series:
        if inclusive:
            if value >= upper:
                zone = "above"
            elif value <= lower:
                zone = "below"
            else:
                zone = "inside"
        else:
            if value > upper:
                zone = "above"
            elif value < lower:
                zone = "below"
            else:
                zone = "inside"

        if zone == "inside":
            continue
        if state is None:
            state = zone
        elif zone != state:
            transitions += 1
            state = zone

    return transitions


def count_crossings(series: pd.Series, equilibrium: float, threshold: float = 0.0) -> int:
    """Count directional crossings of `equilibrium`, with an optional
    symmetric hysteresis band of half-width `threshold`.

    The band [equilibrium - threshold, equilibrium + threshold] is a
    single INCLUSIVE, neutral zone: any observation with
    lower <= value <= upper (band edges included) is classified
    "inside" and never itself establishes a side or counts as part of
    a crossing. An observation only registers a side -- "above" the
    upper edge or "below" the lower edge -- when it is STRICTLY beyond
    that edge. A crossing is counted only when the series has
    previously been confirmed on one side and a later observation is
    confirmed strictly on the other side; any run of "inside" or
    same-side observations in between changes nothing.

    threshold=0.0 (the default) collapses the band to the single point
    `equilibrium`: a value exactly equal to it is "inside" (neutral),
    and values strictly above/below split at that point. This makes
    threshold=0.0 the natural, parameter-free "raw" equilibrium-
    crossing count -- the same primitive, not a separate
    implementation, so raw and hysteresis-based counts are always
    directly comparable.

    Boundary semantics are deterministic and independent of any > vs
    >= choice elsewhere in the codebase: a value sitting exactly on
    `equilibrium`, or exactly on `equilibrium + threshold` /
    `equilibrium - threshold`, is always "inside" -- it never itself
    starts, ends, or counts as part of a crossing. Only a strict
    excursion beyond the band registers a side. Concretely:
        - below -> equilibrium -> above counts as 1 crossing.
        - a run of repeated observations exactly at equilibrium never
          crosses, and never resets an already-established side.
        - touching (not exceeding) either edge of a hysteresis band
          -- including sitting exactly on it -- stays "inside" and
          does not register a new side.

    The first non-"inside" observation only establishes the starting
    side; it is never itself counted as a crossing (there is no prior
    side to have crossed from).

    Args:
        series: chronologically ordered strategy values, NaN-free.
        equilibrium: the level to measure oscillation around.
        threshold: half-width of the neutral band around equilibrium.
            Must be >= 0. 0.0 (default) means no hysteresis.

    Returns:
        Number of confirmed side-to-side transitions. 0 for an empty
        series.

    Raises:
        ValueError: threshold < 0, or series contains NaN.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")
    if series.isna().any():
        raise ValueError("count_crossings does not accept NaN values")

    upper = equilibrium + threshold
    lower = equilibrium - threshold
    return _count_zone_transitions(series, lower, upper, inclusive=False)


def count_oscillations(series: pd.Series, lower: float, upper: float) -> int:
    """Count completed Top<->Bottom traversal-and-return cycles between
    two fixed boundaries (a window's Robust Low/Robust High).

    Trader definition: one complete oscillation occurs when price
    reaches one boundary, subsequently reaches the opposite boundary,
    and subsequently returns to the original boundary --
    Top -> Bottom -> Top = 1, Bottom -> Top -> Bottom = 1. Any number
    of "middle" observations (strictly between `lower` and `upper`) may
    occur between boundary touches and do not reset or modify the last
    confirmed boundary side. A lone half-traversal (Top -> Bottom with
    no return) is 0 completed oscillations, not 1.

    Touching a boundary exactly counts as reaching it: value >= `upper`
    is "Top reached", value <= `lower` is "Bottom reached" -- unlike
    count_crossings' hysteresis-band convention, where touching an edge
    is neutral. This function shares count_crossings' underlying
    zone-transition state machine (_count_zone_transitions) rather than
    reimplementing traversal logic, parameterized with
    inclusive=True instead.

    `lower`/`upper` must be computed ONCE from the resolved analysis
    window (e.g. RangeAnalytics.range_low_robust/range_high_robust) and
    held fixed while walking the window chronologically -- never
    recalculated at every historical observation.

    Since there are only ever two possible confirmed sides ("above"/
    "below"), every 2 consecutive confirmed side-transitions
    necessarily returns to the side the series started on -- so the
    completed-oscillation count is exactly the number of confirmed
    zone transitions, floor-divided by 2:
    TOP -> BOTTOM = 1 transition = 0 oscillations.
    TOP -> BOTTOM -> TOP = 2 transitions = 1 oscillation.
    TOP -> BOTTOM -> TOP -> BOTTOM = 3 transitions = still 1 oscillation.
    TOP -> BOTTOM -> TOP -> BOTTOM -> TOP = 4 transitions = 2 oscillations.

    A zero-width range (`lower == upper`, a completely flat window)
    returns 0 directly -- a flat structure has completed zero
    meaningful range traversals; this is a genuine, well-defined 0,
    never NaN, so it is special-cased ahead of the general state
    machine rather than left to fall out of it.

    Args:
        series: chronologically ordered strategy values, NaN-free.
        lower: the fixed lower boundary (e.g. range_low_robust).
        upper: the fixed upper boundary (e.g. range_high_robust). Must
            be >= lower.

    Returns:
        Number of completed oscillations. 0 for an empty series or a
        zero-width range.

    Raises:
        ValueError: lower > upper, or series contains NaN.
    """
    if series.isna().any():
        raise ValueError("count_oscillations does not accept NaN values")
    if lower > upper:
        raise ValueError(f"lower must be <= upper, got lower={lower}, upper={upper}")

    if lower == upper:
        return 0

    transitions = _count_zone_transitions(series, lower, upper, inclusive=True)
    return transitions // 2
