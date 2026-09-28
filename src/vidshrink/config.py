from dataclasses import dataclass
from pathlib import Path

HOME = Path.home() / "vidshrink"


@dataclass
class Config:
    remote: str = "gdrive:"
    db_path: Path = HOME / "state.db"
    workdir: Path = HOME / "work"
    excluded: tuple[str, ...] = ("Torrent/", "Private/")
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
    max_ready: int = 2
    max_attempts: int = 2
    max_consecutive_failures: int = 5
    retry_attempts: int = 5
    retry_base_delay: float = 5.0
    disk_factor: float = 2.2
    disk_reserve: int = 20_000_000_000
    disk_wait_secs: float = 600
    swap_retry_secs: float = 900
    # Pinned so the Drive behaviour the swap relies on can't drift with rclone.conf:
    # deletes go to the trash, the 750 GB/day upload limit fails fast, and our own
    # with_retries (not rclone's) owns retrying.
    rclone_args: tuple[str, ...] = ("--drive-chunk-size", "64M", "--drive-use-trash=true",
                                    "--drive-stop-on-upload-limit", "--retries", "1")
    probe_workers: int = 8
