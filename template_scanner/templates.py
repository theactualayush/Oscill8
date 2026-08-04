"""
templates.py

Translates a grid-style dense weight vector (one weight per consecutive
contract-curve position, zero meaning "gap") into a strategy_engine.
StrategyDefinition -- the shape strategy_engine already fully supports
(arbitrary strictly-increasing offsets, arbitrary weights, single-leg
outrights through arbitrary multi-leg structures). This is a pure
format conversion, not a new engine: StrategyDefinition's own
validation (offsets/weights length match, not all zero, valid
interval/price_field) is reused as-is, never duplicated here.

SCOPE: this module builds SAME-MARKET templates only -- one market_key
per template, translated directly into StrategyDefinition and executed
through strategy_engine.generate_instances() unchanged. This is
deliberately the only executable template shape today. A future
intermarket template (legs spanning multiple markets) would need a new
per-leg representation and a new contract-alignment algorithm entirely
-- see the Module 5 design review for why that is a materially
different combinatorial problem, not a parameter change to this
function. template_from_dense_weights() is scoped to same-market
templates by name and by its single market_key parameter; a future
intermarket path is expected to be an ADDITIVE sibling in this
package (a new translation function producing whatever multi-market
execution shape strategy_engine eventually supports), not a
modification of this function or of the same-market candidate
generation in universe.py.
"""

from __future__ import annotations

from typing import Sequence

from core.config import BarInterval

from strategy_engine.definitions import StrategyDefinition


def template_from_dense_weights(
    market_key: str,
    dense_weights: Sequence[float],
    interval: BarInterval,
    price_field: str = "Close",
) -> StrategyDefinition:
    """Translate a dense, grid-style weight vector into a StrategyDefinition.

    `dense_weights` is one weight per consecutive contract-curve
    position (e.g. grid columns 1..12), with 0 meaning "no leg at this
    position" -- a gap. Only the nonzero positions become legs; the
    resulting StrategyDefinition never fetches or requires a contract
    at a zero-weight position, since it contributes nothing to the
    weighted sum regardless of its own price.

    Leading and trailing zero entries are harmless no-ops: they shift
    which nominal grid column the shape's first/last active leg sits
    in, but never change which contracts get selected during rolling
    or how the shape's Strategy value is computed. Only the *relative*
    spacing between nonzero positions matters.

    Examples:
        (1, -1)              -> offsets=(0, 1),       weights=(1, -1)
        (1, -2, 1)            -> offsets=(0, 1, 2),     weights=(1, -2, 1)
        (1, -3, 3, -1)         -> offsets=(0, 1, 2, 3),   weights=(1, -3, 3, -1)
        (1, 0, -2, 0, 1)       -> offsets=(0, 2, 4),      weights=(1, -2, 1)
        (2, 0, -1, 0, -1)      -> offsets=(0, 2, 4),      weights=(2, -1, -1)
        (0, 1, -2, 1)          -> offsets=(0, 1, 2),      weights=(1, -2, 1)
            (the leading zero is re-based away)

    All remaining validation (offsets strictly increasing once
    re-based, weights/offsets length match, unsupported price_field,
    unknown market) is StrategyDefinition's own -- not duplicated here.

    Raises:
        ValueError: if `dense_weights` has no nonzero entries (a
            template with no legs at all -- StrategyDefinition itself
            would reject an all-zero weights tuple with the same
            message, but there are no nonzero entries here even to
            find a re-basing offset from, so this is checked first
            with a template-specific message).
    """
    nonzero = [(i, w) for i, w in enumerate(dense_weights) if w != 0]
    if not nonzero:
        raise ValueError("Template has no nonzero weights")

    base = nonzero[0][0]
    offsets = tuple(i - base for i, _ in nonzero)
    weights = tuple(float(w) for _, w in nonzero)

    return StrategyDefinition(
        market_key=market_key,
        offsets=offsets,
        weights=weights,
        interval=interval,
        price_field=price_field,
    )
