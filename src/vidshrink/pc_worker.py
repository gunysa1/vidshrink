"""The remote worker's side: claim a job, encode it, upload, report. Runs on the
Windows PC (NVENC) and, headless, on a Linux cloud VM (x265, encoder="x265-cloud").
Must stay importable on Windows: no fcntl, no vidshrink.worker."""
import os
import platform
import random
import re
import shutil
import threading
import time
import tomllib
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path

from vidshrink.config import Config
from vidshrink.encode import (EncodeCancelled, EncodeError, build_command,
                              build_nvenc_command, run_encode_cancellable)
from vidshrink.jobs import JobClient, JobServiceError, LeaseLost
from vidshrink.paths import md5_file, tmp_path
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, TMP_SUFFIX, TransferCancelled
from vidshrink.verify import compare, decode_errors


def _default_workdir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "vidshrink" / "work"


# Only files whose NAME matches one of these are ever touched by clean_workdir --
# never anything else, and never a subdirectory, even if workdir is a whole drive.
# The first group of each numbered pattern is the job id, so a match can be kept
# when it belongs to the job currently running.
_NUMBERED_PATTERNS = tuple(re.compile(p) for p in (
    r"^(\d+)\.src$",
    r"^(\d+)\.out\.mkv$",
    r"^(\d+)\.log$",
    r"^(\d+)\.encoded$",
    r"^(\d+)\.downloaded$",
    # rclone's local backend, when it does write a partial file, names it
    # "<name>.<random>.partial" (e.g. "65137.src.a1b2c3.partial").
    r"^(\d+)\.(?:src|out\.mkv)\..*\.partial$",
    # A marker's own write-then-rename tmp, if a crash lands mid-write.
    r"^(\d+)\.(?:encoded|downloaded)\.tmp$",
))
# calibrate's own leftovers: never present while the PC worker loop runs (the two
# never run at once in one process), so always safe to sweep here.
_CALIBRATE_PATTERNS = tuple(re.compile(p) for p in (r"^cal-.*$", r"^cal\.log$"))


def clean_workdir(workdir: Path, keep_id: int | None) -> list[Path]:
    """Delete vidshrink's own stale work files from `workdir` -- never a
    subdirectory, never anything whose name doesn't match one of our own
    patterns. Files that belong to `keep_id` (by the numeric id in their name)
    are kept; everything else matching is removed. Returns what was actually
    deleted; an OSError deleting one file is swallowed so the rest still get a
    chance (and so this never fails a job)."""
    deleted = []
    try:
        entries = list(workdir.iterdir())
    except OSError:
        return deleted
    for p in entries:
        if not p.is_file():
            continue
        name = p.name
        match = False
        for pat in _NUMBERED_PATTERNS:
            m = pat.match(name)
            if m:
                if keep_id is not None and int(m.group(1)) == keep_id:
                    match = False
                else:
                    match = True
                break
        if not match:
            match = any(pat.match(name) for pat in _CALIBRATE_PATTERNS)
        if not match:
            continue
        try:
            p.unlink()
            deleted.append(p)
        except OSError:
            continue
    return deleted


@dataclass
class PCConfig:
    server: str
    token: str
    remote: str = "gdrive:"
    # A second, free Google account with Viewer access to the library, used ONLY for
    # downloads (so PC jobs no longer spend the main account's shared download quota).
    # Empty means downloads use `remote`, as before.
    download_remote: str = ""
    workdir: Path = field(default_factory=_default_workdir)
    cq: int = 23
    bwlimit: str = "6M"
    worker_name: str = field(default_factory=platform.node)
    rclone: str = "rclone"
    ffmpeg_dir: str = ""
    min_saving: float = 0.20
    heartbeat_secs: float = 60
    # Must match the server's Config.lease_secs: once heartbeats have failed for this
    # long, the server has expired our lease, so the job is treated as server-cancelled.
    lease_secs: float = 1800
    idle_secs: float = 300
    retry_attempts: int = 5
    retry_base_delay: float = 5.0
    # The cloud worker: "x265-cloud" encodes with the server's x265 command instead of NVENC.
    encoder: str = "nvenc"
    # Which download budget to charge. Empty = "pc" if download_remote else "main".
    download_account: str = ""
    # Only claim jobs expected to finish within this many hours; 0 = no limit sent.
    max_hours: float = 0.0
    # Send the workdir's free bytes on each claim so the server skips jobs that won't fit.
    disk_check: bool = False


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


