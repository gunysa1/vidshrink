import pytest

from dataclasses import replace

from vidshrink import db, scorer
from vidshrink.probe import Probe

P = Probe(container="matroska,webm", duration=3600.0, codec="h264", width=1920, height=1080,
          video_index=0, vbitrate=10e6, is_dv=False, color_primaries=None,
          color_transfer=None, color_space=None, master_display=None, max_cll=None,
          streams=[])


def test_nvenc_defaults(conn):
    assert scorer.ratio_for(conn, "h264", "nvenc") == 0.55 * 0.85
    assert scorer.speed_for(conn, "1080p", "nvenc") == 0.15
    assert scorer.speed_for(conn, "1080p") == 1.7  # x265 unchanged


def test_learn_result_keeps_encoders_apart(conn):
    scorer.learn_result(conn, P, 10_000, 3_000, 720.0, "nvenc")
    assert scorer.ratio_for(conn, "h264", "nvenc") != 0.55 * 0.85
    assert scorer.speed_for(conn, "1080p", "nvenc") != 0.15
    assert scorer.ratio_for(conn, "h264") == 0.55
    assert scorer.speed_for(conn, "1080p") == 1.7


def test_learn_result_without_encode_secs(conn):
    scorer.learn_result(conn, P, 10_000, 3_000, None, "x265")
    assert scorer.speed_for(conn, "1080p") == 1.7


def test_expected_hours_and_score(conn):
    assert scorer.expected_hours(conn, P) == 1.7  # 1 h of video × 1.7
    assert scorer.expected_hours(conn, P, "nvenc") == 0.15
    s_x = scorer.encoder_score(conn, 10_000_000_000, P, "x265")
    s_n = scorer.encoder_score(conn, 10_000_000_000, P, "nvenc")
    assert s_n > s_x  # NVENC saves less but is far faster


def test_legacy_family_nvenc(conn):
    assert scorer.ratio_for(conn, "legacy", "nvenc") == 0.70 * 0.85
    assert scorer.expected_hours(conn, replace(P, width=640, height=480), "nvenc") == 0.05


def test_cloud_ratio_shares_x265_keys(conn):
    scorer.learn_result(conn, P, 10e9, 4e9, 3600, "x265-cloud")
    assert scorer.ratio_for(conn, "h264", "x265") == pytest.approx(0.575)  # default prior 0.55 blended with 0.6
    assert scorer.ratio_for(conn, "h264", "x265-cloud") == pytest.approx(0.575)


def test_cloud_speed_has_own_keys(conn):
    scorer.learn_result(conn, P, 10e9, 4e9, 2 * P.duration, "x265-cloud")
    assert scorer.speed_for(conn, "1080p", "x265-cloud") == pytest.approx(2.275)  # (2.55 prior + 2.0) / 2
    assert scorer.speed_for(conn, "1080p", "x265") == scorer.DEFAULT_SPEED["1080p"]


def test_x265_speed_leaves_cloud_speed_default(conn):
    scorer.learn_result(conn, P, 10e9, 4e9, 2 * P.duration, "x265")
    assert scorer.speed_for(conn, "1080p", "x265-cloud") == pytest.approx(
        scorer.DEFAULT_SPEED["1080p"] * 1.5)


def test_cloud_default_speed_is_slower(conn):
    for r in scorer.DEFAULT_SPEED:
        assert scorer.speed_for(conn, r, "x265-cloud") == pytest.approx(
            scorer.DEFAULT_SPEED[r] * 1.5)
