"""The Windows PC's side: claim a job, encode it with NVENC, upload, report.
Must stay importable on Windows: no fcntl, no vidshrink.worker."""
import os
import platform
import random
import shutil
import threading
import time
import tomllib
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path

from vidshrink.config import Config
from vidshrink.encode import (EncodeCancelled, EncodeError, build_nvenc_command,
                              run_encode_cancellable)
from vidshrink.jobs import JobClient, JobServiceError, LeaseLost
from vidshrink.paths import md5_file, tmp_path
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, TMP_SUFFIX, TransferCancelled
from vidshrink.verify import compare, decode_errors


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
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(PCConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown settings in {path}: {sorted(unknown)}")
    if "workdir" in data:
        data["workdir"] = Path(data["workdir"])
    return PCConfig(**data)


def apply_ffmpeg_dir(cfg: PCConfig) -> None:
    """Prepend cfg.ffmpeg_dir to PATH, idempotently, if set. Shared by PCWorker
    (which needs ffmpeg/ffprobe on PATH to encode) and calibrate (which never
    constructs a PCWorker, so it would otherwise never see ffmpeg_dir)."""
    if not cfg.ffmpeg_dir:
        return
    current = os.environ.get("PATH", "")
    if cfg.ffmpeg_dir not in current.split(os.pathsep):
        os.environ["PATH"] = cfg.ffmpeg_dir + os.pathsep + current


def pc_rclone(cfg: PCConfig) -> Rclone:
    # Same pinned Drive behaviour as the server, plus an upload-only bandwidth cap so the
    # 100 Mbps uplink stays usable for Plex.
    return Rclone(cfg.remote, (*Config().rclone_args, "--bwlimit", f"{cfg.bwlimit}:off"),
                  binary=cfg.rclone)


class _Abandon(Exception):
    """Stop working on the current job (Stop now or the server cancelled it)."""


class _LocalFault(Exception):
    """Something at the PC itself is broken (missing tool, full disk, permissions,
    a crashed subprocess launch, ...) -- not this video's fault. The whole worker
    stops; the job is released with no attempt burned, so it's picked up again
    (by this worker once fixed, or another one) rather than failed outright."""


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
        self._wake = threading.Event()  # wakes an idle wait promptly on either stop mode
        self._current_cancel: threading.Event | None = None  # the running job's cancel, if any
        # Consecutive unexpected (catch-all) errors per job id, in memory: a job that
        # keeps raising something we didn't anticipate gets released once, then
        # reported failed (poison-job guard) rather than released forever.
        self._unexpected_counts: dict[int, int] = {}
        # Prepend once, idempotently: run() may be called more than once per
        # process (e.g. restarted after a fix), and a growing PATH is a sign
        # something is wrong.
        apply_ffmpeg_dir(cfg)

    # --- control -------------------------------------------------------------
    def stop(self, now: bool) -> None:
        if now:
            self._stop_now.set()
            cancel = self._current_cancel
            if cancel is not None:
                cancel.set()
        else:
            self._stop_after.set()
            if self.state == "working":
                self.state = "finishing"
        self._wake.set()

    def _check(self, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise _Abandon()

    # --- startup --------------------------------------------------------------
    def _missing_tools(self) -> list[str]:
        # ffmpeg_dir (if set) was already prepended to PATH in __init__, so a plain
        # PATH lookup covers "in ffmpeg_dir if set, else PATH".
        return [name for name in ("ffmpeg", "ffprobe", self.cfg.rclone)
                if shutil.which(name) is None]

    # --- loop ---------------------------------------------------------------
    def run(self) -> None:
        missing = self._missing_tools()
        if missing:
            self.log(f"required tool(s) not found on PATH: {', '.join(missing)}")
            self.state = "stopped"
            return
        self.cfg.workdir.mkdir(parents=True, exist_ok=True)
        try:
            while not self._stop_now.is_set() and not self._stop_after.is_set():
                try:
                    resp = self.client.claim(self.cfg.worker_name, "nvenc")
                except JobServiceError as e:
                    self.log(f"job service unreachable, retrying: {e}")
                    self._wake.wait(self.cfg.idle_secs)
                    self._wake.clear()
                    continue
                job = resp.get("job")
                if job is None:
                    self.stage = "waiting for work"
                    wait = min(resp.get("retry_after") or self.cfg.idle_secs, self.cfg.idle_secs)
                    self._wake.wait(wait)
                    self._wake.clear()
                    continue
                self.state = "finishing" if self._stop_after.is_set() else "working"
                try:
                    self._do_job(job)
                except _LocalFault as e:
                    self.log(f"stopping: local fault: {e}")
                    return
                except Exception as e:
                    self._handle_unexpected(job, e)
                self.state = "idle"
        finally:
            self.state = "stopped"
            self.stage = ""

    def _handle_unexpected(self, job: dict, e: Exception) -> None:
        """Never let a bug in one job take the whole worker down. The first time a
        given job id raises something we didn't anticipate, release it un-penalised
        (could be a one-off). If the SAME job id does it again right after being
        re-claimed, it's a poison job: report it failed (burns an attempt, so the
        server's max_attempts eventually parks it) instead of releasing forever."""
        job_id = job.get("id")
        tb = "".join(traceback.format_exception_only(type(e), e)).strip()
        count = self._unexpected_counts.get(job_id, 0) + 1
        self._unexpected_counts[job_id] = count
        if count >= 2:
            self.log(f"job {job_id} failed unexpectedly {count} times in a row, "
                     f"reporting failed: {tb}")
            self._unexpected_counts.pop(job_id, None)
            try:
                self._retry_job_call(lambda: self.client.report(
                    job_id, self.cfg.worker_name, "failed", reason=tb[:500]))
            except (LeaseLost, JobServiceError):
                pass
        else:
            self.log(f"unexpected error on job {job_id} (will release): {tb}")
            try:
                self.client.release(job_id, self.cfg.worker_name)
            except (LeaseLost, JobServiceError):
                pass
        self._wake.wait(self.cfg.idle_secs)
        self._wake.clear()

    def _retry(self, fn, cancel: threading.Event):
        """Retry an rclone transfer with backoff, like rclone.with_retries, but
        aborts (no new attempt) once Stop now or a server cancel fires during the
        wait -- and when that happens, raises _Abandon (not the RcloneError), so
        the job is released rather than reported failed: this is an operator/server
        stop, not a video problem."""
        for i in range(self.cfg.retry_attempts):
            try:
                return fn()
            except RcloneError as e:
                if e.rate_limited or e.download_quota or i == self.cfg.retry_attempts - 1:
                    raise
                delay = self.cfg.retry_base_delay * 2 ** i + random.uniform(
                    0, self.cfg.retry_base_delay)
                if self._wait_or_cancelled(delay, cancel):
                    raise _Abandon() from e
        raise AssertionError("unreachable")

    def _wait_or_cancelled(self, delay: float, cancel: threading.Event) -> bool:
        """Wait up to delay seconds, waking early on Stop now or the per-job
        cancel; returns True iff woken early by either."""
        end = time.monotonic() + delay
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            if self._stop_now.wait(min(remaining, 0.2)) or cancel.is_set():
                return True

    def _retry_job_call(self, fn):
        """Retry a job-service call on a transient JobServiceError (no status, or
        5xx): never on LeaseLost, never on a non-transient 4xx. Interruptible by
        Stop now."""
        last = None
        for i in range(self.cfg.retry_attempts):
            try:
                return fn()
            except LeaseLost:
                raise
            except JobServiceError as e:
                last = e
                transient = e.status is None or e.status >= 500
                if not transient or i == self.cfg.retry_attempts - 1:
                    raise
                if self._stop_now.wait(self.cfg.retry_base_delay * 2 ** i):
                    raise
        raise last

    def _heartbeat(self, job_id: int, stop: threading.Event, cancel: threading.Event,
                   server_cancelled: threading.Event) -> None:
        while not stop.wait(self.cfg.heartbeat_secs):
            try:
                if self.client.heartbeat(job_id, self.cfg.worker_name).get("cancel"):
                    self.log("server cancelled the job")
                    server_cancelled.set()
                    cancel.set()
                    return
            except Exception as e:  # never let a heartbeat blip kill the thread
                self.log(f"heartbeat failed (will retry): {e}")

    def _nvenc(self, src: Path, out: Path, probe: Probe, log_path: Path,
               cancel: threading.Event) -> float:
        return run_encode_cancellable(build_nvenc_command(src, out, probe, self.cfg.cq),
                                      log_path, cancel)

    def _report(self, job: dict, outcome: str, **data) -> None:
        try:
            self._retry_job_call(
                lambda: self.client.report(job["id"], self.cfg.worker_name, outcome, **data))
        except LeaseLost:
            self.log(f"lease lost before reporting {outcome}: {job['path']}")
        except JobServiceError as e:
            self.log(f"could not report {outcome} for {job['path']}: {e}")

    def _report_uploaded(self, job: dict, jid: int, path: str, tmp: str, out_size: int,
                         md5: str, secs: float) -> None:
        """Report the completed upload, retrying on a transient JobServiceError.
        Once ANY attempt in this call has failed ambiguously (status None, 5xx,
        timeout, or a connection error -- i.e. we don't know if the server actually
        committed it), a later LeaseLost no longer proves the tmp is safe to
        delete: the server may have accepted our first attempt and already moved
        the row on (report() is idempotent for exactly this retry), so deleting
        the tmp then would destroy the only copy of a successful upload. Only an
        UNAMBIGUOUS LeaseLost on the very first attempt (no prior ambiguity) means
        it's safe to clean up our own tmp."""
        ambiguous = False
        for i in range(self.cfg.retry_attempts):
            try:
                self.client.report(jid, self.cfg.worker_name, "uploaded",
                                   out_size=out_size, out_md5=md5, encode_secs=secs)
                self.done += 1
                self.log(f"uploaded {tmp} ({out_size / 1e9:.1f} GB)")
                return
            except LeaseLost:
                if not ambiguous:
                    self._delete_tmp_if_ours(job, tmp, md5)
                else:
                    self.log(f"lease lost reporting uploaded after an earlier "
                             f"ambiguous failure; leaving {tmp} for the server to "
                             f"reconcile: {job['path']}")
                return
            except JobServiceError as e:
                ambiguous = True
                transient = e.status is None or e.status >= 500
                if not transient or i == self.cfg.retry_attempts - 1:
                    self.log(f"could not report uploaded for {path}: {e}; "
                             f"leaving {tmp} for the server/lease expiry to reconcile")
                    return
                if self._stop_now.wait(self.cfg.retry_base_delay * 2 ** i):
                    self.log(f"stop now while retrying the uploaded report; leaving "
                             f"{tmp} for the server/lease expiry to reconcile: "
                             f"{job['path']}")
                    return

    def _safe_tmp(self, job: dict, tmp: str) -> bool:
        """Defense in depth: never touch a remote path that isn't provably the
        tmp file this job owns."""
        if tmp != tmp_path(job["path"]) or not tmp.endswith(TMP_SUFFIX):
            self.log(f"refusing to delete suspicious tmp path: {tmp!r}")
            return False
        return True

    def _do_job(self, job: dict) -> None:
        jid = job["id"]
        cancel = threading.Event()
        # Publish before checking: if stop() is called between these two lines, it
        # still sees (and sets) THIS job's cancel, instead of a stale or absent one.
        self._current_cancel = cancel
        if self._stop_now.is_set():
            cancel.set()
        src = self.cfg.workdir / f"{jid}.src"
        out = self.cfg.workdir / f"{jid}.out.mkv"
        log_path = self.cfg.workdir / f"{jid}.log"
        self.current_path = job.get("path")
        hb_stop = threading.Event()
        server_cancelled = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(jid, hb_stop, cancel,
                                                             server_cancelled), daemon=True)
        hb.start()
        uploading = False
        md5 = None
        tmp = None
        try:
            path, tmp = job["path"], job["tmp_path"]
            probe = Probe(**job["probe"])
            self.stage = "downloading"
            self._check(cancel)
            self._retry(lambda: self.rclone.download(path, src, cancel=cancel), cancel)
            self._check(cancel)
            if src.stat().st_size != job["size"]:
                self._report(job, "changed")
                return
            self.stage = "encoding"
            secs = self.encode(src, out, probe, log_path, cancel)
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
            self._check(cancel)
            self.stage = "uploading"
            uploading = True
            self._retry(lambda: self.rclone.upload(out, tmp, cancel=cancel), cancel)
            if self.rclone.md5(tmp) != md5:
                self._delete_tmp_if_ours(job, tmp, md5)
                self._report(job, "failed", reason="checksum mismatch after upload")
                return
            self._check(cancel)
            self._report_uploaded(job, jid, path, tmp, out_size, md5, secs)
        except (_Abandon, EncodeCancelled, TransferCancelled):
            self._abandon(job, tmp if uploading else None, md5,
                          guarded=server_cancelled.is_set())
        except RcloneError as e:
            if e.rate_limited and uploading:
                self._delete_tmp_if_ours(job, tmp, md5)
                self._report(job, "upload_limited")
            elif e.rate_limited:
                # Surprising on a download (the limit is normally hit on upload),
                # but still not this video's fault: release and back off like any
                # other transient blip, without burning an attempt or touching any
                # tmp (nothing was uploaded yet).
                self.log(f"rate limited during download (unexpected); releasing "
                         f"and backing off: {job['path']}")
                self._abandon(job, None, None, guarded=server_cancelled.is_set())
                self._wake.wait(self.cfg.idle_secs)
                self._wake.clear()
            elif e.download_quota:
                self._report(job, "download_quota")
            else:
                self._report(job, "failed", reason=str(e))
        except (EncodeError, ProbeError) as e:
            self._report(job, "failed", reason=str(e))
        except OSError as e:
            # A local problem (missing tool, full disk, permissions, ...), not this
            # video's fault: release it (no attempt burned) and stop the worker.
            self._abandon(job, tmp if uploading else None, md5,
                          guarded=server_cancelled.is_set())
            raise _LocalFault(str(e)) from e
        finally:
            hb_stop.set()
            hb.join(5)
            self._current_cancel = None
            for f in (src, out, log_path):
                f.unlink(missing_ok=True)
            self.current_path = None
            self.stage = ""

    def _delete_tmp_if_ours(self, job: dict, tmp: str | None, md5: str | None) -> None:
        if not tmp or not self._safe_tmp(job, tmp):
            return
        try:
            if md5 and self.rclone.exists(tmp) and self.rclone.md5(tmp) == md5:
                self.rclone.delete(tmp)
        except RcloneError as e:
            self.log(f"could not remove {tmp}: {e}")

    def _abandon(self, job: dict, tmp: str | None, md5: str | None, guarded: bool = False) -> None:
        if tmp:
            if guarded:
                # The lease may no longer be ours (server cancel): only remove the
                # tmp if it still provably holds the bytes we uploaded.
                self._delete_tmp_if_ours(job, tmp, md5)
            elif self._safe_tmp(job, tmp):
                # Stop now / our own release: the lease is still ours, nobody else
                # writes this tmp name while that's true.
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