def _bwlimit(cfg: PCConfig) -> str:
    """rclone --bwlimit value. A plain value caps uploads only ("6M" -> "6M:off");
    an explicit "UP:DOWN" pair (e.g. "6M:40M") is passed through as written."""
    return cfg.bwlimit if ":" in cfg.bwlimit else f"{cfg.bwlimit}:off"


def _bwlimit_args(cfg: PCConfig) -> tuple[str, ...]:
    return ("--bwlimit", _bwlimit(cfg)) if cfg.bwlimit else ()


def pc_rclone(cfg: PCConfig) -> Rclone:
    # Same pinned Drive behaviour as the server, plus an upload-only bandwidth cap so the
    # 100 Mbps uplink stays usable for Plex.
    return Rclone(cfg.remote, (*Config().rclone_args, *_bwlimit_args(cfg)),
                  binary=cfg.rclone)


def pc_download_rclone(cfg: PCConfig) -> Rclone:
    # Downloads only: the PC's own Drive account (download_remote) when set, so they're
    # charged against its own quota instead of the main account's; else the main remote.
    return Rclone(cfg.download_remote or cfg.remote,
                  (*Config().rclone_args, *_bwlimit_args(cfg)), binary=cfg.rclone)


class _Abandon(Exception):
    """Stop working on the current job (Stop now or the server cancelled it)."""


class _LocalFault(Exception):
    """Something at the PC itself is broken (missing tool, full disk, permissions,
    a crashed subprocess launch, ...) -- not this video's fault. The whole worker
    stops; the job is released with no attempt burned, so it's picked up again
    (by this worker once fixed, or another one) rather than failed outright.

    alert, if set, is the message sent to the server; otherwise run() sends a
    generic one, so no local fault leaves the worker stopped silently."""

    def __init__(self, message: str, alert: str | None = None):
        super().__init__(message)
        self.alert = alert


EncodeFn = Callable[[Path, Path, Probe, Path, threading.Event], float]


