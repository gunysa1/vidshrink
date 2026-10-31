import fcntl
import shutil
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.encode import EncodeError, build_command, run_encode
from vidshrink.jobs import BUSY_STAGES, PC_SEEN_KEY, JobStore
from vidshrink.paths import final_path, is_valid_md5, md5_file, tmp_path  # re-exported for callers/tests
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, with_retries
from vidshrink.transfer import Transfer
from vidshrink.verify import audio_errors, compare, decode_errors


INTERRUPTED_PREFIX = "interrupted during "


class AlreadyRunning(Exception):
    pass


class SwapError(Exception):
    pass


class DiskTooSmall(Exception):
    pass


@contextmanager
def _lock(workdir: Path):
    workdir.mkdir(parents=True, exist_ok=True)
    with open(workdir / ".lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(str(workdir)) from None
        yield


class Worker:
    def __init__(self, conn, rclone: Rclone, cfg: Config, budget: Budget,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic,
                 download_budget: Budget | None = None):
        self.conn = conn
        self.clock = clock
        self.rclone = rclone
        self.cfg = cfg
        self.budget = budget
        # Whole-file downloads count against Drive's daily download quota, which the
        # Plex mount on the same account also needs.
        self.download_budget = download_budget or Budget(
            conn, cfg.daily_download_limit, budget.now, kind="download")
        self.sleep = sleep
        self.log = log
        self.jobs = JobStore(conn, cfg, rclone, now=budget.now, log=log)
        self.failures = 0  # consecutive download/encode/upload failures with no success between
        self._remote_fail_at = None  # self.clock() of the last remote_ready check we couldn't
        # complete (RcloneError), so we don't hammer Drive every loop iteration for it
        self._done_during_encode = 0  # rows finished (swapped) via _encode_tick()
        self._dry_run = False  # set for the duration of run(); guards _encode_tick's swap
        # Background transfers, as (row snapshot, Transfer): at most one download
        # (fetching the next file) and one upload. Only the main thread records them.
        self._down: tuple | None = None
        self._up: tuple | None = None
        self._deferred: Exception | None = None  # raised by a transfer finished in a tick;
        # re-raised by the main loop once the encode returns, as it would have been there

    # --- work files -------------------------------------------------------
    def _src(self, vid: int) -> Path:
        return self.cfg.workdir / f"{vid}.src"

    def _out(self, vid: int) -> Path:
        return self.cfg.workdir / f"{vid}.out.mkv"

    def _logfile(self, vid: int) -> Path:
        return self.cfg.workdir / f"{vid}.log"

    def _cleanup(self, vid: int, keep_output: bool = False) -> None:
        self._src(vid).unlink(missing_ok=True)
        self._logfile(vid).unlink(missing_ok=True)
        if not keep_output:
            self._out(vid).unlink(missing_ok=True)

    def _retry(self, fn):
        return with_retries(fn, self.cfg.retry_attempts, self.cfg.retry_base_delay, self.sleep)

    def _fail(self, row, reason: str, retry_stage: str = "queued",
              count_failure: bool = True) -> None:
        attempts = row["attempts"] + 1
        final = attempts >= self.cfg.max_attempts
        self._cleanup(row["id"], keep_output=(retry_stage == "ready" and not final))
        stage = "failed" if final else retry_stage
        db.mark(self.conn, row["id"], stage, reason=reason[:500], attempts=attempts)
        if count_failure:
            self.failures += 1
        self.log(f"{stage}: {row['path']}: {reason[:200]}")

    def _tools(self) -> tuple[str, ...]:
        return (self.rclone.binary, "ffmpeg", "ffprobe")

    def _check_tools(self) -> None:
        for tool in self._tools():
            if shutil.which(tool) is None:
                raise RuntimeError(f"required tool not found on PATH: {tool}")

    def _pc_active(self) -> bool:
        seen = float(db.kv_get(self.conn, PC_SEEN_KEY, "0"))
        return self.budget.now() - seen < self.cfg.pc_absent_days * 86400

    def _next_job(self):
        """Best queued row for the server, leaving long jobs to the PC while it's
        around, and skipping rows that share a final_path/tmp_path stem with another
        row currently busy (so the two can't race over the same remote file)."""
        pc_active = self._pc_active()
        busy = {final_path(r["path"]) for r in db.rows_in(self.conn, *BUSY_STAGES)}
        for row in self.conn.execute(
                "SELECT * FROM videos WHERE stage='queued' ORDER BY score DESC, id LIMIT 500"):
            if final_path(row["path"]) in busy:
                continue
            if pc_active:
                probe = Probe.from_json(row["probe_json"])
                if not probe.is_hdr and \
                        scorer.expected_hours(self.conn, probe) > self.cfg.pc_handover_hours:
                    continue
            return row
        return None

    def _finish_remote(self, row) -> bool:
        """Verify a PC upload on Drive against its report, then swap like our own."""
        vid, tmp = row["id"], tmp_path(row["path"])
        # Defense in depth: the PC's own report should already be sane (jobs.py
        # validates it on the way in), but never trust it enough to swap on its say alone.
        report_valid = is_valid_md5(row["out_md5"]) and (row["out_size"] or 0) > 0
        try:
            found = self.rclone.entries(tmp)
            ok = (report_valid and len(found) == 1 and found[0]["Size"] == row["out_size"]
                  and self.rclone.md5(tmp) == row["out_md5"])
        except RcloneError as e:
            self.log(f"could not check PC upload {tmp}, will retry: {e}")
            return False
        if not ok:
            try:
                if found:
                    self.rclone.delete(tmp)
            except RcloneError:
                pass
            # Not this worker's failure (the PC's), so it shouldn't trip the
            # consecutive-failure breaker that stops the server's own encoding.
            self._fail(row, "PC upload on Drive doesn't match what the PC reported",
                      count_failure=False)
            return False
        if not row["final_path"]:
            db.mark(self.conn, vid, "remote_ready", final_path=final_path(row["path"]))
        db.mark(self.conn, vid, "swapping")
        try:
            self._swap(db.get(self.conn, vid))
        except (SwapError, RcloneError) as e:
            db.mark(self.conn, vid, "swapping", reason=f"swap: {e}"[:500])
            self.log(f"SWAP FAILED, will retry: {row['path']}: {e}")
            return False
        self.failures = 0
        return True

    def _service_remote(self) -> int:
        """Check one remote_ready row (PC upload) for verify-and-swap, rate-limited
        on check errors so it can't hammer Drive every call. Returns 1 if a row was
        swapped to done, -1 if a row was handled but not completed (its stage moved
        off remote_ready, e.g. a mismatch), or 0 if there was nothing to do right now
        (no remote_ready rows, or still rate-limited after a prior check error)."""
        remote = db.rows_in(self.conn, "remote_ready")
        if not remote:
            return 0
        if not (self._remote_fail_at is None
                or self.clock() - self._remote_fail_at >= self.cfg.idle_poll_secs):
            return 0
        if self._finish_remote(remote[0]):
            self._remote_fail_at = None
            return 1
        if db.get(self.conn, remote[0]["id"])["stage"] == "remote_ready":
            # Couldn't even check Drive (RcloneError): don't hammer it every call,
            # and get on with the server's own work instead of spinning on this row.
            self._remote_fail_at = self.clock()
            return 0
        return -1

    def _encode_tick(self) -> None:
        """Called every idle_poll_secs while the server's own ffmpeg encode runs, so
        the next download, an upload and a PC upload don't wait hours behind a long
        movie. Never raises into run_encode: any failure here must not interrupt the
        server's own encode."""
        if self._deferred is None:  # else the loop crashes as soon as the encode ends
            try:
                self._done_during_encode += self._finish_transfers()
                self._start_transfers()
            except Exception as e:
                self.log(f"transfer check during encode failed: {e!r}")
                self._deferred = e
        if self._dry_run:
            return  # a dry run must not swap/delete anything for real
        try:
            self.jobs.expire_leases()
            while True:
                result = self._service_remote()
                if result == 0:
                    break
                if result > 0:
                    self._done_during_encode += 1
        except Exception as e:
            self.log(f"PC result check during encode failed: {e!r}")

    # --- background transfers (recorded on the main thread only) -----------
    def _finish_transfers(self) -> int:
        """Record finished transfers; returns how many rows that completed."""
        finished = 0
        if self._down is not None and self._down[1].done():
            (row, transfer), self._down = self._down, None
            try:
                transfer.result()
            except Exception as e:
                self.finish_download(row, error=e)
            else:
                self.finish_download(row)
        if self._up is not None and self._up[1].done():
            (row, transfer), self._up = self._up, None
            try:
                transfer.result()
            except Exception as e:
                finished += self.finish_upload(row, e, self._dry_run)
            else:
                finished += self.finish_upload(row, None, self._dry_run)
        return finished

    def _start_transfers(self, block: bool = False) -> None:
        """Start an upload of a ready row and fetch the next file ahead, each if none
        is running and the budgets allow. Waits for disk only when `block` is set and
        no upload is running (recording it is what frees the disk after a swap)."""
        if self.failures >= self.cfg.max_consecutive_failures:
            return  # the loop is about to stop
        ready = db.rows_in(self.conn, "ready")
        if self._up is None and ready and self.budget.allows(ready[0]["out_size"]):
            self._up = (ready[0], self.start_upload(ready[0]))
        if (self._down is not None or db.rows_in(self.conn, "downloaded")
                or len(ready) >= self.cfg.max_ready):
            return
        row = self._next_job()
        if row is not None and self.download_budget.allows(row["size"]):
            if self.start_download(row, block=block and self._up is None):
                cancel = threading.Event()
                self._down = (db.get(self.conn, row["id"]), Transfer(
                    lambda: self._download(row, cancel), cancel=cancel))

    def _wait_transfers(self) -> None:
        """Wait up to idle_poll_secs for a running transfer to finish."""
        running = [t for _, t in filter(None, (self._down, self._up))]
        deadline = time.monotonic() + self.cfg.idle_poll_secs
        while not any(t.done() for t in running) and time.monotonic() < deadline:
            running[0].wait(min(0.5, max(0.0, deadline - time.monotonic())))

    def _drain_transfers(self) -> int:
        """On a normal exit: let running transfers finish and record them."""
        finished = 0
        while self._down is not None or self._up is not None:
            self._wait_transfers()
            finished += self._finish_transfers()
        return finished

    def _cancel_transfers(self) -> None:
        """On a crash or Ctrl-C: stop running transfers without recording them. Their
        rows stay downloading/uploading, which recover() settles on the next start."""
        for pending in (self._down, self._up):
            if pending is not None:
                pending[1].cancel()
        self._down = self._up = None

    def _raise_deferred(self) -> None:
        if self._deferred is not None:
            e, self._deferred = self._deferred, None
            raise e

    def _pc_holds_work(self) -> bool:
        return bool(db.rows_in(self.conn, "leased", "remote_ready")) or (
            self._pc_active() and db.next_queued(self.conn) is not None)

    # --- main loop ---------------------------------------------------------
    def run(self, limit: int | None = None, dry_run: bool = False) -> int:
        self._check_tools()
        self._dry_run = dry_run
        with _lock(self.cfg.workdir):
            done = self.recover(dry_run)
            self.failures = 0
            try:
                done += self._loop(done, limit, dry_run)
                return done + self._drain_transfers()
            except BaseException:
                self._cancel_transfers()
                raise

    def _loop(self, done: int, limit: int | None, dry_run: bool) -> int:
        """The work loop; returns how many rows it completed. Downloads and uploads
        run in the background (see _start_transfers) while this thread encodes."""
        start = done
        last_reconcile = self.clock()
        while limit is None or done < limit:
            # A transfer error deferred during the last encode goes first: recording
            # transfers again could raise a second error and hide it.
            self._raise_deferred()
            done += self._finish_transfers()
            if self.failures >= self.cfg.max_consecutive_failures:
                self.log(f"!!! STOPPING: {self.failures} failures in a row with no success; "
                         f"something is wrong beyond a single file (see reasons in status)")
                break
            if (not dry_run and self.clock() - last_reconcile >= self.cfg.swap_retry_secs
                    and db.rows_in(self.conn, "swapping")):
                done += self._reconcile_swaps()
                last_reconcile = self.clock()
            self.jobs.expire_leases()
            if limit is not None and done >= limit:
                break
            if not dry_run:
                serviced = self._service_remote()
                if serviced > 0:
                    done += 1
                    continue
                if serviced < 0:
                    continue
            # Waiting for disk is fine only with nothing else under way: no transfer
            # to record and no download waiting to be encoded.
            idle = self._down is None and self._up is None
            self._start_transfers(block=idle and not db.rows_in(self.conn, "downloaded"))
            ready = db.rows_in(self.conn, "ready")
            waiting = db.rows_in(self.conn, "downloaded")
            if waiting and len(ready) < self.cfg.max_ready:
                self.encode_and_verify(waiting[0])
                done += self._done_during_encode
                self._done_during_encode = 0
                continue
            if self._down is not None or self._up is not None:
                self._wait_transfers()
                continue
            row = self._next_job() if len(ready) < self.cfg.max_ready else None
            if row is not None and self.download_budget.allows(row["size"]):
                continue  # it was settled (e.g. failed, or taken by the PC) instead
            if not ready and row is None:
                if dry_run or not self._pc_holds_work():
                    break
                self.sleep(self.cfg.idle_poll_secs)
                continue
            waits = {}
            if ready:
                waits["upload"] = self.budget.seconds_until_reset()
            if row is not None:
                waits["download"] = self.download_budget.seconds_until_reset()
            kind = min(waits, key=waits.get)
            wait = waits[kind]
            if self._pc_holds_work():
                # Wake up regularly anyway: a PC result (remote_ready) needs no
                # budget of ours to verify and swap, and must not sit for hours.
                wait = min(wait, self.cfg.idle_poll_secs)
            self.log(f"{' and '.join(waits)} budget used up; "
                     f"sleeping {wait / 3600:.1f} h")
            self.sleep(wait)
        return done - start

    def _wait_for_disk(self, size: int, block: bool = True) -> bool:
        """True once there is room for `size`; without `block`, False instead of waiting."""
        need = self.cfg.disk_factor * size + self.cfg.disk_reserve
        total = shutil.disk_usage(self.cfg.workdir).total
        if need > total:
            raise DiskTooSmall(f"needs {need / 1e9:.0f} GB disk, only {total / 1e9:.0f} GB total")
        while shutil.disk_usage(self.cfg.workdir).free < need + self._pending_output():
            if not block:
                return False
            self.log(f"waiting for {need / 1e9:.0f} GB free disk")
            self.sleep(self.cfg.disk_wait_secs)
        return True

    def _pending_output(self) -> int:
        """Disk the unfinished encodes still need: up to each source's size of output
        beyond what its out file holds now. Free space doesn't show it yet, and a
        download fetched ahead mustn't take it (the serial worker never overlapped)."""
        pending = 0
        for row in db.rows_in(self.conn, "downloaded", "encoding"):
            out = self._out(row["id"])
            pending += max(0, row["size"] - (out.stat().st_size if out.exists() else 0))
        return pending

    def start_download(self, row, block: bool = True) -> bool:
        """Mark the row `downloading` if it can start now; False if it was skipped
        (or, without `block`, if the disk is too full to start right now)."""
        vid, path = row["id"], row["path"]
        final = final_path(path)
        try:
            if final != path and self.rclone.exists(final):
                db.mark_if(self.conn, vid, ("queued",), "failed",
                          reason=f"target {final} already exists")
                return False
            if not self._wait_for_disk(row["size"], block):
                return False  # fetching ahead: try again later rather than wait here
            self.log(f"downloading {path} ({row['size'] / 1e9:.1f} GB)")
            # The interruption marker (reason) stays through downloading/downloaded/
            # encoding/verifying and is cleared only when the work completes (-> ready),
            # so a second interruption in a row is recognised by recover().
            # Atomic: re-check the busy-stem guard and switch to downloading in one
            # transaction, so a lease or another row's progress can't slip in between
            # the check and the mark (requirement A: no double assignment).
            with db.transaction(self.conn):
                busy = {final_path(r["path"]) for r in db.rows_in(self.conn, *BUSY_STAGES)
                        if r["id"] != vid}
                if final in busy:
                    started = False
                else:
                    started = db.mark_if(self.conn, vid, ("queued",), "downloading",
                                         encoder="x265")
        except DiskTooSmall as e:
            self._cleanup(vid)
            db.mark_if(self.conn, vid, ("queued",), "failed", reason=str(e)[:500])
            self.log(f"failed: {path}: {e}")
            return False
        except (RcloneError, OSError) as e:
            self._on_error(row, e)
            return False
        if not started:
            self.log(f"skipping {path}: no longer queued or its target is busy "
                     f"(leased to another worker?)")
        return started

    def _download(self, row, cancel: threading.Event | None = None) -> None:
        """Only the rclone transfer: no database access, so it may run off the main thread."""
        self._retry(lambda: self.rclone.download(row["path"], self._src(row["id"]),
                                                 cancel=cancel))

    def finish_download(self, row, error: Exception | None = None) -> bool:
        """After the transfer (or its `error`): count it, check the size, mark `downloaded`."""
        vid, path = row["id"], row["path"]
        if error is not None:
            self._on_error(row, error)
            return False
        try:
            self.download_budget.add(row["size"])
            if self._src(vid).stat().st_size != row["size"]:
                self._cleanup(vid)
                db.mark(self.conn, vid, "unprobed", reason="changed since scan",
                        probe_json=None, score=None)
                self.log(f"changed since scan, will re-probe: {path}")
                return False
        except OSError as e:
            self._on_error(row, e)
            return False
        db.mark(self.conn, vid, "downloaded")
        return True

    def encode_and_verify(self, row) -> None:
        """`downloaded` -> encoding -> verifying -> ready, failed or not_worth_it."""
        vid, path = row["id"], row["path"]
        final = final_path(path)
        probe = Probe.from_json(row["probe_json"])
        src, out = self._src(vid), self._out(vid)
        try:
            self.log(f"encoding {path}")
            db.mark(self.conn, vid, "encoding")
            # Record transfers, start the next ones and service PC (remote_ready)
            # results every idle_poll_secs while this potentially hours-long encode
            # runs, so they don't wait behind it.
            secs = run_encode(build_command(src, out, probe, self.cfg), self._logfile(vid),
                              tick=self._encode_tick, tick_secs=self.cfg.idle_poll_secs)
            db.mark(self.conn, vid, "verifying", encode_secs=secs)
            out_size = out.stat().st_size
            out_probe = parse_ffprobe(run_ffprobe(str(out)), out_size)
            errors = decode_errors(out)
            out_audio, src_audio = audio_errors(src, out) if errors else ("", "")
            verdict = compare(probe, out_probe, row["size"], out_size, self.cfg.min_saving,
                              errors, src_audio, out_audio)
            if verdict.problems:
                self._fail(row, "; ".join(verdict.problems))
                return
            if verdict.not_worth_it:
                self._cleanup(vid)
                saved = 1 - out_size / row["size"]
                db.mark(self.conn, vid, "not_worth_it", reason=f"only {saved:.0%} smaller")
                self.failures = 0
                self.log(f"not worth it: {path} ({saved:.0%})")
                return
            md5 = md5_file(out)
        except (RcloneError, EncodeError, ProbeError, OSError) as e:
            self._on_error(row, e, keep_src=True)
            return
        self._cleanup(vid, keep_output=True)
        db.mark(self.conn, vid, "ready", out_size=out_size, out_md5=md5, final_path=final,
                reason=None)
        self.failures = 0

    def _on_error(self, row, e: Exception, keep_src: bool = False) -> None:
        """Settle a row whose download/encode raised `e`; re-raises when a tool vanished.
        `keep_src`: the download is complete (the encode failed), so when a tool vanished
        the row goes back to `downloaded` with it, for the next start to reuse."""
        vid = row["id"]
        if isinstance(e, FileNotFoundError):
            if e.filename not in self._tools():
                # A missing work file (e.g. ffmpeg exited 0 without output) is this
                # file's problem; crashing would re-download it on every restart.
                self._fail(row, str(e))
                return
            # A tool (rclone/ffmpeg/ffprobe) vanished: not this file's fault, so crash
            # without burning an attempt and leave the row to be picked up again.
            if keep_src:
                self._out(vid).unlink(missing_ok=True)
                self._logfile(vid).unlink(missing_ok=True)
                db.mark(self.conn, vid, "downloaded")
            else:
                self._cleanup(vid)
                db.mark(self.conn, vid, "queued")
            raise e
        if isinstance(e, RcloneError) and e.download_quota:
            # Not this file's fault: pause all downloads, keep the file queued.
            self._cleanup(vid)
            self.download_budget.block(self.cfg.download_block_secs)
            db.mark(self.conn, vid, "queued")
            self.log("Drive download quota exceeded; pausing downloads "
                     f"for {self.cfg.download_block_secs / 3600:.0f} h")
            return
        self._fail(row, str(e))

    def start_upload(self, row) -> Transfer:
        """Mark a ready row `uploading` and start uploading it in the background;
        finish_upload records the outcome."""
        vid, tmp = row["id"], tmp_path(row["path"])
        db.mark(self.conn, vid, "uploading")
        self.log(f"uploading {tmp} ({row['out_size'] / 1e9:.1f} GB)")
        cancel = threading.Event()
        return Transfer(lambda: self._retry(
            lambda: self.rclone.upload(self._out(vid), tmp, cancel=cancel)), cancel=cancel)

    def finish_upload(self, row, error: Exception | None, dry_run: bool) -> bool:
        """After the upload (or its `error`): check the md5, count it, then swap (or,
        in a dry run, stop at `dry_run`). True if the row is finished."""
        vid, path = row["id"], row["path"]
        tmp = tmp_path(path)
        try:
            if error is not None:
                raise error
            remote_md5 = self.rclone.md5(tmp)
        except RcloneError as e:
            if e.rate_limited:
                self.budget.block(self.cfg.rate_limit_block_secs)
                db.mark(self.conn, vid, "ready")
                self.log("Drive upload limit hit; blocking uploads")
                return False
            self._fail(row, f"upload: {e}", retry_stage="ready")
            return False
        self.budget.add(row["out_size"])
        if remote_md5 != row["out_md5"]:
            try:
                self.rclone.delete(tmp)
            except RcloneError:
                pass
            self._fail(row, "checksum mismatch after upload", retry_stage="ready")
            return False
        self.failures = 0
        if dry_run:
            self._cleanup(vid)
            db.mark(self.conn, vid, "dry_run", reason=f"uploaded as {tmp}")
            return True
        db.mark(self.conn, vid, "swapping")
        try:
            self._swap(db.get(self.conn, vid))
        except (SwapError, RcloneError) as e:
            db.mark(self.conn, vid, "swapping", reason=f"swap: {e}"[:500])
            self.log(f"SWAP FAILED, will retry: {path}: {e}")
            return False
        return True

    def _one(self, path: str) -> dict | None:
        found = self.rclone.entries(path)
        if len(found) > 1:
            raise SwapError(f"{len(found)} files named {path} on Drive")
        return found[0] if found else None

    def _swap(self, row) -> None:
        path, final, tmp = row["path"], row["final_path"], tmp_path(row["path"])
        if self._one(tmp):
            if final != path and self._one(final):
                raise SwapError(f"target {final} already exists")
            # Defense in depth, right before the point of no return (deleting the
            # original): re-verify the upload we're about to promote still matches
            # the encode we actually verified, in case it changed since.
            if self.rclone.md5(tmp) != row["out_md5"]:
                raise SwapError("tmp no longer matches the verified encode; not deleting")
            originals = self.rclone.entries(path)
            if originals:
                # Only delete the exact file that was downloaded and encoded.
                if len(originals) != 1 or (originals[0]["Size"], originals[0]["ModTime"]) \
                        != (row["size"], row["modtime"]):
                    raise SwapError("original changed or duplicated; not deleting")
                self._retry(lambda: self.rclone.delete(path))
            self._retry(lambda: self.rclone.move(tmp, final))
        elif not (self._one(final) and self.rclone.md5(final) == row["out_md5"]):
            raise SwapError(f"{tmp} missing and {final} does not match the encode")
        self._finish(row)

    def _finish(self, row) -> None:
        self._cleanup(row["id"])
        db.mark(self.conn, row["id"], "done", reason=None)
        probe = Probe.from_json(row["probe_json"])
        scorer.learn_result(self.conn, probe, row["size"], row["out_size"],
                            row["encode_secs"], row["encoder"] or "x265")
        scorer.rescore(self.conn, self.cfg)
        self.log(f"done: {row['final_path']} saved {(row['size'] - row['out_size']) / 1e9:.1f} GB")

    def recover(self, dry_run: bool = False) -> int:
        for row in db.rows_in(self.conn, "downloading", "downloaded", "encoding", "verifying"):
            vid = row["id"]
            complete = self._src(vid).exists() and self._src(vid).stat().st_size == row["size"]
            if row["stage"] == "downloaded" and complete:
                continue  # a finished download waiting its turn: reuse it
            reason = f"{INTERRUPTED_PREFIX}{row['stage']}"
            keep_src = row["stage"] in ("encoding", "verifying") and complete
            if (row["reason"] or "").startswith(INTERRUPTED_PREFIX):
                # Second interruption in a row for this file: it may be what's
                # crashing us, so it costs an attempt like any other failure.
                self._fail(row, reason)
            elif keep_src:
                # Only the partial encode is lost; the download is still good.
                self._out(vid).unlink(missing_ok=True)
                self._logfile(vid).unlink(missing_ok=True)
                db.mark(self.conn, vid, "downloaded", reason=reason)
                self.log(f"downloaded: {row['path']}: {reason} (first time, no attempt used)")
            else:
                # A reboot or restart isn't this file's fault: requeue for free, but
                # remember it so a repeat does count.
                self._cleanup(vid)
                db.mark(self.conn, vid, "queued", reason=reason)
                self.log(f"queued: {row['path']}: {reason} (first time, no attempt used)")
        for row in db.rows_in(self.conn, "uploading"):
            tmp = tmp_path(row["path"])
            try:
                if self.rclone.exists(tmp):
                    self.rclone.delete(tmp)
            except RcloneError as e:
                self.log(f"could not remove {tmp}: {e}")
            stage = "ready" if self._out(row["id"]).exists() else "queued"
            db.mark(self.conn, row["id"], stage)
        if dry_run:
            pending = len(db.rows_in(self.conn, "swapping"))
            if pending:
                self.log(f"dry run: {pending} pending swaps left alone")
            return 0
        return self._reconcile_swaps()

    def _reconcile_swaps(self) -> int:
        """Finish or settle rows left in `swapping`; returns how many were completed."""
        finished = 0
        for row in db.rows_in(self.conn, "swapping"):
            try:
                self._swap(row)
                finished += 1
            except SwapError as e:
                self._swap_failed(row, e)
            except RcloneError as e:
                db.mark(self.conn, row["id"], "swapping", reason=f"swap: {e}"[:500])
                self.log(f"swap could not be checked (rclone error), "
                         f"will retry: {row['path']}: {e}")
        return finished

    def _swap_failed(self, row, e: SwapError) -> None:
        try:
            original_present = bool(self.rclone.entries(row["path"]))
        except RcloneError as err:
            db.mark(self.conn, row["id"], "swapping", reason=f"swap: {e}"[:500])
            self.log(f"swap failed and original could not be checked, will retry: "
                     f"{row['path']}: {e}; {err}")
            return
        if original_present:
            db.mark(self.conn, row["id"], "failed", reason=f"swap: {e}"[:500])
            self._cleanup(row["id"])
            self.log(f"SWAP FAILED, original kept: {row['path']}: {e}")
            return
        # The original is already in Drive's trash; the encode may be the only copy.
        # A remote encode (PC or cloud) only ever existed here as the Drive tmp, never locally.
        kept = (tmp_path(row["path"]) + " on Drive" if row["encoder"] in scorer.REMOTE_ENCODERS
                else self._out(row["id"]))
        db.mark(self.conn, row["id"], "swapping", reason=f"swap: needs attention: {e}"[:500])
        self.log(f"!!! SWAP NEEDS ATTENTION: original of {row['path']} is gone and the "
                 f"encode is not in place; keeping {kept}: {e}")
