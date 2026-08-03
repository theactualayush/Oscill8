"""
mean_reversion.py

AR(1) mean-reversion fit on level changes, and the discrete-time
half-life derived from it.

Fitted form (differenced/OU-style, per design approval):
    Delta S_t = alpha + gamma * S_(t-1) + epsilon_t

This is algebraically equivalent to the raw-level form
    S_t = alpha + beta * S_(t-1) + epsilon_t
via beta = 1 + gamma (subtract S_(t-1) from both sides of the level
form to get the differenced form). gamma is what's actually fit by
OLS; beta is derived and reported because its sign/magnitude directly
answers "smooth vs. oscillatory vs. random-walk vs. explosive" without
requiring the reader to remember the +1 shift.

Half-life: the expected deviation from the long-run mean decays as
beta^t, so its *magnitude* decays as |beta|^t regardless of beta's
sign. Solving |beta|^t = 0.5 for t gives the discrete-time half-life:

    half_life = ln(2) / (-ln(|beta|))          for 0 < |beta| < 1

Valid region, in terms of beta:
    0 < beta < 1   : smooth mean reversion (deviation shrinks
                     monotonically, no sign flips) -- half-life finite.
    beta == 0      : degenerate/instant reversion (no dependence on
                     own lag at all) -- half_life = 0.0 by the same
                     limit (ln(2)/(-ln(0)) -> 0), computed directly
                     rather than through the general formula to avoid
                     a log(0) singularity.
    -1 < beta < 0  : oscillatory mean reversion (deviation magnitude
                     shrinks geometrically, but sign flips every bar)
                     -- half-life finite via |beta|; beta's negative
                     sign in the output is what tells a reader it's
                     oscillatory rather than smooth.
    beta == 1      : exact random walk -- half_life = NaN.
    |beta| >= 1     : non-mean-reverting -- trending/explosive
                     (beta > 1) or unstable growing oscillation
                     (beta <= -1) -- half_life = NaN in both cases.

No dependency beyond numpy/pandas (already project dependencies); no
statsmodels/scipy regression call is used since this is a single-
predictor OLS with edge cases (zero regressor variance, near-zero
degrees of freedom) that are simpler to reason about hand-rolled than
via a general-purpose regression routine's own edge-case handling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

_NAN = float("nan")


@dataclass(frozen=True)
class AR1Fit:
    """Result of fitting Delta S_t = alpha + gamma * S_(t-1) + epsilon_t.

    All fields are NaN when not computable -- see fit_ar1 for the exact
    conditions. No field here classifies the strategy; `beta`'s sign
    and magnitude, together with whether `half_life` is NaN, are what
    let a caller distinguish smooth reversion / oscillatory reversion /
    random walk / explosive dynamics without this module labeling any
    of them itself.
    """

    gamma: float
    beta: float
    std_error: float
    r_squared: float
    half_life: float


def _half_life_from_beta(beta: float) -> float:
    if beta == 0.0:
        return 0.0
    if abs(beta) >= 1.0:
        return _NAN
    return math.log(2.0) / (-math.log(abs(beta)))


def fit_ar1(series: pd.Series) -> AR1Fit:
    """Fit the AR(1) level-change regression and derive its half-life.

    Mathematical minimums (not reliability thresholds -- see design
    review): fitting `gamma`/`beta` at all needs >= 2
    (S_(t-1), Delta S_t) pairs, i.e. observation_count >= 3 (2 unknowns:
    alpha, gamma). `std_error`/`r_squared` additionally need >= 1
    residual degree of freedom, i.e. observation_count >= 4. Below
    each floor the corresponding fields are NaN -- these are exact
    algebraic requirements of OLS, not chosen cutoffs.

    A regressor (S_(t-1)) with zero variance makes the OLS slope
    undefined (0/0 in the normal equations) regardless of how many
    observations there are (e.g. a perfectly flat window) -- also NaN.

    `std_error` is the standard error of `gamma` (equivalently of
    `beta`, since beta = 1 + gamma is a constant shift). `r_squared` is
    NaN when the response (Delta S_t) has zero variance (a constant
    level-change series), since 1 - SSR/SST is then a 0/0.
    """
    values = series.to_numpy(dtype=float)
    n = len(values)

    if n < 3:
        return AR1Fit(gamma=_NAN, beta=_NAN, std_error=_NAN, r_squared=_NAN, half_life=_NAN)

    x = values[:-1]              # S_(t-1)
    y = values[1:] - values[:-1]  # Delta S_t
    n_pairs = len(x)

    x_mean = x.mean()
    y_mean = y.mean()
    sxx = float(((x - x_mean) ** 2).sum())

    if sxx == 0:
        return AR1Fit(gamma=_NAN, beta=_NAN, std_error=_NAN, r_squared=_NAN, half_life=_NAN)

    sxy = float(((x - x_mean) * (y - y_mean)).sum())
    gamma = sxy / sxx
    alpha = y_mean - gamma * x_mean
    beta = 1.0 + gamma

    if n_pairs < 3:
        std_error = _NAN
        r_squared = _NAN
    else:
        residuals = y - (alpha + gamma * x)
        ssr = float((residuals ** 2).sum())
        sst = float(((y - y_mean) ** 2).sum())
        dof = n_pairs - 2
        std_error = math.sqrt(ssr / dof / sxx)
        r_squared = 1.0 - ssr / sst if sst > 0 else _NAN

    half_life = _half_life_from_beta(beta)

    return AR1Fit(
        gamma=float(gamma),
        beta=float(beta),
        std_error=std_error,
        r_squared=r_squared,
        half_life=half_life,
    )