class PCWorker:
    def __init__(self, cfg: PCConfig, client: JobClient, rclone: Rclone,
                 encode: EncodeFn | None = None, log: Callable[[str], None] = print,
                 download_rclone: Rclone | None = None):
        self.cfg = cfg
        self.client = client
        self.rclone = rclone
        # Downloads only; upload/md5/exists/delete always stay on `rclone` (the main
        # account), since every file must stay owned by the main user.
        self.download_rclone = download_rclone or rclone
        # Sent on every claim/heartbeat/report so the server charges the right budget.
        self.download_account = (cfg.download_account
                                 or ("pc" if cfg.download_remote else "main"))
        self.encode = encode or (self._nvenc if cfg.encoder == "nvenc" else self._x265)
        self.log = log
        self.state = "idle"
        self.stage = ""
        self.current_path: str | None = None
        self.done = 0
        # (bytes downloaded today, daily limit) from the server's last claim/heartbeat
        # answer, for the tray's status; None until the server has told us.
        self.download_today: tuple[float, float] | None = None
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
        first_claim = True
        try:
            while not self._stop_now.is_set() and not self._stop_after.is_set():
                resume = first_claim
                try:
                    # Only passed when set, so the PC's claim is exactly what it always was.
                    extra = {}
                    if self.cfg.disk_check:
                        extra["free_bytes"] = shutil.disk_usage(self.cfg.workdir).free
                    if self.cfg.max_hours > 0:
                        extra["max_hours"] = self.cfg.max_hours
                    resp = self.client.claim(self.cfg.worker_name, self.cfg.encoder,
                                             download_account=self.download_account,
                                             resume=resume, **extra)
                except JobServiceError as e:
                    self.log(f"job service unreachable, retrying: {e}")
                    self._wake.wait(self.cfg.idle_secs)
                    self._wake.clear()
                    continue
                first_claim = False  # only a claim that got an answer uses up resume
                self._note_download_today(resp)
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
                    try:
                        self.client.alert(self.cfg.worker_name,
                                          e.alert or f"Local fault, worker stopped: {e}")
                    except (JobServiceError, LeaseLost):
                        pass  # best-effort: the worker stops either way
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
                    job_id, self.cfg.worker_name, "failed",
                    download_account=self.download_account, reason=tb[:500]))
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
                if (e.rate_limited or e.download_quota or e.auth_failed
                        or i == self.cfg.retry_attempts - 1):
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

    def _note_download_today(self, resp: dict) -> None:
        if "download_used" in resp and "download_limit" in resp:
            self.download_today = (resp["download_used"], resp["download_limit"])

    def _heartbeat(self, job_id: int, stop: threading.Event, cancel: threading.Event,
                   server_cancelled: threading.Event) -> None:
        failing_since = None  # time.monotonic() of the first failure in a row
        while not stop.wait(self.cfg.heartbeat_secs):
            try:
                resp = self.client.heartbeat(job_id, self.cfg.worker_name,
                                              download_account=self.download_account)
                failing_since = None
                self._note_download_today(resp)
                if resp.get("cancel"):
                    self.log("server cancelled the job")
                    server_cancelled.set()
                    cancel.set()
                    return
            except Exception as e:  # never let a heartbeat blip kill the thread
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                if now - failing_since >= self.cfg.lease_secs:
                    # The server has expired our lease by now (and may have handed
                    # the job on): stop, as for a server cancel, so only a tmp that
                    # provably holds our bytes is ever deleted.
                    self.log(f"heartbeats failing for {now - failing_since:.0f} s, past "
                             f"the lease; abandoning the job: {e}")
                    server_cancelled.set()
                    cancel.set()
                    return
                self.log(f"heartbeat failed (will retry): {e}")

    def _nvenc(self, src: Path, out: Path, probe: Probe, log_path: Path,
               cancel: threading.Event) -> float:
        return run_encode_cancellable(build_nvenc_command(src, out, probe, self.cfg.cq),
                                      log_path, cancel)

    def _x265(self, src: Path, out: Path, probe: Probe, log_path: Path,
              cancel: threading.Event) -> float:
        return run_encode_cancellable(build_command(src, out, probe, Config()),
                                      log_path, cancel)

    def _read_marker(self, marker: Path) -> float | None:
        """The encode seconds recorded in a finished-encode marker, or None if
        it's absent or unreadable (treated the same as absent)."""
        try:
            return float(marker.read_text())
        except (OSError, ValueError):
            return None

    def _write_marker(self, marker: Path, content) -> None:
        """Record a successful download/encode atomically: write-then-rename, so
        a crash mid-write never leaves a marker that looks valid but isn't."""
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(str(content))
        os.replace(tmp, marker)

    def _report(self, job: dict, outcome: str, **data) -> None:
        try:
            self._retry_job_call(
                lambda: self.client.report(job["id"], self.cfg.worker_name, outcome,
                                          download_account=self.download_account, **data))
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
                                   download_account=self.download_account,
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
        marker = self.cfg.workdir / f"{jid}.encoded"
        downloaded = self.cfg.workdir / f"{jid}.downloaded"
        self.current_path = job.get("path")
        deleted = clean_workdir(self.cfg.workdir, jid)
        if deleted:
            self.log(f"cleaned stale work files: {', '.join(p.name for p in deleted)}")
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
            secs = self._read_marker(marker) if out.exists() else None
            if secs is not None:
                self.log(f"resuming {path}: reusing finished encode")
            else:
                if downloaded.exists() and src.exists() and src.stat().st_size == job["size"]:
                    self.log(f"resuming {path}: reusing download")
                else:
                    # Normal path. A leftover out without a (valid) marker is an
                    # unfinished encode from a previous run; a leftover src
                    # without a (valid) marker may be a shutdown-truncated
                    # download that happens to sit at the full size (rclone's
                    # local backend preallocates it) -- neither is trustworthy,
                    # so clear them all before starting over.
                    src.unlink(missing_ok=True)
                    downloaded.unlink(missing_ok=True)
                    out.unlink(missing_ok=True)
                    marker.unlink(missing_ok=True)
                    self.stage = "downloading"
                    self.log(f"downloading {path} ({job['size'] / 1e9:.1f} GB)")
                    self._check(cancel)
                    self._retry(lambda: self.download_rclone.download(path, src, cancel=cancel),
                               cancel)
                    self._check(cancel)
                    if src.stat().st_size != job["size"]:
                        self._report(job, "changed")
                        return
                    self._write_marker(downloaded, job["size"])
                self.stage = "encoding"
                self.log(f"encoding {path}")
                secs = self.encode(src, out, probe, log_path, cancel)
                self._write_marker(marker, secs)
            self.stage = "verifying"
            self.log(f"verifying ({secs / 60:.0f} min encode)")
            out_size = out.stat().st_size
            verdict = compare(probe, parse_ffprobe(run_ffprobe(str(out)), out_size),
                              job["size"], out_size, self.cfg.min_saving, decode_errors(out))
            if verdict.problems:
                self.log(f"failed checks: {'; '.join(verdict.problems)}")
                self._report(job, "failed", reason="; ".join(verdict.problems))
                return
            if verdict.not_worth_it:
                self.log(f"not worth it: only {1 - out_size / job['size']:.0%} smaller")
                self._report(job, "not_worth_it",
                             reason=f"only {1 - out_size / job['size']:.0%} smaller")
                return
            md5 = md5_file(out)
            self._check(cancel)
            self.stage = "uploading"
            self.log(f"uploading {out_size / 1e9:.1f} GB "
                     f"({1 - out_size / job['size']:.0%} smaller)")
            uploading = True
            self._retry(lambda: self.rclone.upload(out, tmp, cancel=cancel), cancel)
            if self.rclone.md5(tmp) != md5:
                self._delete_tmp_if_ours(job, tmp, md5)
                self._report(job, "failed", reason="checksum mismatch after upload")
                return
            self._check(cancel)
            self._report_uploaded(job, jid, path, tmp, out_size, md5, secs)
        except (_Abandon, EncodeCancelled, TransferCancelled):
            self._abandon(job, tmp if uploading else None, md5)
        except RcloneError as e:
            if e.auth_failed:
                # The PC's Drive login is dead: every job would fail the same way,
                # so release this one (no attempt used) and stop until it's fixed.
                self._abandon(job, tmp if uploading else None, md5)
                remote = (self.download_rclone if self.stage == "downloading"
                          else self.rclone).remote
                raise _LocalFault(
                    f"Drive login failed (run `rclone config reconnect` "
                    f"for the remote, then Start again): {e}",
                    alert=f"Drive login failed for {remote}; re-authorize that rclone "
                          f"remote, then restart the worker: {e}") from e
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
                self._abandon(job, None, None)
                self._wake.wait(self.cfg.idle_secs)
                self._wake.clear()
            elif e.download_quota:
                self._report(job, "download_quota")
            else:
                self._report(job, "failed", reason=str(e))
        except (EncodeError, ProbeError) as e:
            self._report(job, "failed", reason=str(e))
        except FileNotFoundError as e:
            if e.filename not in (str(src), str(out)):
                self._abandon(job, tmp if uploading else None, md5)
                raise _LocalFault(str(e)) from e
            # A missing work file (e.g. ffmpeg exited 0 without writing the output)
            # is this video's problem, not the PC's: report it, keep working.
            self._report(job, "failed", reason=f"missing work file: {e}")
        except OSError as e:
            # A local problem (missing tool, full disk, permissions, ...), not this
            # video's fault: release it (no attempt burned) and stop the worker.
            self._abandon(job, tmp if uploading else None, md5)
            raise _LocalFault(str(e)) from e
        finally:
            hb_stop.set()
            hb.join(5)
            self._current_cancel = None
            for f in (src, out, log_path, marker, downloaded):
                f.unlink(missing_ok=True)
            self.current_path = None
            self.stage = ""

    def _delete_tmp_if_ours(self, job: dict, tmp: str | None, md5: str | None) -> None:
        if not tmp or not self._safe_tmp(job, tmp):
            return
        if not md5:
            # No local md5 yet means the upload never completed, so there's no
            # complete tmp of ours on Drive to remove.
            self.log(f"no local md5 yet; not deleting {tmp}")
            return
        try:
            if self.rclone.exists(tmp) and self.rclone.md5(tmp) == md5:
                self.rclone.delete(tmp)
        except RcloneError as e:
            self.log(f"could not remove {tmp}: {e}")

    def _abandon(self, job: dict, tmp: str | None, md5: str | None) -> None:
        # Stop now, a server cancel or a local fault: the lease may no longer be ours
        # by now, so only ever remove a tmp that provably holds the bytes we uploaded.
        self._delete_tmp_if_ours(job, tmp, md5)
        try:
            self.client.release(job["id"], self.cfg.worker_name)
        except (LeaseLost, JobServiceError):
            pass
        self.log(f"abandoned: {job['path']}")
