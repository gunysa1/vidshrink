"""The remote worker's side: claim a job, encode it, upload, report. Runs on the
Windows PC (NVENC) and, headless, on a Linux cloud VM (x265, encoder="x265-cloud").
Must stay importable on Windows: no fcntl, no vidshrink.worker."""
import json
import os
import platform
import random
import re
import shutil
import threading
import time
import tomllib
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath

from vidshrink.config import Config
from vidshrink.encode import (EncodeCancelled, EncodeError, build_command,
                              build_nvenc_command, run_encode_cancellable)
from vidshrink.jobs import JobClient, JobServiceError, LeaseLost
from vidshrink.local_queue import LocalJob, LocalQueue, read_uploaded
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
    r"^(\d+)\.uploaded$",
    # The queued job's claim dict, plus its own write-then-rename tmp.
    r"^(\d+)\.job\.json(?:\.tmp)?$",
    # rclone's local backend, when it does write a partial file, names it
    # "<name>.<random>.partial" (e.g. "65137.src.a1b2c3.partial").
    r"^(\d+)\.(?:src|out\.mkv)\..*\.partial$",
    # A marker's own write-then-rename tmp, if a crash lands mid-write.
    r"^(\d+)\.(?:encoded|downloaded|uploaded)\.tmp$",
))
# calibrate's own leftovers: never present while the PC worker loop runs (the two
# never run at once in one process), so always safe to sweep here.
_CALIBRATE_PATTERNS = tuple(re.compile(p) for p in (r"^cal-.*$", r"^cal\.log$"))


