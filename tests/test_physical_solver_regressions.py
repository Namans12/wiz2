"""Targets generated from known feasible drives are an independent oracle."""
import numpy as np
import pytest

from src.core.bulb_physical_model import PhysicalPrimaries, solve_channel_mix, solve_channel_mix_batch
from src.core.color_mapping import BulbColorProfile


@pytest.mark.parametrize("white_channels", [1, 2])
def test_known_feasible_bounded_mixtures_reconstruct(white_channels):
    primaries = PhysicalPrimaries.from_profile(BulbColorProfile(white_channels=white_channels))
    n = primaries.xyz.shape[0]
    rng = np.random.default_rng(726)
    weights = np.vstack([np.zeros(n), np.ones(n), np.full(n, .8), rng.random((60, n))])
    targets = primaries.mix(weights)
    result = solve_channel_mix_batch(targets, primaries)
    assert result.shape == weights.shape
    assert np.all(result >= 0) and np.all(result <= 1)
    np.testing.assert_allclose(primaries.mix(result), targets, atol=1e-6)
    for target in targets[::9]:
        single = solve_channel_mix(target, primaries)
        np.testing.assert_allclose(primaries.mix(single), target, atol=1e-6)
