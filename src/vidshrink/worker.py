import fcntl
import hashlib
import posixpath
import shutil
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.encode import EncodeError, build_command, run_encode
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone, RcloneError, with_retries
from vidshrink.verify import compare, decode_errors


class AlreadyRunning(Exception):
    pass


class SwapError(Exception):
    pass


def final_path(path: str) -> str:
    return posixpath.splitext(path)[0] + ".mkv"


def tmp_path(path: str) -> str:
    return posixpath.splitext(path)[0] + ".vidshrink-tmp.mkv"


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


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
                 log: Callable[[str], None] = print):
        self.conn = conn
        self.rclone = rclone
        self.cfg = cfg
        self.budget = budget
        self.sleep = sleep
        self.log = log

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

    def _fail(self, row, reason: str, retry_stage: str = "queued") -> None:
        attempts = row["attempts"] + 1
        final = attempts >= self.cfg.max_attempts
        self._cleanup(row["id"], keep_output=(retry_stage == "ready" and not final))
        stage = "failed" if final else retry_stage
        db.mark(self.conn, row["id"], stage, reason=reason[:500], attempts=attempts)
        self.log(f"{stage}: {row['path']}: {reason[:200]}")

    # --- main loop ---------------------------------------------------------
    def run(self, limit: int | None = None, dry_run: bool = False) -> int:
        with _lock(self.cfg.workdir):
            self.recover()
            done = 0
            while limit is None or done < limit:
                ready = db.rows_in(self.conn, "ready")
                if ready and self.budget.allows(ready[0]["out_size"]):
                    if self.upload_and_swap(ready[0], dry_run):
                        done += 1
                    continue
                row = db.next_queued(self.conn) if len(ready) < self.cfg.max_ready else None
                if row is not None:
                    self.prepare(row)
                    continue
                if not ready:
                    break
                wait = self.budget.seconds_until_reset()
                self.log(f"upload budget used up; sleeping {wait / 3600:.1f} h")
                self.sleep(wait)
            return done

    def _wait_for_disk(self, size: int) -> None:
        need = self.cfg.disk_factor * size + self.cfg.disk_reserve
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
                db.mark(self.conn, vid, "failed", reason=f"target {final} already exists")
                return
            self._wait_for_disk(row["size"])
            self.log(f"downloading {path} ({row['size'] / 1e9:.1f} GB)")
            db.mark(self.conn, vid, "downloading")
            self._retry(lambda: self.rclone.download(path, src))
            self.log(f"encoding {path}")
            db.mark(self.conn, vid, "encoding")
            secs = run_encode(build_command(src, out, probe, self.cfg), self._logfile(vid))
            db.mark(self.conn, vid, "verifying", encode_secs=secs)
            out_size = out.stat().st_size
            out_probe = parse_ffprobe(run_ffprobe(str(out)), out_size)
            verdict = compare(probe, out_probe, row["size"], out_size,
                              self.cfg.min_saving, decode_errors(out))
        except (RcloneError, EncodeError, ProbeError, OSError) as e:
            self._fail(row, str(e))
            return
        if verdict.problems:
            self._fail(row, "; ".join(verdict.problems))
            return
        if verdict.not_worth_it:
            self._cleanup(vid)
            saved = 1 - out_size / row["size"]
            db.mark(self.conn, vid, "not_worth_it", reason=f"only {saved:.0%} smaller")
            self.log(f"not worth it: {path} ({saved:.0%})")
            return
        md5 = _md5(out)
        self._cleanup(vid, keep_output=True)
        db.mark(self.conn, vid, "ready", out_size=out_size, out_md5=md5, final_path=final)

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
        if dry_run:
            self._cleanup(vid)
            db.mark(self.conn, vid, "dry_run", reason=f"uploaded as {tmp}")
            return True
        db.mark(self.conn, vid, "swapping")
        try:
            self._swap(db.get(self.conn, vid))
        except (SwapError, RcloneError) as e:
            db.mark(self.conn, vid, "failed", reason=f"swap: {e}"[:500])
            self.log(f"SWAP FAILED, needs attention: {path}: {e}")
            return False
        return True

    def _swap(self, row) -> None:
        path, final, tmp = row["path"], row["final_path"], tmp_path(row["path"])
        if self.rclone.exists(tmp):
            if self.rclone.exists(path):
                self._retry(lambda: self.rclone.delete(path))
            self._retry(lambda: self.rclone.move(tmp, final))
        elif not (self.rclone.exists(final) and self.rclone.md5(final) == row["out_md5"]):
            raise SwapError(f"{tmp} missing and {final} does not match the encode")
        self._finish(row)

    def _finish(self, row) -> None:
        self._cleanup(row["id"])
        db.mark(self.conn, row["id"], "done", reason=None)
        probe = Probe.from_json(row["probe_json"])
        family = scorer.codec_family(probe.codec)
        if family:
            db.learn(self.conn, f"ratio:{family}", 1 - row["out_size"] / row["size"])
        if row["encode_secs"]:
            res = scorer.resolution_class(probe.width, probe.height)
            db.learn(self.conn, f"speed:{res}", row["encode_secs"] / probe.duration)
        scorer.rescore(self.conn, self.cfg)
        self.log(f"done: {row['final_path']} saved {(row['size'] - row['out_size']) / 1e9:.1f} GB")

    def recover(self) -> None:
        pass  # Task 10