def clean_workdir(workdir: Path, keep_ids: set[int]) -> list[Path]:
    """Delete vidshrink's own stale work files from `workdir` -- never a
    subdirectory, never anything whose name doesn't match one of our own
    patterns. Files that belong to any id in `keep_ids` (by the numeric id in their
    name) are kept; everything else matching is removed. Returns what was actually
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
                if int(m.group(1)) in keep_ids:
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
    # Local job queue limits: bytes on disk (sources waiting + outputs not yet
    # uploaded), expected encode hours not yet encoded, and how long a finished
    # download stays reserved for us on the server while we are switched off.
    queue_disk_gb: float = 300.0
    queue_hours: float = 24.0
    reserve_days: float = 7.0


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


# Local states of a held job that is waiting in a stage's queue rather than being
# worked on: a server cancel can drop it at once, since no stage owns it.
_IDLE_STATES = ("downloaded", "encoded", "uploaded")
_LEFTOVER_JOB = re.compile(r"^(\d+)\.job\.json$")


def _parse_resume(resp) -> tuple[dict[int, dict], set[int]]:
    """A /resume reply as ({id: job}, refused ids); ValueError etc. if malformed."""
    if not (isinstance(resp, dict) and isinstance(resp.get("resumed"), list)
            and isinstance(resp.get("refused"), list)
            and all(isinstance(j, dict) for j in resp["resumed"])):
        raise ValueError(f"malformed resume reply: {resp!r:.200}")
    return {int(j["id"]): j for j in resp["resumed"]}, {int(i) for i in resp["refused"]}


class PCWorker:
    """Three stage threads, one job at a time each -- download, encode, then
    verify+upload+report -- with on-disk queues between them (LocalQueue), plus
    one heartbeat thread covering every held job."""

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
        self.done = 0
        # (bytes downloaded today, daily limit) from the server's last claim/heartbeat
        # answer, for the tray's status; None until the server has told us.
        self.download_today: tuple[float, float | None] | None = None
        server = Config()
        # The prefetch limits. The claim filter reads them from here too, so the
        # two can never disagree.
        self.queue = LocalQueue(cfg.workdir, int(cfg.queue_disk_gb * 1e9), cfg.queue_hours,
                                server.disk_factor, server.disk_reserve)
        # After Drive's upload limit the server blocks uploads this long; so do we.
        self.upload_pause_secs = server.rate_limit_block_secs
        self._phase = "new"  # "new", "running", "stopped"
        self._stop_now = threading.Event()
        self._stop_after = threading.Event()
        # Wakes the downloader's idle wait promptly: either stop mode, or the queue
        # changed (a job moved on or was dropped, so there may be room to claim).
        self._wake = threading.Event()
        # Guards everything below plus every local state change, so a server cancel
        # and a stage taking a job never race.
        self._cv = threading.Condition(threading.RLock())
        self._jobs: dict[int, LocalJob] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._reserved: set[int] = set()  # ids the server holds as reservations
        # Ids the server took back (cancel, or our lease ran out): dropped, never kept.
        self._gone: set[int] = set()
        # Ids whose files _forget is deleting outside the lock; _hold waits for them.
        self._deleting: set[int] = set()
        self._resuming = 0  # jobs offered to /resume, until the server answers
        self._to_encode: deque[LocalJob] = deque()  # downloaded, oldest first
        self._to_upload: deque[LocalJob] = deque()  # encoded, oldest first
        self._encoder_done = threading.Event()
        # Uploaded jobs whose "uploaded" report is still owed: id -> (out_size, md5,
        # encode secs), also kept in each one's `<id>.uploaded` marker. The uploader
        # retries the report alone, never the upload, once _reports_due has passed.
        self._unreported: dict[int, tuple[int, str, float]] = {}
        # Those of them resumed after a restart: their tmp on Drive is checked once
        # before the report (the server may have expired it and deleted the tmp).
        self._check_tmp: set[int] = set()
        self._reports_due = 0.0  # time.monotonic()
        self._upload_stage = ""  # "verifying" or "uploading" while the uploader works
        self._uploads_paused_until = 0.0  # time.monotonic() deadline
        self._fault_exc: _LocalFault | None = None
        self._claim_not_before = 0.0  # time.monotonic(); see _back_off_claims
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
        with self._cv:
            if now:
                self._stop_now.set()
                for cancel in self._cancels.values():
                    cancel.set()
            else:
                self._stop_after.set()
                # Stop after aborts the running download (that job is released);
                # the running encode and the uploads carry on.
                for jid, lj in self._jobs.items():
                    if lj.state == "downloading":
                        self._cancels[jid].set()
            self._cv.notify_all()
        self._wake.set()

    def _stopping(self) -> bool:
        return self._stop_now.is_set() or self._stop_after.is_set()

    def _check(self, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise _Abandon()

    def _fault(self, e: _LocalFault) -> None:
        """A local fault in any stage stops every stage; run() sends the alert."""
        self.log(f"stopping: local fault: {e}")
        with self._cv:
            if self._fault_exc is None:
                self._fault_exc = e
        self.stop(now=True)

    def _wait_stop(self, secs: float) -> bool:
        """Wait up to secs, waking early on either stop mode; True iff woken early."""
        end = time.monotonic() + secs
        while (remaining := end - time.monotonic()) > 0:
            if self._stop_now.wait(min(remaining, 0.2)) or self._stop_after.is_set():
                return True
        return False

    def _idle(self, secs: float) -> None:
        """The downloader's wait: ends early on a stop or a queue change."""
        self._wake.wait(secs)
        self._wake.clear()

    # --- status ----------------------------------------------------------------
    @property
    def state(self) -> str:
        if self._phase == "stopped":
            return "stopped"
        if self._phase == "running" and self._stop_after.is_set():
            return "finishing"
        return "working" if self._snapshot() else "idle"

    def _snapshot(self) -> list[LocalJob]:
        with self._cv:
            return list(self._jobs.values())

    def _focus(self) -> tuple[str, LocalJob | None]:
        """The encoder's job, or the most active stage's when nothing encodes."""
        jobs = self._snapshot()
        for state in ("encoding", "uploading", "downloading"):
            for lj in jobs:
                if lj.state == state:
                    stage = (self._upload_stage or state) if state == "uploading" else state
                    return stage, lj
        return ("waiting for work" if self._phase == "running" else ""), None

    @property
    def stage(self) -> str:
        """Compatibility for the tray: see _focus."""
        return self._focus()[0]

    @property
    def current_path(self) -> str | None:
        lj = self._focus()[1]
        return lj.job.get("path") if lj is not None else None

    def status_line(self) -> str:
        """Every stage at once, e.g. "Encoding X (3 waiting) · Uploading Y"."""
        jobs = self._snapshot()

        def name(lj: LocalJob) -> str:
            return PurePosixPath(lj.job.get("path", "")).name

        def having(state: str) -> list[LocalJob]:
            return [lj for lj in jobs if lj.state == state]

        parts = []
        if self._resuming:
            parts.append(f"Resuming {self._resuming} job(s)")
        waiting = len(having("downloaded"))
        encoding = having("encoding")
        if encoding:
            parts.append(f"Encoding {name(encoding[0])}"
                         + (f" ({waiting} waiting)" if waiting else ""))
        elif waiting:
            parts.append(f"{waiting} downloaded waiting")
        parts += [f"Downloading {name(lj)}" for lj in having("downloading")]
        parts += [f"{(self._upload_stage or 'uploading').title()} {name(lj)}"
                  for lj in having("uploading")]
        encoded = len(having("encoded"))
        if encoded and time.monotonic() < self._uploads_paused_until:
            parts.append(f"Uploads paused ({encoded} waiting)")
        elif encoded:
            parts.append(f"{encoded} encoded waiting")
        unreported = len(having("uploaded"))
        if unreported:
            parts.append(f"{unreported} uploaded, report pending")
        if not parts and self._phase == "running":
            parts.append("Waiting for work")
        return " · ".join(parts)

    # --- startup --------------------------------------------------------------
    def _missing_tools(self) -> list[str]:
        # ffmpeg_dir (if set) was already prepended to PATH in __init__, so a plain
        # PATH lookup covers "in ffmpeg_dir if set, else PATH".
        return [name for name in ("ffmpeg", "ffprobe", self.cfg.rclone)
                if shutil.which(name) is None]

    def _startup(self) -> bool:
        """Pick up where a previous run left off: partial downloads are released and
        deleted, finished downloads and encodes are offered to the server and, once
        confirmed, queued at their local state. False if Stop was pressed before the
        server answered; nothing is deleted then."""
        self.cfg.workdir.mkdir(parents=True, exist_ok=True)
        try:
            names = [p.name for p in self.cfg.workdir.iterdir()]
        except OSError:
            names = []
        resumable, incomplete = self.queue.scan()
        offered = []
        for lj in resumable:
            # The server rejects an item without these; treat it like a partial one.
            if isinstance(lj.job.get("size"), int) and isinstance(lj.job.get("modtime"), str):
                offered.append(lj)
            else:
                self.queue.forget(lj.id)
                incomplete.append(lj.id)
        # A .job.json that can't be read still names the lease to release.
        unreadable = {int(m.group(1)) for name in names if (m := _LEFTOVER_JOB.match(name))}
        unreadable -= {lj.id for lj in resumable} | set(incomplete)
        for jid in (*incomplete, *sorted(unreadable)):
            try:
                self.client.release(jid, self.cfg.worker_name)
            except (LeaseLost, JobServiceError):
                pass  # not ours any more, or the server is down: it expires
            self.queue.forget(jid)
        unlisted: set[int] = set()
        if offered and (unlisted := self._resume(offered)) is None:
            return False
        with self._cv:
            held = set(self._jobs)
        deleted = clean_workdir(self.cfg.workdir, held | unlisted)
        if deleted:
            self.log(f"cleaned stale work files: {', '.join(p.name for p in deleted)}")
        return True

    def _resume(self, offered: list[LocalJob]) -> set[int] | None:
        """Ask the server to confirm `offered`, retrying until it gives a proper
        answer; None if Stop is pressed first. Local files are deleted only for an
        explicit refusal. Returns the ids the answer named neither way: they are
        left on disk, untouched, and not worked on."""
        items = [{"id": lj.id, "size": lj.job["size"], "modtime": lj.job["modtime"],
                  "reserve_secs": self.cfg.reserve_days * 86400} for lj in offered]
        self._resuming = len(offered)
        try:
            while True:
                try:
                    resp = self.client.resume(self.cfg.worker_name, self.cfg.encoder,
                                              self.download_account, items)
                    confirmed, refused = _parse_resume(resp)
                    break
                except Exception as e:
                    # Anything at all -- unreachable, a 4xx, LeaseLost, a garbled
                    # reply: nothing has been deleted yet, so just ask again. Ending
                    # run() here would stop the cloud worker for good.
                    self.log(f"resume failed, retrying: {type(e).__name__}: {e}")
                    if self._wait_stop(self.cfg.idle_secs):
                        return None
        finally:
            self._resuming = 0
        unlisted = set()
        for lj in offered:
            job = confirmed.get(lj.id)
            if job is None:
                if lj.id in refused:
                    self.log(f"resume refused, deleting: {lj.job.get('path')}")
                    self.queue.forget(lj.id)
                else:
                    self.log(f"resume neither resumed nor refused {lj.job.get('path')}; "
                             f"leaving its files for the next start")
                    self.queue.drop(lj.id)
                    unlisted.add(lj.id)
                continue
            lj.job.update(job)  # the server's fresh view, e.g. expected_hours
            info = (read_uploaded(self.cfg.workdir / f"{lj.id}.uploaded")
                    if lj.state == "uploaded" else None)
            if lj.state == "uploaded" and info is None:
                lj.state = "encoded"  # the marker went bad since scan(): upload again
            with self._cv:
                self._jobs[lj.id] = lj
                self._cancels[lj.id] = threading.Event()
                self._reserved.add(lj.id)
                if info is not None:
                    # The uploader reports it (see _report_pending), no queue needed.
                    self._unreported[lj.id] = (info["out_size"], info["md5"],
                                               info["encode_secs"])
                    self._check_tmp.add(lj.id)
            if lj.state != "uploaded":
                self._push(self._to_encode if lj.state == "downloaded" else self._to_upload,
                           lj)
            self.log(f"resumed ({lj.state}): {lj.job.get('path')}")
        return unlisted

    def _on_stopped(self) -> None:
        """Every stage has exited. A job that never finished downloading is released
        and deleted; every finished download or encode stays on disk, reserved for
        the next start."""
        kept = 0
        for lj in self._snapshot():
            with self._cv:
                gone = lj.id in self._gone
            if lj.state == "downloading":
                self._abandon(lj.job, None, None)
                self._forget(lj)
            elif gone:
                self._forget(lj)
            else:
                kept += 1
        if kept:
            self.log(f"stopped; keeping {kept} job(s) on disk, reserved for next time")

    # --- loop ---------------------------------------------------------------
    def run(self) -> None:
        """Returns once every stage thread has exited."""
        missing = self._missing_tools()
        if missing:
            self.log(f"required tool(s) not found on PATH: {', '.join(missing)}")
            self._phase = "stopped"
            return
        self._phase = "running"
        try:
            if not self._startup():
                return
            stages = [threading.Thread(target=self._stage, args=(fn,), daemon=True,
                                       name=f"vidshrink{fn.__name__}")
                      for fn in (self._downloader, self._encoder, self._uploader)]
            hb_stop = threading.Event()
            hb = threading.Thread(target=self._heartbeat, args=(hb_stop,), daemon=True,
                                  name="vidshrink_heartbeat")
            for t in (*stages, hb):
                t.start()
            for t in stages:
                t.join()
            hb_stop.set()
            hb.join(5)
            self._on_stopped()
            e = self._fault_exc
            if e is not None:
                try:
                    self.client.alert(self.cfg.worker_name,
                                      e.alert or f"Local fault, worker stopped: {e}")
                except (JobServiceError, LeaseLost):
                    pass  # best-effort: the worker stops either way
        finally:
            self._phase = "stopped"

    def _stage(self, loop: Callable[[], None]) -> None:
        try:
            loop()
        except Exception as e:  # a bug in the loop itself, not in one job
            tb = "".join(traceback.format_exception_only(type(e), e)).strip()
            self._fault(_LocalFault(f"{loop.__name__} crashed: {tb}"))

    def _run_job(self, lj: LocalJob, step: Callable[[LocalJob], None]) -> None:
        """One stage's work on one job. A local fault stops every stage; anything
        unanticipated goes through the poison-job guard for this job alone."""
        try:
            step(lj)
        except _LocalFault as e:
            self._fault(e)
        except Exception as e:
            # Back off first: forgetting wakes the downloader. And forget before
            # the release, after which this id may be claimed again.
            self._back_off_claims()
            self._forget(lj)
            self._handle_unexpected(lj.job, e)

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
        self._back_off_claims()

    def _back_off_claims(self) -> None:
        """Hold the next claim for idle_secs, so a failing job isn't re-claimed in
        a tight loop; the other stages carry on meanwhile."""
        self._claim_not_before = time.monotonic() + self.cfg.idle_secs

    # --- held jobs -----------------------------------------------------------
    def _hold(self, job: dict) -> LocalJob:
        with self._cv:
            while int(job["id"]) in self._deleting:  # its old files are going
                self._cv.wait(0.5)
            lj = self.queue.add(job)
            cancel = threading.Event()
            if self._stopping():
                # A stop landed while the claim was in flight: release, don't download.
                cancel.set()
            self._jobs[lj.id] = lj
            self._cancels[lj.id] = cancel
        return lj

    def _held(self, lj: LocalJob) -> bool:
        return self._jobs.get(lj.id) is lj

    def _cancel_of(self, lj: LocalJob) -> threading.Event:
        with self._cv:
            if self._held(lj):
                return self._cancels[lj.id]
        gone = threading.Event()
        gone.set()
        return gone

    def _set_state(self, lj: LocalJob, state: str) -> bool:
        """False if the job was dropped meanwhile (a server cancel). A job the
        server cancelled while its stage was finishing is forgotten here, at once."""
        with self._cv:
            if not self._held(lj):
                return False
            gone = lj.id in self._gone
            if not gone:
                try:
                    self.queue.set_state(lj.id, state)
                except KeyError:
                    return False
        if gone:
            self._forget(lj)
            return False
        self._wake.set()
        return True

    def _forget(self, lj: LocalJob) -> None:
        """Drop the job locally and delete its files. A no-op if it is already gone
        (it may even have been claimed again since, under the same id). The files
        are deleted outside the lock; _hold waits for that before reusing the id.
        Never call this while holding _cv."""
        with self._cv:
            if not self._held(lj):
                return
            del self._jobs[lj.id]
            self._cancels.pop(lj.id, None)
            self._reserved.discard(lj.id)
            self._gone.discard(lj.id)
            self._unreported.pop(lj.id, None)
            self._check_tmp.discard(lj.id)
            self._deleting.add(lj.id)
        try:
            self.queue.forget(lj.id)
        finally:
            with self._cv:
                self._deleting.discard(lj.id)
                self._cv.notify_all()
        self._wake.set()

    def _push(self, queue: deque, lj: LocalJob, front: bool = False) -> None:
        with self._cv:
            if front:
                queue.appendleft(lj)
            else:
                queue.append(lj)
            self._cv.notify_all()

    def _take(self, queue: deque, state: str) -> LocalJob | None:
        """The next job still held and not cancelled, moved to `state`; else wait
        briefly for one and return None."""
        dropped = []
        with self._cv:
            while queue:
                lj = queue.popleft()
                if not self._held(lj):
                    continue
                if lj.id in self._gone:
                    dropped.append(lj)  # the server cancelled it as it was queued
                    continue
                if self._cancels[lj.id].is_set():
                    continue  # stopping: it stays held, on disk
                try:
                    self.queue.set_state(lj.id, state)
                except KeyError:
                    continue
                self._wake.set()  # the claim filter depends on what waits behind
                return lj
            if not dropped:
                self._cv.wait(0.5)
        for lj in dropped:
            self._forget(lj)
        return None

    def _server_cancel(self, job_id: int, why: str) -> None:
        """The server no longer holds job_id for us: cancel only that job's stage
        work. A job waiting in a queue is dropped here; a stage drops its own."""
        with self._cv:
            lj = self._jobs.get(job_id)
            if lj is None:
                return
            self.log(f"{why}: {lj.job.get('path')}")
            self._gone.add(job_id)
            self._cancels[job_id].set()
            idle = lj.state in _IDLE_STATES
        if idle:
            self._forget(lj)

    # --- download stage -------------------------------------------------------
    def _free_bytes(self) -> int:
        return shutil.disk_usage(self.cfg.workdir).free

    def _claim_filters(self) -> dict | None:
        """Extra claim arguments, or None while the queue is full. With the queue
        empty they are exactly what they always were; otherwise free_bytes and
        max_hours shrink to the room left under the queue's own limits."""
        jobs = self._snapshot()
        extra = {}
        if self.cfg.max_hours > 0:
            extra["max_hours"] = self.cfg.max_hours
        if not jobs:
            if self.cfg.disk_check:
                extra["free_bytes"] = self._free_bytes()
            return extra
        # Both count the output the held encodes still have to write.
        room = self.queue.queue_disk_bytes - self.queue.on_disk()
        if room <= 0:
            return None
        extra["free_bytes"] = max(0, min(self._free_bytes() - self.queue.pending_output(),
                                         room))
        # The hours limit applies only behind the encoder (see LocalQueue.admits).
        if any(lj.state in ("downloading", "downloaded") for lj in jobs):
            hours = sum(float(lj.job.get("expected_hours") or 0.0) for lj in jobs
                        if lj.state in ("downloading", "downloaded", "encoding"))
            remaining = self.queue.queue_hours - hours
            if remaining <= 0:
                return None
            extra["max_hours"] = min(remaining, extra.get("max_hours", remaining))
        return extra

    def _admits(self, job: dict) -> bool:
        if not self._snapshot():
            return True  # the first job into an empty queue always fits
        return self.queue.admits(job["size"], float(job.get("expected_hours") or 0.0),
                                 self._free_bytes())

    def _downloader(self) -> None:
        while not self._stopping():
            backoff = self._claim_not_before - time.monotonic()
            if backoff > 0:
                self._wait_stop(backoff)
                continue
            extra = self._claim_filters()
            if extra is None:
                self._idle(self.cfg.idle_secs)  # full: wait for a job to move on
                continue
            try:
                resp = self.client.claim(self.cfg.worker_name, self.cfg.encoder,
                                         download_account=self.download_account,
                                         **extra)
            except JobServiceError as e:
                self.log(f"job service unreachable, retrying: {e}")
                self._idle(self.cfg.idle_secs)
                continue
            self._note_download_today(resp)
            job = resp.get("job")
            if job is None:
                self._idle(min(resp.get("retry_after") or self.cfg.idle_secs,
                               self.cfg.idle_secs))
                continue
            if self._stopping() or not self._admits(job):
                # Stopped meanwhile, or the queue filled up after the filter was
                # computed: hand it straight back.
                try:
                    self.client.release(job["id"], self.cfg.worker_name)
                except (LeaseLost, JobServiceError):
                    pass
                if not self._stopping():
                    self._idle(self.cfg.idle_secs)
                continue
            try:
                lj = self._hold(job)
            except OSError as e:  # can't even write its .job.json: a local fault
                try:
                    self.client.release(job["id"], self.cfg.worker_name)
                except (LeaseLost, JobServiceError):
                    pass
                self._fault(_LocalFault(str(e)))
                return
            self._run_job(lj, self._download)

    def _download(self, lj: LocalJob) -> None:
        job, src = lj.job, lj.src
        cancel = self._cancel_of(lj)
        downloaded = self.cfg.workdir / f"{lj.id}.downloaded"
        try:
            path = job["path"]
            # A fresh claim: nothing already on disk under this id is trustworthy.
            for f in (src, downloaded, lj.out, self.cfg.workdir / f"{lj.id}.encoded"):
                f.unlink(missing_ok=True)
            self.log(f"downloading {path} ({job['size'] / 1e9:.1f} GB)")
            self._check(cancel)
            self._retry(lambda: self.download_rclone.download(path, src, cancel=cancel),
                        cancel)
            self._check(cancel)
            if src.stat().st_size != job["size"]:
                self._report(job, "changed")
                self._forget(lj)
                return
            self._write_marker(downloaded, job["size"])
        except Exception as e:
            self._job_error(lj, e, remote=self.download_rclone.remote)
            return
        self._report_downloaded(lj)

    def _report_downloaded(self, lj: LocalJob) -> None:
        """Turn the lease into a reservation, then hand the job to the encoder."""
        try:
            self._retry_job_call(lambda: self.client.report(
                lj.id, self.cfg.worker_name, "downloaded",
                download_account=self.download_account,
                reserve_secs=self.cfg.reserve_days * 86400))
        except LeaseLost:
            self.log(f"lease lost after downloading, dropping: {lj.job['path']}")
            self._forget(lj)
            return
        except JobServiceError as e:
            # Keep it: the next heartbeat tells us if the lease is gone.
            self.log(f"could not report downloaded for {lj.job['path']}: {e}")
        else:
            with self._cv:
                if self._held(lj):
                    self._reserved.add(lj.id)
        if self._set_state(lj, "downloaded"):
            self._push(self._to_encode, lj)

    # --- encode stage ---------------------------------------------------------
    def _encoder(self) -> None:
        try:
            # Stop after: the encode already running finishes; no new one starts.
            while not self._stopping():
                lj = self._take(self._to_encode, "encoding")
                if lj is not None:
                    self._run_job(lj, self._encode_one)
        finally:
            self._encoder_done.set()
            with self._cv:
                self._cv.notify_all()

    def _encode_one(self, lj: LocalJob) -> None:
        job = lj.job
        cancel = self._cancel_of(lj)
        try:
            probe = Probe(**job["probe"])
            self.log(f"encoding {job['path']}")
            self._check(cancel)
            secs = self.encode(lj.src, lj.out, probe, lj.log, cancel)
            self._write_marker(self.cfg.workdir / f"{lj.id}.encoded", secs)
            # Only the output and its marker are needed from here on (verify reads
            # the probe from the job), so free the source's disk at once.
            lj.src.unlink(missing_ok=True)
        except Exception as e:
            self._job_error(lj, e)
            return
        if self._set_state(lj, "encoded"):
            self._push(self._to_upload, lj)

    # --- upload stage ---------------------------------------------------------
    def _uploader(self) -> None:
        while not self._stop_now.is_set():
            self._retry_reports()
            if self._stop_after.is_set():
                with self._cv:
                    if self._encoder_done.is_set() and not self._to_upload:
                        return  # Stop after: every encoded file is uploaded
            paused = self._uploads_paused_until - time.monotonic()
            if paused > 0:
                if self._stop_after.is_set():
                    self.log("uploads are paused; stopping, the encoded files stay reserved")
                    return
                self._stop_now.wait(min(paused, 0.5))
                continue
            lj = self._take(self._to_upload, "uploading")
            if lj is not None:
                self._run_job(lj, self._upload_one)

    def _upload_one(self, lj: LocalJob) -> None:
        job, jid, out = lj.job, lj.id, lj.out
        cancel = self._cancel_of(lj)
        uploading = False
        md5 = None
        tmp = None
        try:
            path, tmp = job["path"], job["tmp_path"]
            probe = Probe(**job["probe"])
            secs = self._read_marker(self.cfg.workdir / f"{jid}.encoded")
            if secs is None:
                raise FileNotFoundError(2, "no finished-encode marker for", str(out))
            self._upload_stage = "verifying"
            self.log(f"verifying ({secs / 60:.0f} min encode)")
            out_size = out.stat().st_size
            verdict = compare(probe, parse_ffprobe(run_ffprobe(str(out)), out_size),
                              job["size"], out_size, self.cfg.min_saving, decode_errors(out))
            if verdict.problems:
                self.log(f"failed checks: {'; '.join(verdict.problems)}")
                self._report(job, "failed", reason="; ".join(verdict.problems))
                self._forget(lj)
                return
            if verdict.not_worth_it:
                self.log(f"not worth it: only {1 - out_size / job['size']:.0%} smaller")
                self._report(job, "not_worth_it",
                             reason=f"only {1 - out_size / job['size']:.0%} smaller")
                self._forget(lj)
                return
            md5 = md5_file(out)
            self._check(cancel)
            self._upload_stage = "uploading"
            self.log(f"uploading {out_size / 1e9:.1f} GB "
                     f"({1 - out_size / job['size']:.0%} smaller)")
            uploading = True
            self._retry(lambda: self.rclone.upload(out, tmp, cancel=cancel), cancel)
            if self.rclone.md5(tmp) != md5:
                self._delete_tmp_if_ours(job, tmp, md5)
                self._report(job, "failed", reason="checksum mismatch after upload")
                self._forget(lj)
                return
            self._check(cancel)
            if self._report_uploaded(job, jid, path, tmp, out_size, md5, secs) == "pending":
                self._keep_unreported(lj, out_size, md5, secs)
                return
            self._forget(lj)
        except Exception as e:
            if (isinstance(e, RcloneError) and e.rate_limited and uploading
                    and not e.auth_failed):
                self._pause_uploads(lj, tmp, md5)
            else:
                self._job_error(lj, e, tmp=tmp, md5=md5, uploading=uploading,
                                remote=self.rclone.remote)
        finally:
            self._upload_stage = ""

    def _keep_unreported(self, lj: LocalJob, out_size: int, md5: str, secs: float) -> None:
        """The upload is done but the server hasn't confirmed the report: keep the
        job, held and heartbeated, and retry only the report later -- now, or on
        the next start. Never upload again: the server may be swapping that tmp."""
        marker = self.cfg.workdir / f"{lj.id}.uploaded"
        try:
            self._write_marker(marker, json.dumps(
                {"out_size": out_size, "md5": md5, "encode_secs": secs}))
        except OSError as e:
            # Still retried from memory; only a restart would lose it.
            self.log(f"could not write {marker.name}: {e}")
        with self._cv:
            if self._held(lj):
                self._unreported[lj.id] = (out_size, md5, secs)
        self._reports_due = time.monotonic() + self.cfg.idle_secs
        if self._set_state(lj, "uploaded"):
            self.log(f"keeping {lj.job['path']}; will retry its uploaded report")

    def _retry_reports(self) -> None:
        """Once due, try each owed "uploaded" report once; see _report_pending."""
        if time.monotonic() < self._reports_due:
            return
        with self._cv:
            pending = [lj for lj in self._jobs.values()
                       if lj.state == "uploaded" and lj.id in self._unreported
                       and lj.id not in self._gone]
        if not pending:
            return
        self._reports_due = time.monotonic() + self.cfg.idle_secs
        for lj in pending:
            if self._stop_now.is_set():
                return
            self._run_job(lj, self._report_pending)

    def _report_pending(self, lj: LocalJob) -> None:
        job = lj.job
        with self._cv:
            if not self._held(lj) or lj.id not in self._unreported:
                return
            out_size, md5, secs = self._unreported[lj.id]
            check = lj.id in self._check_tmp
        tmp = job["tmp_path"]
        if check:
            # Resumed after a restart, so the row is leased to us again and the
            # server isn't swapping it. Its tmp is still ours unless the server
            # expired the reservation meanwhile and deleted it: then upload again.
            try:
                ours = self.rclone.exists(tmp) and self.rclone.md5(tmp) == md5
            except RcloneError as e:
                self.log(f"could not check {tmp}, will retry: {e}")
                return
            with self._cv:
                self._check_tmp.discard(lj.id)
                if not ours:
                    self._unreported.pop(lj.id, None)
            if not ours:
                self.log(f"{tmp} is no longer ours; uploading again: {job['path']}")
                (self.cfg.workdir / f"{lj.id}.uploaded").unlink(missing_ok=True)
                if self._set_state(lj, "encoded"):
                    self._push(self._to_upload, lj)
                return
        # Right after a resume the first attempt is unambiguous (the earlier report
        # can't have committed, or the row wouldn't be leased to us); otherwise an
        # earlier attempt already failed ambiguously.
        result = self._report_uploaded(job, lj.id, job["path"], tmp, out_size, md5, secs,
                                       ambiguous=not check, attempts=1)
        if result != "pending":
            self._forget(lj)

    def _pause_uploads(self, lj: LocalJob, tmp: str | None, md5: str | None) -> None:
        """Drive's upload limit: keep the encoded file and the lease, tell the server
        (which blocks uploads for everyone), and retry only after the block."""
        job = lj.job
        self._delete_tmp_if_ours(job, tmp, md5)
        self.log(f"Drive's upload limit hit; uploads paused for "
                 f"{self.upload_pause_secs / 3600:.0f} h, keeping {job['path']}")
        try:
            self._retry_job_call(lambda: self.client.report(
                lj.id, self.cfg.worker_name, "upload_paused",
                download_account=self.download_account))
        except LeaseLost:
            self.log(f"lease lost reporting upload_paused, dropping: {job['path']}")
            self._forget(lj)
            return
        except JobServiceError as e:
            self.log(f"could not report upload_paused for {job['path']}: {e}")
        self._uploads_paused_until = time.monotonic() + self.upload_pause_secs
        if self._set_state(lj, "encoded"):
            self._push(self._to_upload, lj, front=True)

    # --- per-job outcomes -------------------------------------------------------
    def _job_error(self, lj: LocalJob, e: Exception, *, tmp: str | None = None,
                   md5: str | None = None, uploading: bool = False,
                   remote: str = "") -> None:
        """What a stage does with an exception from one job, as the one-job worker
        always did. Raises _LocalFault to stop every stage; re-raises anything
        unanticipated for the poison-job guard."""
        job = lj.job
        ours = tmp if uploading else None
        if isinstance(e, (_Abandon, EncodeCancelled, TransferCancelled)):
            if self._keep_on_stop(lj, ours, md5):
                return
            self._abandon(job, ours, md5)
        elif isinstance(e, RcloneError):
            if e.auth_failed:
                # The PC's Drive login is dead: every job would fail the same way,
                # so release this one (no attempt used) and stop until it's fixed.
                self._abandon(job, ours, md5)
                self._forget(lj)
                raise _LocalFault(
                    f"Drive login failed (run `rclone config reconnect` "
                    f"for the remote, then Start again): {e}",
                    alert=f"Drive login failed for {remote}; re-authorize that rclone "
                          f"remote, then restart the worker: {e}") from e
            if e.rate_limited:
                # Surprising on a download (the limit is normally hit on upload),
                # but still not this video's fault: release and back off like any
                # other transient blip, without burning an attempt or touching any
                # tmp (nothing was uploaded yet).
                self.log(f"rate limited during download (unexpected); releasing "
                         f"and backing off: {job['path']}")
                self._abandon(job, None, None)
                self._forget(lj)
                self._back_off_claims()
                return
            if e.download_quota:
                self._report(job, "download_quota")
            else:
                self._report(job, "failed", reason=str(e))
        elif isinstance(e, (EncodeError, ProbeError)):
            self._report(job, "failed", reason=str(e))
        elif isinstance(e, FileNotFoundError) and e.filename in (str(lj.src), str(lj.out)):
            # A missing work file (e.g. ffmpeg exited 0 without writing the output)
            # is this video's problem, not the PC's: report it, keep working.
            self._report(job, "failed", reason=f"missing work file: {e}")
        elif isinstance(e, OSError):
            # A local problem (missing tool, full disk, permissions, ...), not this
            # video's fault: release it (no attempt burned) and stop the worker.
            self._abandon(job, ours, md5)
            self._forget(lj)
            raise _LocalFault(str(e)) from e
        else:
            raise e
        self._forget(lj)

    def _keep_on_stop(self, lj: LocalJob, tmp: str | None, md5: str | None) -> bool:
        """A stop (not a server cancel) interrupted a job whose download had
        finished: drop only the cancelled stage's partial work and keep the job on
        disk, still reserved, at its last finished state. False otherwise."""
        with self._cv:
            if (not self._stopping() or not self._held(lj) or lj.id in self._gone
                    or lj.state == "downloading"):
                return False
        if lj.state == "encoding":
            lj.out.unlink(missing_ok=True)
            back = "downloaded"
        else:  # uploading (verifying, or the transfer itself)
            self._delete_tmp_if_ours(lj.job, tmp, md5)
            back = "encoded"
        if self._set_state(lj, back):
            self.log(f"stopped; keeping {lj.job['path']} ({back}) for next time")
        return True

    # --- helpers ----------------------------------------------------------------
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
        # An uncapped account sends download_used without download_limit.
        if "download_used" in resp:
            self.download_today = (resp["download_used"], resp.get("download_limit"))

    def _heartbeat(self, stop: threading.Event) -> None:
        """One heartbeat_many for every held job, every heartbeat_secs, until stop."""
        failing_since = None  # time.monotonic() of the first failure in a row
        while not stop.wait(self.cfg.heartbeat_secs):
            with self._cv:
                ids = sorted(self._jobs)
                reserved = set(self._reserved)
            if not ids:
                failing_since = None
                continue
            try:
                resp = self.client.heartbeat_many(ids, self.cfg.worker_name,
                                                  download_account=self.download_account)
                failing_since = None
                self._note_download_today(resp)
                for job_id in resp.get("cancel") or ():
                    self._server_cancel(job_id, "server cancelled the job")
            except Exception as e:  # never let a heartbeat blip kill the thread
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                down = now - failing_since
                for job_id in ids:
                    # By now the server has expired this lease (or reservation)
                    # and may have handed the job on: drop it, as for a server
                    # cancel, so only a tmp that provably holds our bytes is ever
                    # deleted.
                    limit = (self.cfg.reserve_days * 86400 if job_id in reserved
                             else self.cfg.lease_secs)
                    if down >= limit:
                        self._server_cancel(job_id, f"heartbeats failing for {down:.0f} s, "
                                                    f"past the lease; abandoning the job: {e}")
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
                         md5: str, secs: float, ambiguous: bool = False,
                         attempts: int | None = None) -> str:
        """Report the completed upload, retrying on a transient JobServiceError.
        Once ANY attempt in this call has failed ambiguously (status None, 5xx,
        timeout, or a connection error -- i.e. we don't know if the server actually
        committed it), a later LeaseLost no longer proves the tmp is safe to
        delete: the server may have accepted our first attempt and already moved
        the row on (report() is idempotent for exactly this retry), so deleting
        the tmp then would destroy the only copy of a successful upload. Only an
        UNAMBIGUOUS LeaseLost on the very first attempt (no prior ambiguity) means
        it's safe to clean up our own tmp; pass ambiguous=True when an earlier call
        already failed.

        Returns "done" (reported), "lost" (LeaseLost: the job isn't ours any more)
        or "pending" (gave up for now: the caller keeps the job and retries)."""
        attempts = attempts or self.cfg.retry_attempts
        for i in range(attempts):
            try:
                self.client.report(jid, self.cfg.worker_name, "uploaded",
                                   download_account=self.download_account,
                                   out_size=out_size, out_md5=md5, encode_secs=secs)
                self.done += 1
                self.log(f"uploaded {tmp} ({out_size / 1e9:.1f} GB)")
                return "done"
            except LeaseLost:
                if not ambiguous:
                    self._delete_tmp_if_ours(job, tmp, md5)
                else:
                    self.log(f"lease lost reporting uploaded after an earlier "
                             f"ambiguous failure; leaving {tmp} for the server to "
                             f"reconcile: {job['path']}")
                return "lost"
            except JobServiceError as e:
                ambiguous = True
                transient = e.status is None or e.status >= 500
                if not transient or i == attempts - 1:
                    self.log(f"could not report uploaded for {path}: {e}; "
                             f"keeping the job to retry the report")
                    return "pending"
                if self._stop_now.wait(self.cfg.retry_base_delay * 2 ** i):
                    self.log(f"stop now while retrying the uploaded report; keeping "
                             f"the job to retry the report next time: {job['path']}")
                    return "pending"
        return "pending"

    def _safe_tmp(self, job: dict, tmp: str) -> bool:
        """Defense in depth: never touch a remote path that isn't provably the
        tmp file this job owns."""
        if tmp != tmp_path(job["path"]) or not tmp.endswith(TMP_SUFFIX):
            self.log(f"refusing to delete suspicious tmp path: {tmp!r}")
            return False
        return True

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
