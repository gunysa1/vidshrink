from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home() / "vidshrink"


def load_excluded(path: Path = HOME / "excluded.txt") -> tuple[str, ...]:
    """Torrent/ plus any personal folder prefixes listed one per line in a local,
    untracked file (blank lines and # comments ignored), so they stay out of the repo."""
    extra = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                extra.append(line)
    return ("Torrent/", *extra)


@dataclass
class Config:
    remote: str = "gdrive:"
    db_path: Path = HOME / "state.db"
    workdir: Path = HOME / "work"
    excluded: tuple[str, ...] = field(default_factory=load_excluded)
    extensions: tuple[str, ...] = (
        "mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "ts", "m2ts",
    )
    min_size: int = 200_000_000
    max_size: int = 120_000_000_000
    crf: int = 22
    crf_4k: int = 24
    preset: str = "medium"
    min_saving: float = 0.20
    daily_upload_limit: int = 700_000_000_000
    rate_limit_block_secs: float = 6 * 3600
    # Drive's daily download quota is shared with the Plex mount on the same account;
    # stay well under it. A downloadQuotaExceeded error pauses downloads for 12 h.
    daily_download_limit: int = 500_000_000_000
    download_block_secs: float = 12 * 3600
    # The PC's own (second) Google account, used only for downloads, with its own
    # separate quota -- so PC jobs no longer spend the main account's 500 GB/day.
    # Own accounts, Plex never reads through them; only Google's limit applies (2026-10-19).
    daily_pc_download_limit: int | None = None
    daily_cloud_download_limit: int | None = None  # the cloud VM's own Drive account
    max_ready: int = 2
    max_attempts: int = 2
    max_consecutive_failures: int = 5
    retry_attempts: int = 5
    retry_base_delay: float = 5.0
    disk_factor: float = 2.2
    disk_reserve: int = 20_000_000_000
    disk_wait_secs: float = 600
    swap_retry_secs: float = 900
    # PC worker / job service (see docs/superpowers/specs/2026-10-05-pc-worker-design.md)
    lease_secs: float = 1800
    pc_handover_hours: float = 6.0
    pc_absent_days: float = 3.0
    idle_poll_secs: float = 60
    serve_host: str = "0.0.0.0"
    serve_port: int = 8765
    token_path: Path = HOME / "serve.token"
    # Pinned so the Drive behaviour the swap relies on can't drift with rclone.conf:
    # deletes go to the trash, the 750 GB/day upload limit fails fast, and our own
    # with_retries (not rclone's) owns retrying.
    rclone_args: tuple[str, ...] = ("--drive-chunk-size", "64M", "--drive-use-trash=true",
                                    "--drive-stop-on-upload-limit", "--retries", "1")
    probe_workers: int = 4
    scan_batch: int = 100
    # serve http reads only what ffprobe asks for (defaults buffer 16M / chunk 128M).
    serve_args: tuple[str, ...] = ("--buffer-size", "0", "--vfs-read-chunk-size", "1M",
                                   "--vfs-read-chunk-size-limit", "1M")
