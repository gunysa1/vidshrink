"""The Windows PC's side: claim a job, encode it with NVENC, upload, report.
Must stay importable on Windows: no fcntl, no vidshrink.worker."""
import os
import platform
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path

from vidshrink.config import Config
from vidshrink.encode import (EncodeCancelled, EncodeError, build_nvenc_command,
                              run_encode_cancellable)
from vidshrink.jobs import JobClient, JobServiceError, LeaseLost
from vidshrink.paths import md5_file
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, with_retries
from vidshrink.verify import compare, decode_errors

TOOLS = ("rclone", "ffmpeg", "ffprobe")


def _default_workdir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "vidshrink" / "work"


@dataclass
class PCConfig:
    server: str
    token: str
    remote: str = "gdrive:"
    workdir: Path = field(default_factory=_default_workdir)
    cq: int = 23
    bwlimit: str = "6M"
    worker_name: str = field(default_factory=platform.node)
    rclone: str = "rclone"
    ffmpeg_dir: str = ""
    min_saving: float = 0.20
    heartbeat_secs: float = 60
    idle_secs: float = 300
    retry_attempts: int = 5
    retry_base_delay: float = 5.0


def default_config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "vidshrink" / "config.toml"


def load_pc_config(path: Path) -> PCConfig:
    data = tomllib.loads(Path(path).read_text())
    known = {f.name for f in fields(PCConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown settings in {path}: {sorted(unknown)}")
    if "workdir" in data:
        data["workdir"] = Path(data["workdir"])
    return PCConfig(**data)


def pc_rclone(cfg: PCConfig) -> Rclone:
    # Same pinned Drive behaviour as the server, plus an upload-only bandwidth cap so the
    # 100 Mbps uplink stays usable for Plex.
    return Rclone(cfg.remote, (*Config().rclone_args, "--bwlimit", f"{cfg.bwlimit}:off"),
                  binary=cfg.rclone)


class _Abandon(Exception):
    """Stop working on the current job (Stop now or the server cancelled it)."""


EncodeFn = Callable[[Path, Path, Probe, Path, threading.Event], float]


class PCWorker:
    def __init__(self, cfg: PCConfig, client: JobClient, rclone: Rclone,
                 encode: EncodeFn | None = None, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.client = client
        self.rclone = rclone
        self.encode = encode or self._nvenc
        self.log = log
        self.state = "idle"
        self.stage = ""
        self.current_path: str | None = None
        self.done = 0
        self._stop_now = threading.Event()
        self._stop_after = threading.Event()
        self._cancel = threading.Event()  # current job only
        if cfg.ffmpeg_dir:
            os.environ["PATH"] = cfg.ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    # --- control -------------------------------------------------------------
    def stop(self, now: bool) -> None:
        if now:
            self._stop_now.set()
            self._cancel.set()
        else:
            self._stop_after.set()
            if self.state == "working":
                self.state = "finishing"

    def _check(self) -> None:
        if self._cancel.is_set():
            raise _Abandon()

    # --- loop ---------------------------------------------------------------
    def run(self) -> None:
        self.cfg.workdir.mkdir(parents=True, exist_ok=True)
        try:
            while not self._stop_now.is_set() and not self._stop_after.is_set():
                try:
                    resp = self.client.claim(self.cfg.worker_name, "nvenc")
                except JobServiceError as e:
                    self.log(f"job service unreachable, retrying: {e}")
                    self._stop_now.wait(self.cfg.idle_secs)
                    continue
                job = resp.get("job")
                if job is None:
                    self.stage = "waiting for work"
                    self._stop_now.wait(min(resp.get("retry_after") or self.cfg.idle_secs,
                                            self.cfg.idle_secs))
                    continue
                self.state = "finishing" if self._stop_after.is_set() else "working"
                self._do_job(job)
                self.state = "idle"
        finally:
            self.state = "stopped"
            self.stage = ""

    def _retry(self, fn):
        return with_retries(fn, self.cfg.retry_attempts, self.cfg.retry_base_delay,
                            lambda s: self._stop_now.wait(s))

    def _heartbeat(self, job_id: int, stop: threading.Event) -> None:
        while not stop.wait(self.cfg.heartbeat_secs):
            try:
                if self.client.heartbeat(job_id, self.cfg.worker_name).get("cancel"):
                    self.log("server cancelled the job")
                    self._cancel.set()
                    return
            except JobServiceError as e:
                self.log(f"heartbeat failed (will retry): {e}")

    def _nvenc(self, src: Path, out: Path, probe: Probe, log_path: Path,
               cancel: threading.Event) -> float:
        return run_encode_cancellable(build_nvenc_command(src, out, probe, self.cfg.cq),
                                      log_path, cancel)

    def _report(self, job: dict, outcome: str, **data) -> None:
        try:
            self.client.report(job["id"], self.cfg.worker_name, outcome, **data)
        except LeaseLost:
            self.log(f"lease lost before reporting {outcome}: {job['path']}")

    def _do_job(self, job: dict) -> None:
        self._cancel.clear()
        if self._stop_now.is_set():
            self._cancel.set()
        jid, path, tmp = job["id"], job["path"], job["tmp_path"]
        src = self.cfg.workdir / f"{jid}.src"
        out = self.cfg.workdir / f"{jid}.out.mkv"
        log_path = self.cfg.workdir / f"{jid}.log"
        probe = Probe(**job["probe"])
        self.current_path = path
        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(jid, hb_stop), daemon=True)
        hb.start()
        uploading = False
        md5 = None
        try:
            self.stage = "downloading"
            self._check()
            self._retry(lambda: self.rclone.download(path, src))
            self._check()
            if src.stat().st_size != job["size"]:
                self._report(job, "changed")
                return
            self.stage = "encoding"
            secs = self.encode(src, out, probe, log_path, self._cancel)
            self.stage = "verifying"
            out_size = out.stat().st_size
            verdict = compare(probe, parse_ffprobe(run_ffprobe(str(out)), out_size),
                              job["size"], out_size, self.cfg.min_saving, decode_errors(out))
            if verdict.problems:
                self._report(job, "failed", reason="; ".join(verdict.problems))
                return
            if verdict.not_worth_it:
                self._report(job, "not_worth_it",
                             reason=f"only {1 - out_size / job['size']:.0%} smaller")
                return
            md5 = md5_file(out)
            self._check()
            self.stage = "uploading"
            uploading = True
            self._retry(lambda: self.rclone.upload(out, tmp))
            if self.rclone.md5(tmp) != md5:
                self.rclone.delete(tmp)
                self._report(job, "failed", reason="checksum mismatch after upload")
                return
            self._check()
            try:
                self.client.report(jid, self.cfg.worker_name, "uploaded", out_size=out_size,
                                   out_md5=md5, encode_secs=secs)
                self.done += 1
                self.log(f"uploaded {tmp} ({out_size / 1e9:.1f} GB)")
            except LeaseLost:
                self._delete_tmp_if_ours(tmp, md5)
        except (_Abandon, EncodeCancelled):
            self._abandon(job, tmp if uploading else None, md5)
        except RcloneError as e:
            if e.download_quota:
                self._report(job, "download_quota")
            else:
                self._report(job, "failed", reason=str(e))
        except FileNotFoundError as e:
            if Path(str(e.filename)).stem.lower() in TOOLS:
                self._abandon(job, None, None)
                raise
            self._report(job, "failed", reason=str(e))
        except (EncodeError, ProbeError, OSError) as e:
            self._report(job, "failed", reason=str(e))
        finally:
            hb_stop.set()
            hb.join(5)
            for f in (src, out, log_path):
                f.unlink(missing_ok=True)
            self.current_path = None
            self.stage = ""

    def _delete_tmp_if_ours(self, tmp: str, md5: str | None) -> None:
        try:
            if md5 and self.rclone.exists(tmp) and self.rclone.md5(tmp) == md5:
                self.rclone.delete(tmp)
        except RcloneError as e:
            self.log(f"could not remove {tmp}: {e}")

    def _abandon(self, job: dict, tmp: str | None, md5: str | None) -> None:
        # While the job is still leased to us nobody else writes this tmp name, so a
        # partial upload can be removed without an md5 check.
        if tmp:
            try:
                if self.rclone.exists(tmp):
                    self.rclone.delete(tmp)
            except RcloneError as e:
                self.log(f"could not remove partial {tmp}: {e}")
        try:
            self.client.release(job["id"], self.cfg.worker_name)
        except (LeaseLost, JobServiceError):
            pass
        self.log(f"abandoned: {job['path']}")
