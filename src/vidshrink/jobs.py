"""Leases encode jobs to remote workers (the PC). The server's DB stays the source of
truth; only the server verifies uploads and replaces originals."""
import json
import time
from collections.abc import Callable

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.paths import final_path, tmp_path
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone, RcloneError

PC_SEEN_KEY = "pc_last_seen"
NO_WORK_RETRY_SECS = 600.0


class LeaseLost(Exception):
    """The job isn't leased by this worker any more (expired, released or cancelled)."""


class JobStore:
    def __init__(self, conn, cfg: Config, rclone: Rclone,
                 now: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.conn = conn
        self.cfg = cfg
        self.rclone = rclone
        self.now = now
        self.log = log
        self.uploads = Budget(conn, cfg.daily_upload_limit, now)
        self.downloads = Budget(conn, cfg.daily_download_limit, now, kind="download")

    def _seen(self) -> None:
        db.kv_set(self.conn, PC_SEEN_KEY, str(self.now()))

    def _leased_by(self, job_id: int, worker: str):
        row = db.get(self.conn, job_id)
        if row is None or row["stage"] != "leased" or row["worker"] != worker:
            raise LeaseLost(f"job {job_id} is not leased by {worker}")
        return row

    def _candidates(self, encoder: str) -> list:
        scored = []
        for row in db.rows_in(self.conn, "queued"):
            probe = Probe.from_json(row["probe_json"])
            if probe.is_hdr:
                continue
            scored.append((scorer.encoder_score(self.conn, row["size"], probe, encoder), row))
        scored.sort(key=lambda t: (-t[0], t[1]["id"]))
        return [row for _, row in scored]

    def claim(self, worker: str, encoder: str = "nvenc") -> dict:
        self._seen()
        self.expire_leases()
        if self.downloads.blocked():
            return {"job": None, "retry_after": self.downloads.seconds_until_reset()}
        for row in self._candidates(encoder):
            final = final_path(row["path"])
            if final != row["path"] and self.rclone.exists(final):
                db.mark_if(self.conn, row["id"], ("queued",), "failed",
                           reason=f"target {final} already exists")
                continue
            if not self.downloads.allows(row["size"]):
                return {"job": None, "retry_after": self.downloads.seconds_until_reset()}
            with db.transaction(self.conn):
                taken = db.mark_if(self.conn, row["id"], ("queued",), "leased", worker=worker,
                                   encoder=encoder, final_path=final, reason=None,
                                   lease_until=self.now() + self.cfg.lease_secs)
                if taken:
                    self.downloads.add(row["size"])
            if not taken:
                continue
            self.log(f"leased to {worker}: {row['path']}")
            return {"job": {"id": row["id"], "path": row["path"], "size": row["size"],
                            "modtime": row["modtime"], "final_path": final,
                            "tmp_path": tmp_path(row["path"]),
                            "probe": json.loads(row["probe_json"])},
                    "retry_after": 0.0}
        return {"job": None, "retry_after": NO_WORK_RETRY_SECS}

    def heartbeat(self, job_id: int, worker: str) -> dict:
        self._seen()
        if not self._is_leased_by(job_id, worker):
            return {"cancel": True}
        db.mark_if(self.conn, job_id, ("leased",), "leased",
                   lease_until=self.now() + self.cfg.lease_secs)
        return {"cancel": False}

    def _is_leased_by(self, job_id: int, worker: str) -> bool:
        try:
            self._leased_by(job_id, worker)
            return True
        except LeaseLost:
            return False

    def report(self, job_id: int, worker: str, outcome: str, **data) -> None:
        self._seen()
        row = self._leased_by(job_id, worker)
        clear = {"worker": None, "lease_until": None}
        if outcome == "uploaded":
            db.mark(self.conn, job_id, "remote_ready", out_size=int(data["out_size"]),
                    out_md5=data["out_md5"], encode_secs=float(data["encode_secs"]),
                    reason=None, **clear)
            self.uploads.add(int(data["out_size"]))
        elif outcome == "failed":
            attempts = row["attempts"] + 1
            stage = "failed" if attempts >= self.cfg.max_attempts else "queued"
            db.mark(self.conn, job_id, stage, attempts=attempts,
                    reason=f"PC: {data.get('reason', '')}"[:500], **clear)
        elif outcome == "not_worth_it":
            db.mark(self.conn, job_id, "not_worth_it", reason=str(data.get("reason", ""))[:500],
                    **clear)
        elif outcome == "changed":
            db.mark(self.conn, job_id, "unprobed", reason="changed since scan",
                    probe_json=None, score=None, **clear)
        elif outcome == "download_quota":
            self.downloads.block(self.cfg.download_block_secs)
            db.mark(self.conn, job_id, "queued", **clear)
            self.log("PC hit Drive's download quota; downloads paused for everyone")
        else:
            raise ValueError(f"unknown outcome {outcome!r}")
        self.log(f"{worker} reported {outcome}: {row['path']}")

    def release(self, job_id: int, worker: str) -> None:
        self._seen()
        row = self._leased_by(job_id, worker)
        db.mark(self.conn, job_id, "queued", worker=None, lease_until=None)
        self.log(f"{worker} released: {row['path']}")

    def expire_leases(self) -> int:
        expired = self.conn.execute(
            "SELECT * FROM videos WHERE stage='leased' AND lease_until < ?", (self.now(),)
        ).fetchall()
        n = 0
        for row in expired:
            tmp = tmp_path(row["path"])
            try:
                if self.rclone.exists(tmp):
                    self.rclone.delete(tmp)
            except RcloneError as e:
                self.log(f"lease expired but {tmp} could not be removed yet: {e}")
                continue  # try again on the next sweep
            if db.mark_if(self.conn, row["id"], ("leased",), "queued", worker=None,
                          lease_until=None):
                n += 1
                self.log(f"lease expired ({row['worker']}): {row['path']}")
        return n
