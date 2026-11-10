import numpy as np

from src.core.color_mapping import BulbColorProfile, BulbGamutMapper
from src.core.color_science import (ColorSpace, HDRCurve, WideGamutFrameProcessor,
                                    aces_tone_map, compress_to_srgb, detect_hdr_content, pq_decode)


def test_gamut_compression_preserves_in_gamut_highlights_and_white():
    colors = np.array([[0.9, 0.83, 0.99], [1.0, 1.0, 1.0], [0.0, 0.3, 0.7]], dtype=np.float32)
    np.testing.assert_allclose(compress_to_srgb(colors), colors, rtol=0, atol=1e-7)


def test_gamut_compression_keeps_out_of_gamut_values_bounded():
    colors = np.array([[-0.2, 1.2, 0.3], [1.5, 0.2, 0.4]], dtype=np.float32)
    mapped = compress_to_srgb(colors)
    assert np.all((mapped >= 0) & (mapped <= 1))


def test_gamut_compression_is_continuous_near_the_gamut_boundary():
    colors = np.array([[1.0, 0.9, 0.4], [1.0001, 0.9, 0.4]], dtype=np.float32)
    mapped = compress_to_srgb(colors)
    assert np.max(np.abs(mapped[1] - mapped[0])) < 0.001


def test_display_p3_neutral_white_stays_white():
    processor = WideGamutFrameProcessor(source_color_space=ColorSpace.DISPLAY_P3)
    assert processor.process_single((255, 255, 255)) == (255, 255, 255)


def test_display_p3_processor_remaps_saturated_green_into_srgb_gamut():
    processor = WideGamutFrameProcessor(source_color_space=ColorSpace.DISPLAY_P3)

    mapped = processor.process_single((0, 255, 0))

    assert mapped[1] >= 254
    assert 0 <= mapped[0] <= 80
    assert 0 <= mapped[2] <= 80


def test_hdr_processor_tone_maps_bright_signal():
    processor = WideGamutFrameProcessor(
        source_color_space=ColorSpace.REC2020,
        hdr_curve=HDRCurve.PQ,
        peak_nits=2000.0,
    )

    mapped = processor.process_single((210, 180, 90))

    assert all(0 <= channel <= 255 for channel in mapped)
    assert max(mapped) > 0


def test_pq_tone_mapping_uses_absolute_10000_nit_reference():
    # ST 2084 code value 0.508078 corresponds to approximately 100 nits.
    linear = pq_decode(np.array([0.508078], dtype=np.float64))
    np.testing.assert_allclose(linear[0], 0.01, atol=2e-7)
    result = aces_tone_map(linear, peak_nits=1000, target_nits=200)
    # 100 nits / 200 nits = 0.5 before the ACES fit.
    expected = (0.5 * (2.51 * 0.5 + 0.03)) / (0.5 * (2.43 * 0.5 + 0.59) + 0.14)
    np.testing.assert_allclose(result, [expected], atol=1e-4)
    np.testing.assert_allclose(result, aces_tone_map(linear, peak_nits=4000, target_nits=200))


def test_pq_processor_keeps_reference_white_visible():
    processor = WideGamutFrameProcessor(source_color_space=ColorSpace.REC2020,
                                        hdr_curve=HDRCurve.PQ, peak_nits=1000,
                                        target_nits=200)
    mapped = processor.process_single((130, 130, 130))
    assert all(195 <= channel <= 215 for channel in mapped)


def test_hlg_aces_scaling_remains_relative_to_its_peak():
    processor = WideGamutFrameProcessor(source_color_space=ColorSpace.REC2020,
                                        hdr_curve=HDRCurve.HLG, peak_nits=1000,
                                        target_nits=200)
    relative = np.array([0.05, 0.1, 0.2], dtype=np.float32)
    expected = aces_tone_map(relative * (1000 / 10000), target_nits=200)
    np.testing.assert_allclose(processor._tone_map(relative), expected)


def test_bulb_mapper_source_color_space_changes_payload_for_wide_gamut_input():
    srgb_mapper = BulbGamutMapper(BulbColorProfile(source_color_space="srgb"))
    p3_mapper = BulbGamutMapper(BulbColorProfile(source_color_space="display-p3"))

    srgb_payload = srgb_mapper.map_rgb((0, 255, 0))
    p3_payload = p3_mapper.map_rgb((0, 255, 0))

    assert srgb_payload != p3_payload
    assert abs(p3_payload[1] - srgb_payload[1]) <= 2


def test_hdr_detection_returns_stable_report_shape():
    frame = np.zeros((40, 40, 3), dtype=np.uint8)
    frame[0:5, 0:5] = (255, 250, 240)

    report = detect_hdr_content(frame)

    assert set(report) >= {
        "hdr_likely",
        "wide_gamut_likely",
        "mean_luma",
        "peak_luma",
        "pq_hint",
    }
