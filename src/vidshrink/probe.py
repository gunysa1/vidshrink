import json
import subprocess
from dataclasses import asdict, dataclass

from vidshrink import procflags
from vidshrink.procflags import TEXT_KWARGS

HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


class ProbeError(Exception):
    pass


@dataclass
class Probe:
    container: str
    duration: float
    codec: str
    width: int
    height: int
    video_index: int
    vbitrate: float
    is_dv: bool
    color_primaries: str | None
    color_transfer: str | None
    color_space: str | None
    master_display: str | None
    max_cll: str | None
    streams: list[list[str]]
    field_order: str | None = None  # last, with a default: older stored JSON lacks it

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Probe":
        return cls(**json.loads(s))


def _ratio(v: str) -> float:
    num, _, den = str(v).partition("/")
    d = float(den or 1)
    return float(num) / d if d else 0.0


def _master_display(sd: dict) -> str:
    def xy(prefix):
        return f"{round(_ratio(sd[prefix + '_x']) * 50000)},{round(_ratio(sd[prefix + '_y']) * 50000)}"

    lmax = round(_ratio(sd["max_luminance"]) * 10000)
    lmin = round(_ratio(sd["min_luminance"]) * 10000)
    return f"G({xy('green')})B({xy('blue')})R({xy('red')})WP({xy('white_point')})L({lmax},{lmin})"


def _stream_bitrate(s: dict) -> float | None:
    for v in (s.get("bit_rate"), (s.get("tags") or {}).get("BPS"),
              (s.get("tags") or {}).get("BPS-eng")):
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except ValueError:
            pass
    return None


def parse_ffprobe(data: dict, size: int) -> Probe:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    videos = [s for s in streams if s.get("codec_type") == "video"]
    main = next((s for s in videos if not (s.get("disposition") or {}).get("attached_pic")), None)
    if main is None:
        raise ProbeError("no video stream")
    try:
        duration = float(fmt.get("duration") or main.get("duration"))
    except (TypeError, ValueError):
        raise ProbeError("no duration") from None
    if duration <= 0:
        raise ProbeError("zero duration")

    master = cll = None
    is_dv = main.get("codec_tag_string") in ("dvh1", "dvhe", "dav1")
    for s in videos:
        for sd in s.get("side_data_list") or []:
            kind = sd.get("side_data_type", "")
            if "DOVI" in kind:
                is_dv = True
            elif s is main and kind == "Mastering display metadata":
                try:
                    master = _master_display(sd)
                except (KeyError, ValueError):
                    master = None
            elif s is main and kind == "Content light level metadata":
                cll = f"{sd.get('max_content', 0)},{sd.get('max_average', 0)}"

    vbitrate = _stream_bitrate(main) or size * 8 / duration * 0.9
    return Probe(
        container=fmt.get("format_name", ""),
        duration=duration,
        codec=main.get("codec_name", ""),
        width=int(main.get("width") or 0),
        height=int(main.get("height") or 0),
        video_index=videos.index(main),
        vbitrate=vbitrate,
        is_dv=is_dv,
        color_primaries=main.get("color_primaries"),
        color_transfer=main.get("color_transfer"),
        color_space=main.get("color_space"),
        master_display=master,
        max_cll=cll,
        streams=[
            [s["codec_type"], s.get("codec_name", ""), (s.get("tags") or {}).get("language", "und")]
            for s in streams if s.get("codec_type") in ("audio", "subtitle")
        ],
        field_order=main.get("field_order"),
    )


def run_ffprobe(target: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", target]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=300, **TEXT_KWARGS,
                             **procflags.spawn_kwargs())
    except subprocess.TimeoutExpired:
        raise ProbeError("ffprobe timed out") from None
    if res.returncode != 0:
        raise ProbeError(res.stderr.strip()[:300] or f"ffprobe exit {res.returncode}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ProbeError(f"bad ffprobe json: {e}") from None
