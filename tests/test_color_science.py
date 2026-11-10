import numpy as np

from src.core.color_mapping import BulbColorProfile, BulbGamutMapper
from src.core.color_science import ColorSpace, HDRCurve, WideGamutFrameProcessor, compress_to_srgb, detect_hdr_content


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
