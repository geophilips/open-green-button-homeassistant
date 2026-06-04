"""Tests for the statistics helper — id format invariants + unit conversion.

The full async_add_external_statistics path is exercised by the coordinator-level tests
where a recorder is wired up; these focus on the pure-function bits that don't need HA.
"""

from __future__ import annotations

from custom_components.greenbutton.statistics import (
    statistic_id_for_series,
    statistic_id_prefix_for_entry,
)


def test_statistic_id_is_scoped_per_entry() -> None:
    """Two entries on the same utility point must produce distinct statistic IDs.

    This is the invariant that lets a tester swap a sandbox account for a real one without
    bleeding data between them in the Energy dashboard — see README Roadmap.
    """
    id_a = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    id_b = statistic_id_for_series("entry_b", "up_123", "FORWARD")
    assert id_a != id_b
    assert id_a.startswith("greenbutton:entry_a_")
    assert id_b.startswith("greenbutton:entry_b_")


def test_statistic_id_differentiates_flow_directions() -> None:
    """Same usage point, opposite flow direction → different statistic IDs.

    Solar PV would emit both FORWARD (consumption) and REVERSE (export) — they must be
    separate series in HA so the Energy dashboard can graph them separately.
    """
    forward = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    reverse = statistic_id_for_series("entry_a", "up_123", "REVERSE")
    assert forward != reverse


def test_statistic_id_lowercases_flow_direction() -> None:
    """Flow casing is normalized so a future server enum-rename doesn't shift the id."""
    upper = statistic_id_for_series("entry_a", "up_123", "FORWARD")
    lower = statistic_id_for_series("entry_a", "up_123", "forward")
    assert upper == lower


def test_statistic_id_prefix_matches_all_ids_for_an_entry() -> None:
    """The remove-entry purge filter must catch every id produced by `statistic_id_for_series`.

    The prefix is the load-bearing surface for async_remove_entry — if these drift the
    purge silently leaks orphan rows.
    """
    prefix = statistic_id_prefix_for_entry("entry_a")
    assert statistic_id_for_series("entry_a", "up_1", "FORWARD").startswith(prefix)
    assert statistic_id_for_series("entry_a", "up_2", "REVERSE").startswith(prefix)
    # …and crucially, *doesn't* catch a different entry's ids
    assert not statistic_id_for_series("entry_b", "up_1", "FORWARD").startswith(prefix)
