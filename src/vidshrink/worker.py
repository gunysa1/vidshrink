import fcntl
import shutil
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
from vidshrink.verify import compare, decode_errors


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
        self.failures = 0  # consecutive prepare/upload failures with no success between
        self._remote_fail_at = None  # self.clock() of the last remote_ready check we couldn't
        # complete (RcloneError), so we don't hammer Drive every loop iteration for it
        self._remote_done_during_encode = 0  # remote_ready rows swapped via _encode_tick()
        self._dry_run = False  # set for the duration of run(); guards _encode_tick's swap

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
        a PC upload doesn't wait hours behind a long movie. Never raises into
        run_encode: any failure here must not interrupt the server's own encode."""
        try:
            self.jobs.expire_leases()
            while True:
                result = self._service_remote()
                if result == 0:
                    break
                if result > 0:
                    self._remote_done_during_encode += 1
        except Exception as e:
            self.log(f"PC result check during encode failed: {e!r}")

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
            last_reconcile = self.clock()
            while limit is None or done < limit:
                if self.failures >= self.cfg.max_consecutive_failures:
                    self.log(f"!!! STOPPING: {self.failures} failures in a row with no success; "
                             f"something is wrong beyond a single file (see reasons in status)")
                    break
                if (not dry_run and self.clock() - last_reconcile >= self.cfg.swap_retry_secs
                        and db.rows_in(self.conn, "swapping")):
                    done += self._reconcile_swaps()
                    last_reconcile = self.clock()
                self.jobs.expire_leases()
                if not dry_run:
                    serviced = self._service_remote()
                    if serviced > 0:
                        done += 1
                        continue
                    if serviced < 0:
                        continue
                ready = db.rows_in(self.conn, "ready")
                if ready and self.budget.allows(ready[0]["out_size"]):
                    if self.upload_and_swap(ready[0], dry_run):
                        done += 1
                    continue
                row = self._next_job() if len(ready) < self.cfg.max_ready else None
                if row is not None and self.download_budget.allows(row["size"]):
                    self.prepare(row)
                    done += self._remote_done_during_encode
                    self._remote_done_during_encode = 0
                    continue
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
            return done

    def _wait_for_disk(self, size: int) -> None:
        need = self.cfg.disk_factor * size + self.cfg.disk_reserve
        total = shutil.disk_usage(self.cfg.workdir).total
        if need > total:
            raise DiskTooSmall(f"needs {need / 1e9:.0f} GB disk, only {total / 1e9:.0f} GB total")
        while shutil.disk_usage(self.cfg.workdir).free < need:
            self.log(f"waiting for {need / 1e9:.0f} GB free disk")
            self.sleep(self.cfg.disk_wait_secs)

    def prepare(self, row) -> None:
        vid, path = row["id"], row["path"]
        final = final_path(path)
        probe = Probe.from_json(row["probe_json"])
        src, out = self._src(vid), self._out(vid)
        try:
            if final != path and self.rclone.exists(final):
                db.mark_if(self.conn, vid, ("queued",), "failed",
                          reason=f"target {final} already exists")
                return
            self._wait_for_disk(row["size"])
            self.log(f"downloading {path} ({row['size'] / 1e9:.1f} GB)")
            # The interruption marker (reason) stays through downloading/encoding/
            # verifying and is cleared only when the work completes (-> ready), so a
            # second interruption in a row is recognised by recover().
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
            if not started:
                self.log(f"skipping {path}: no longer queued or its target is busy "
                         f"(leased to another worker?)")
                return
            self._retry(lambda: self.rclone.download(path, src))
            self.download_budget.add(row["size"])
            if src.stat().st_size != row["size"]:
                self._cleanup(vid)
                db.mark(self.conn, vid, "unprobed", reason="changed since scan",
                        probe_json=None, score=None)
                self.log(f"changed since scan, will re-probe: {path}")
                return
            self.log(f"encoding {path}")
            db.mark(self.conn, vid, "encoding")
            # Service PC (remote_ready) results every idle_poll_secs while this
            # potentially hours-long encode runs, so they don't wait behind it; never
            # during a dry run, which must not swap/delete anything for real.
            tick = None if self._dry_run else self._encode_tick
            secs = run_encode(build_command(src, out, probe, self.cfg), self._logfile(vid),
                              tick=tick, tick_secs=self.cfg.idle_poll_secs)
            db.mark(self.conn, vid, "verifying", encode_secs=secs)
            out_size = out.stat().st_size
            out_probe = parse_ffprobe(run_ffprobe(str(out)), out_size)
            verdict = compare(probe, out_probe, row["size"], out_size,
                              self.cfg.min_saving, decode_errors(out))
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
        except DiskTooSmall as e:
            self._cleanup(vid)
            db.mark_if(self.conn, vid, ("queued",), "failed", reason=str(e)[:500])
            self.log(f"failed: {path}: {e}")
            return
        except FileNotFoundError as e:
            if e.filename not in self._tools():
                # A missing work file (e.g. ffmpeg exited 0 without output) is this
                # file's problem; crashing would re-download it on every restart.
                self._fail(row, str(e))
                return
            # A tool (rclone/ffmpeg/ffprobe) vanished: not this file's fault, so crash
            # without burning an attempt and leave the row to be picked up again.
            self._cleanup(vid)
            db.mark(self.conn, vid, "queued")
            raise
        except (RcloneError, EncodeError, ProbeError, OSError) as e:
            if isinstance(e, RcloneError) and e.download_quota:
                # Not this file's fault: pause all downloads, keep the file queued.
                self._cleanup(vid)
                self.download_budget.block(self.cfg.download_block_secs)
                db.mark(self.conn, vid, "queued")
                self.log("Drive download quota exceeded; pausing downloads "
                         f"for {self.cfg.download_block_secs / 3600:.0f} h")
                return
            self._fail(row, str(e))
            return
        self._cleanup(vid, keep_output=True)
        db.mark(self.conn, vid, "ready", out_size=out_size, out_md5=md5, final_path=final,
                reason=None)
        self.failures = 0

    def upload_and_swap(self, row, dry_run: bool) -> bool:
        vid, path = row["id"], row["path"]
        tmp = tmp_path(path)
        db.mark(self.conn, vid, "uploading")
        self.log(f"uploading {tmp} ({row['out_size'] / 1e9:.1f} GB)")
        try:
            self._retry(lambda: self.rclone.upload(self._out(vid), tmp))
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
        for row in db.rows_in(self.conn, "downloading", "encoding", "verifying"):
            reason = f"{INTERRUPTED_PREFIX}{row['stage']}"
            if (row["reason"] or "").startswith(INTERRUPTED_PREFIX):
                # Second interruption in a row for this file: it may be what's
                # crashing us, so it costs an attempt like any other failure.
                self._fail(row, reason)
            else:
                # A reboot or restart isn't this file's fault: requeue for free, but
                # remember it so a repeat does count.
                self._cleanup(row["id"])
                db.mark(self.conn, row["id"], "queued", reason=reason)
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
        # A PC encode (nvenc) only ever existed here as the Drive tmp, never locally.
        kept = (tmp_path(row["path"]) + " on Drive" if row["encoder"] == "nvenc"
                else self._out(row["id"]))
        db.mark(self.conn, row["id"], "swapping", reason=f"swap: needs attention: {e}"[:500])
        self.log(f"!!! SWAP NEEDS ATTENTION: original of {row['path']} is gone and the "
                 f"encode is not in place; keeping {kept}: {e}")
