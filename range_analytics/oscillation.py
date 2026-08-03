"""
oscillation.py

Crossing-count primitive for measuring oscillation around an
equilibrium level, with an optional hysteresis band so tick-level noise
around that level doesn't inflate the count.
"""

from __future__ import annotations

import pandas as pd


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

    state: str | None = None
    crossings = 0
    for value in series:
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
            crossings += 1
            state = zone

    return crossings
