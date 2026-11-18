from concurrent.futures import TimeoutError as FutureTimeoutError

import numpy as np

from scripts.calibrate_bulb_camera import run_bulb_command
from scripts.calibrate_bulb_camera import camera_feed_has_signal
from src.core.calibration import (
    BulbCalibrationTable,
    CalibrationSample,
    CalibrationStore,
    ToneCalibrationStore,
)
from src.core.color_mapping import BulbColorProfile, BulbGamutMapper


def test_calibration_store_round_trip(tmp_path):
    store = CalibrationStore(tmp_path)
    table = BulbCalibrationTable(
        key="cc4085e2d228",
        bulb_mac="cc4085e2d228",
        bulb_model="ESP25_SHRGB_01",
        samples=[
            CalibrationSample(target_rgb=(0, 0, 0), measured_rgb=(0, 0, 0)),
            CalibrationSample(target_rgb=(255, 0, 0), measured_rgb=(250, 8, 8)),
        ],
    )

    path = store.save(table)
    loaded = store.load("cc4085e2d228")

    assert path.exists()
    assert loaded is not None
    assert loaded.bulb_model == "ESP25_SHRGB_01"
    assert loaded.samples[1].measured_rgb == (250, 8, 8)


def test_user_white_balance_round_trip_and_mapping(tmp_path):
    store = CalibrationStore(tmp_path)
    table = BulbCalibrationTable(key="lamp", neutral_white_payload=(180, 255, 180, 160))
    store.save(table)
    loaded = store.load("lamp")
    assert loaded.neutral_white_payload == (180, 255, 180, 160)
    mapper = BulbGamutMapper(BulbColorProfile(white_channels=1), calibration=loaded)
    plain = BulbGamutMapper(BulbColorProfile(white_channels=1))
    assert mapper.map_rgb((255, 255, 255)) == (180, 255, 180, 160)
    assert mapper.map_rgb((0, 0, 0)) == (0, 0, 0, 0)
    assert mapper.map_rgb((255, 0, 0)) == plain.map_rgb((255, 0, 0))
    gray = mapper.map_rgb((128, 128, 128))
    assert all(abs(actual - expected) <= 1 for actual, expected in zip(gray, (90, 128, 90, 80)))


def test_white_balance_ignores_incompatible_or_invalid_payload():
    profile = BulbColorProfile(white_channels=2)
    plain = BulbGamutMapper(profile)
    for payload in ((180, 255, 180, 160), (0, 0, 0, 0, 0), (0, 0, 0, 0, 300)):
        mapper = BulbGamutMapper(profile, calibration=BulbCalibrationTable(
            key="lamp", neutral_white_payload=payload))
        assert mapper.map_rgb((255, 255, 255)) == plain.map_rgb((255, 255, 255))


def test_tone_calibration_store_uses_tone_suffix(tmp_path):
    store = ToneCalibrationStore(tmp_path)
    table = BulbCalibrationTable(
        key="cc4085e2d228",
        samples=[CalibrationSample(target_rgb=(0, 0, 0), measured_rgb=(0, 0, 0))],
    )

    path = store.save(table)

    assert path.name == "cc4085e2d228-tone.json"


def test_calibration_table_without_enough_samples_is_identity():
    table = BulbCalibrationTable(
        key="test",
        samples=[
            CalibrationSample(target_rgb=(0, 0, 0), measured_rgb=(0, 0, 0)),
            CalibrationSample(target_rgb=(255, 255, 255), measured_rgb=(255, 255, 255)),
        ],
    )

    assert table.correct_rgb((96, 132, 188)) == (96, 132, 188)


def test_calibration_table_corrects_midtones_toward_measured_inverse():
    levels = (0, 128, 255)
    samples = []
    for r in levels:
        for g in levels:
            for b in levels:
                measured = (
                    int(round(r * 0.8)),
                    int(round(g * 0.9)),
                    int(round(b * 1.0)),
                )
                samples.append(CalibrationSample(target_rgb=(r, g, b), measured_rgb=measured))

    table = BulbCalibrationTable(key="test", samples=samples)
    corrected = table.correct_rgb((96, 96, 96))

    assert corrected[0] >= 96
    assert corrected[1] >= 96
    assert corrected[2] >= 96


