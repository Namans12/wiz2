import pytest

from scripts.sync_test_pattern import PATTERNS, pattern_at


def test_pattern_sequence_and_cycle_use_elapsed_time():
    assert pattern_at(0, 2)[1] == "Black"
    assert pattern_at(1.99, 2)[1] == "Black"
    assert pattern_at(2, 2)[1] == "White"
    assert pattern_at(len(PATTERNS) * 2, 2)[1] == "Black"
    with pytest.raises(ValueError):
        pattern_at(1, 0)
