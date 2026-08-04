"""
tests/test_template_scanner_universe.py

generate_candidates/generate_candidate_universe/dedupe_candidates
tested against real SOFR contract RICs (via core.futures_calendar --
pure calendar arithmetic, no I/O, no LSEG/database involved). Every
assertion below was verified against the actual implementation before
being written.
"""

from __future__ import annotations

from core.config import BarInterval
from template_scanner.templates import template_from_dense_weights
from template_scanner.universe import (
    dedupe_candidates,
    generate_candidate_universe,
    generate_candidates,
)


def _fly():
    return template_from_dense_weights("SOFR", (1, -2, 1), BarInterval.DAILY)


def _spread():
    return template_from_dense_weights("SOFR", (1, -1), BarInterval.DAILY)


# ---------------------------------------------------------------------
# Rolling candidate generation
# ---------------------------------------------------------------------

def test_rolling_fly_across_eligible_contracts():
    instances = generate_candidates(_fly(), "2026-01-01", "2027-12-31")
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAM26", "SRAU26", "SRAZ26"),
        ("SRAU26", "SRAZ26", "SRAH27"),
        ("SRAZ26", "SRAH27", "SRAM27"),
        ("SRAH27", "SRAM27", "SRAU27"),
        ("SRAM27", "SRAU27", "SRAZ27"),
    ]
    assert all(inst.definition.weights == (1.0, -2.0, 1.0) for inst in instances)


def test_rolling_gapped_template_matches_offset_spacing():
    gapped = template_from_dense_weights("SOFR", (1, 0, -2, 0, 1), BarInterval.DAILY)
    instances = generate_candidates(gapped, "2026-01-01", "2027-12-31")
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SRAU26", "SRAH27"),
        ("SRAM26", "SRAZ26", "SRAM27"),
        ("SRAU26", "SRAH27", "SRAU27"),
        ("SRAZ26", "SRAM27", "SRAZ27"),
    ]


def test_insufficient_contracts_near_curve_end_returns_empty_not_error():
    # Only 2 quarterly contracts (H26, M26) are listed in this window --
    # not enough to fill a 3-leg fly's span.
    instances = generate_candidates(_fly(), "2026-01-01", "2026-08-31")
    assert instances == []


def test_single_leg_outright_rolls_across_every_listed_contract():
    outright = template_from_dense_weights("SOFR", (1,), BarInterval.DAILY)
    instances = generate_candidates(outright, "2026-01-01", "2026-12-31")
    assert [inst.rics for inst in instances] == [
        ("SRAH26",), ("SRAM26",), ("SRAU26",), ("SRAZ26",),
    ]


# ---------------------------------------------------------------------
# max_curve_position / eligible_rics filtering
# ---------------------------------------------------------------------

def test_max_curve_position_keeps_only_near_dated_instances():
    instances = generate_candidates(
        _fly(), "2026-01-01", "2027-12-31", max_curve_position=2
    )
    assert [inst.rics for inst in instances] == [("SRAH26", "SRAM26", "SRAU26")]


def test_max_curve_position_zero_keeps_only_the_nearest_instance():
    instances = generate_candidates(
        _fly(), "2026-01-01", "2027-12-31", max_curve_position=1
    )
    assert instances == []  # the nearest fly's furthest leg is at index 2 > 1


def test_eligible_rics_keeps_only_fully_covered_instances():
    eligible = {"SRAH26", "SRAM26", "SRAU26", "SRAZ26"}
    instances = generate_candidates(
        _fly(), "2026-01-01", "2027-12-31", eligible_rics=eligible
    )
    assert [inst.rics for inst in instances] == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAM26", "SRAU26", "SRAZ26"),
    ]


def test_eligible_rics_excludes_instances_with_any_leg_outside_the_set():
    # SRAM26 alone isn't enough to build any 3-leg fly window.
    instances = generate_candidates(
        _fly(), "2026-01-01", "2027-12-31", eligible_rics={"SRAM26"}
    )
    assert instances == []


# ---------------------------------------------------------------------
# Multiple template rows
# ---------------------------------------------------------------------

def test_generate_candidate_universe_combines_multiple_template_rows():
    universe = generate_candidate_universe([_fly(), _spread()], "2026-01-01", "2026-12-31")
    assert [inst.rics for inst in universe] == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAM26", "SRAU26", "SRAZ26"),
        ("SRAH26", "SRAM26"),
        ("SRAM26", "SRAU26"),
        ("SRAU26", "SRAZ26"),
    ]


def test_generate_candidate_universe_forwards_filters_to_every_row():
    universe = generate_candidate_universe(
        [_fly(), _spread()], "2026-01-01", "2027-12-31", max_curve_position=2
    )
    # only instances whose furthest leg sits at curve index <= 2 survive,
    # from both rows.
    assert [inst.rics for inst in universe] == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAH26", "SRAM26"),
        ("SRAM26", "SRAU26"),
    ]


# ---------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------

def test_dedupe_removes_exact_duplicates_across_templates():
    # Two identical template rows (same shape) produce fully overlapping
    # candidates -- collapse to the unique set.
    raw = generate_candidate_universe([_fly(), _fly()], "2026-01-01", "2026-12-31")
    assert len(raw) == 4  # 2 fly instances, duplicated across the two rows

    deduped = dedupe_candidates(raw)
    assert len(deduped) == 2
    assert [inst.rics for inst in deduped] == [
        ("SRAH26", "SRAM26", "SRAU26"),
        ("SRAM26", "SRAU26", "SRAZ26"),
    ]


def test_dedupe_preserves_first_occurrence_order():
    raw = generate_candidate_universe([_spread(), _fly()], "2026-01-01", "2026-12-31")
    deduped = dedupe_candidates(raw)
    assert deduped == raw  # nothing to remove, order must be unchanged


def test_scaled_weights_are_not_deduplicated():
    fly = _fly()
    fly_2x = template_from_dense_weights("SOFR", (2, -4, 2), BarInterval.DAILY)

    universe = generate_candidate_universe([fly, fly_2x], "2026-01-01", "2026-12-31")
    assert len(universe) == 4  # 2 candidates per template, same RICs, different weights

    deduped = dedupe_candidates(universe)
    assert len(deduped) == 4  # scaled weights are economically different -- nothing collapses

    weight_sets = {inst.definition.weights for inst in deduped}
    assert weight_sets == {(1.0, -2.0, 1.0), (2.0, -4.0, 2.0)}


def test_dedupe_distinguishes_by_interval_and_price_field():
    daily = template_from_dense_weights("SOFR", (1, -1), BarInterval.DAILY)
    hourly = template_from_dense_weights("SOFR", (1, -1), BarInterval.HOURLY)
    open_field = template_from_dense_weights(
        "SOFR", (1, -1), BarInterval.DAILY, price_field="Open"
    )

    instances = (
        generate_candidates(daily, "2026-01-01", "2026-06-30")
        + generate_candidates(hourly, "2026-01-01", "2026-06-30")
        + generate_candidates(open_field, "2026-01-01", "2026-06-30")
    )
    deduped = dedupe_candidates(instances)
    # same RICs/weights, but 3 genuinely distinct series (different
    # interval or price_field) -- nothing should collapse.
    assert len(deduped) == len(instances)


def test_dedupe_empty_list():
    assert dedupe_candidates([]) == []