def test_gamut_mapper_uses_loaded_calibration_table():
    table = BulbCalibrationTable(
        key="test",
        samples=[
            CalibrationSample(target_rgb=(0, 0, 0), measured_rgb=(0, 0, 0)),
            CalibrationSample(target_rgb=(255, 0, 0), measured_rgb=(220, 0, 0)),
            CalibrationSample(target_rgb=(0, 255, 0), measured_rgb=(0, 255, 0)),
            CalibrationSample(target_rgb=(0, 0, 255), measured_rgb=(0, 0, 255)),
            CalibrationSample(target_rgb=(255, 255, 255), measured_rgb=(220, 255, 255)),
        ],
    )
    mapper_plain = BulbGamutMapper(BulbColorProfile())
    mapper_calibrated = BulbGamutMapper(BulbColorProfile(), calibration=table)

    plain = np.array(mapper_plain.map_rgb((220, 0, 0)))
    calibrated = np.array(mapper_calibrated.map_rgb((220, 0, 0)))

    assert calibrated[0] >= plain[0]


def test_calibration_table_black_offset_does_not_lift_black():
    table = BulbCalibrationTable(
        key="test",
        samples=[
            CalibrationSample(target_rgb=(0, 0, 0), measured_rgb=(72, 50, 70)),
            CalibrationSample(target_rgb=(255, 0, 0), measured_rgb=(197, 76, 48)),
            CalibrationSample(target_rgb=(0, 255, 0), measured_rgb=(89, 188, 87)),
            CalibrationSample(target_rgb=(0, 0, 255), measured_rgb=(78, 126, 207)),
            CalibrationSample(target_rgb=(255, 255, 255), measured_rgb=(170, 140, 150)),
        ],
    )

    assert table.correct_rgb((0, 0, 0)) == (0, 0, 0)
    corrected = table.correct_rgb((10, 4, 4))
    assert max(corrected) <= 16


def test_run_bulb_command_retries_after_timeout():
    class FakeRunner:
        def __init__(self):
            self.calls = 0

        def run(self, coro, timeout=None):
            self.calls += 1
            if self.calls == 1:
                coro.close()
                raise FutureTimeoutError()
            coro.close()
            return None

    runner = FakeRunner()

    run_bulb_command(
        runner,
        lambda: _noop(),
        timeout=1.0,
        retries=2,
        retry_delay=0.0,
    )

    assert runner.calls == 2


async def _noop():
    return None


def test_camera_feed_has_signal_rejects_black_frames():
    class FakeCamera:
        def __init__(self, frame):
            self.frame = frame

        def read(self):
            return True, self.frame.copy()

    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    assert not camera_feed_has_signal(FakeCamera(frame), frame_count=3)


def test_camera_feed_rejects_sparse_centered_loading_spinner():
    from scripts.calibrate_bulb_camera import frame_has_signal

    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    yy, xx = np.ogrid[:720, :1280]
    radius = (xx - 640) ** 2 + (yy - 360) ** 2
    frame[(radius >= 62 ** 2) & (radius <= 70 ** 2)] = 255
    assert not frame_has_signal(frame)
    frame[:] = 32
    assert frame_has_signal(frame)


def test_camera_feed_has_signal_accepts_visible_roi():
    class FakeCamera:
        def __init__(self, frame):
            self.frame = frame

        def read(self):
            return True, self.frame.copy()

    frame = np.zeros((20, 20, 3), dtype=np.uint8)
    frame[5:10, 5:10] = 32
    assert camera_feed_has_signal(FakeCamera(frame), roi=(5, 5, 5, 5), frame_count=3)


def test_wrong_bulb_never_inherits_a_calibration_after_dhcp_reassignment(tmp_path):
    store = CalibrationStore(tmp_path)
    ip = "192.0.2.8"
    store.save(BulbCalibrationTable(key=ip, bulb_mac="000000000001"))
    assert store.load_for_bulb("000000000002", ip) is None
    store.save(BulbCalibrationTable(key="000000000002", bulb_mac="000000000002"))
    assert store.load_for_bulb("00:00:00:00:00:02", ip).key == "000000000002"
