from vidshrink import db
from vidshrink.config import Config
from vidshrink.probe import Probe

EFFICIENT = {"hevc", "av1"}
H264 = {"h264"}
LEGACY = {"mpeg1video", "mpeg2video", "vc1", "mpeg4", "msmpeg4v1", "msmpeg4v2",
          "msmpeg4v3", "wmv1", "wmv2", "wmv3"}
DEFAULT_RATIO = {"h264": 0.55, "legacy": 0.70}
DEFAULT_SPEED = {"sd": 0.5, "720p": 1.0, "1080p": 1.7, "4k": 6.5}
ENCODERS = ("x265", "nvenc")
NVENC_RATIO_FACTOR = 0.85
DEFAULT_SPEED_NVENC = {"sd": 0.05, "720p": 0.08, "1080p": 0.15, "4k": 0.5}
INTERLACED = ("tt", "bb", "tb", "bt")
BITRATE_FLOOR = {"sd": 0.8e6, "720p": 1.2e6, "1080p": 2.5e6, "4k": 8e6}


def resolution_class(width: int, height: int) -> str:
    if width >= 3000 or height >= 1800:
        return "4k"
    if width >= 1700 or height >= 1000:
        return "1080p"
    if width >= 1100 or height >= 650:
        return "720p"
    return "sd"


def codec_family(codec: str) -> str | None:
    if codec in H264:
        return "h264"
    if codec in LEGACY:
        return "legacy"
    return None


def skip_reason(path: str, size: int, probe: Probe, cfg: Config) -> str | None:
    if any(path.startswith(p) for p in cfg.excluded):
        return "excluded folder"
    if size < cfg.min_size:
        return "too small"
    if size > cfg.max_size:
        return "too large"
    if probe.codec in EFFICIENT:
        return f"already efficient ({probe.codec})"
    if probe.is_dv:
        return "dolby vision"
    if probe.field_order in INTERLACED:
        return "interlaced"
    if codec_family(probe.codec) is None:
        return f"unsupported codec ({probe.codec})"
    res = resolution_class(probe.width, probe.height)
    if probe.vbitrate < BITRATE_FLOOR[res]:
        return f"bitrate already low ({probe.vbitrate / 1e6:.1f} Mbps at {res})"
    return None


def _key(base: str, encoder: str) -> str:
    # x265 keeps the original keys so existing learned data stays valid.
    return base if encoder == "x265" else f"{base}:{encoder}"


def ratio_for(conn, family: str, encoder: str = "x265") -> float:
    default = DEFAULT_RATIO[family] * (NVENC_RATIO_FACTOR if encoder == "nvenc" else 1.0)
    return db.learned_mean(conn, _key(f"ratio:{family}", encoder), default)


def speed_for(conn, res: str, encoder: str = "x265") -> float:
    defaults = DEFAULT_SPEED_NVENC if encoder == "nvenc" else DEFAULT_SPEED
    return db.learned_mean(conn, _key(f"speed:{res}", encoder), defaults[res])


def learn_result(conn, probe: Probe, size: int, out_size: int, encode_secs: float | None,
                 encoder: str) -> None:
    family = codec_family(probe.codec)
    if family:
        db.learn(conn, _key(f"ratio:{family}", encoder), 1 - out_size / size)
    if encode_secs:
        res = resolution_class(probe.width, probe.height)
        db.learn(conn, _key(f"speed:{res}", encoder), encode_secs / probe.duration)


def expected_hours(conn, probe: Probe, encoder: str = "x265") -> float:
    res = resolution_class(probe.width, probe.height)
    return probe.duration * speed_for(conn, res, encoder) / 3600


def encoder_score(conn, size: int, probe: Probe, encoder: str) -> float:
    family = codec_family(probe.codec)
    res = resolution_class(probe.width, probe.height)
    return score(size, probe, ratio_for(conn, family, encoder), speed_for(conn, res, encoder))


def encoder_tables(conn, encoder: str) -> tuple[dict[str, float], dict[str, float]]:
    """Learned ratio per codec family and speed per resolution class for one encoder,
    looked up once so callers can score many rows without a query per row."""
    return ({f: ratio_for(conn, f, encoder) for f in DEFAULT_RATIO},
            {r: speed_for(conn, r, encoder) for r in DEFAULT_SPEED})


def score(size: int, probe: Probe, ratio: float, speed: float) -> float:
    saved_gb = size / 1e9 * ratio
    hours = probe.duration * speed / 3600
    return saved_gb / max(hours, 1e-6)


RESCORABLE = ("probed", "queued")


def rescore(conn, cfg: Config) -> None:
    ratios = {f: ratio_for(conn, f) for f in DEFAULT_RATIO}
    speeds = {r: speed_for(conn, r) for r in DEFAULT_SPEED}
    rows = db.rows_in(conn, *RESCORABLE)
    with db.transaction(conn):
        for row in rows:
            # Guarded: a concurrent worker may have taken the row since it was read.
            probe = Probe.from_json(row["probe_json"])
            reason = skip_reason(row["path"], row["size"], probe, cfg)
            if reason:
                db.mark_if(conn, row["id"], RESCORABLE, "skipped", reason=reason, score=None)
                continue
            family = codec_family(probe.codec)
            res = resolution_class(probe.width, probe.height)
            db.mark_if(conn, row["id"], RESCORABLE, "queued",
                       score=score(row["size"], probe, ratios[family], speeds[res]))
