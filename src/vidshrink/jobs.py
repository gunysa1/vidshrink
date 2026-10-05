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
# Rows in any of these stages occupy a final_path/tmp_path stem; a queued row that
# shares a stem with one of them (same basename, different extension) can't be
# claimed yet, since the two would race over the same remote final_path/tmp_path.
BUSY_STAGES = db.IN_PROGRESS + ("ready",) + db.REMOTE_STAGES


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

    def _leased_by(self, job_id: int, worker: str, now: float):
        """The row, iff it is validly leased by worker right now (stage, worker AND an
        unexpired lease_until all hold); otherwise LeaseLost."""
        row = db.get(self.conn, job_id)
        if (row is None or row["stage"] != "leased" or row["worker"] != worker
                or row["lease_until"] is None or row["lease_until"] < now):
            raise LeaseLost(f"job {job_id} is not leased by {worker}")
        return row

    def _is_leased_by(self, job_id: int, worker: str, now: float) -> bool:
        try:
            self._leased_by(job_id, worker, now)
            return True
        except LeaseLost:
            return False

    def _update_leased(self, job_id: int, worker: str, now: float, fields: dict) -> bool:
        """Write fields iff the row is still validly leased by worker right now. The
        WHERE clause re-checks the lease itself, so this is safe even without a
        surrounding transaction (though callers that also read the row first should
        still wrap both in one db.transaction for a consistent snapshot)."""
        fields = {**fields, "updated_at": now}
        cols = ", ".join(f"{k}=?" for k in fields)
        return self.conn.execute(
            f"UPDATE videos SET {cols} WHERE id=? AND stage='leased' AND worker=?"
            " AND lease_until>=?",
            (*fields.values(), job_id, worker, now),
        ).rowcount == 1

    def _mark_leased_or_raise(self, job_id: int, worker: str, now: float, stage: str,
                              fields: dict) -> None:
        if not self._update_leased(job_id, worker, now, {**fields, "stage": stage}):
            raise LeaseLost(f"job {job_id} is not leased by {worker}")

    def _busy_final_paths(self) -> set:
        return {final_path(r["path"]) for r in db.rows_in(self.conn, *BUSY_STAGES)}

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
        busy = self._busy_final_paths()
        for row in self._candidates(encoder):
            final = final_path(row["path"])
            if final in busy:
                continue  # another row owns this final_path/tmp_path stem right now
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
        now = self.now()
        with db.transaction(self.conn):
            ok = self._is_leased_by(job_id, worker, now) and self._update_leased(
                job_id, worker, now, {"lease_until": now + self.cfg.lease_secs})
        return {"cancel": not ok}

    def report(self, job_id: int, worker: str, outcome: str, **data) -> None:
        self._seen()
        now = self.now()
        with db.transaction(self.conn):
            row = self._leased_by(job_id, worker, now)
            clear = {"worker": None, "lease_until": None}
            if outcome == "uploaded":
                self._mark_leased_or_raise(job_id, worker, now, "remote_ready", {
                    "out_size": int(data["out_size"]), "out_md5": data["out_md5"],
                    "encode_secs": float(data["encode_secs"]), "reason": None, **clear})
                self.uploads.add(int(data["out_size"]))
            elif outcome == "failed":
                attempts = row["attempts"] + 1
                stage = "failed" if attempts >= self.cfg.max_attempts else "queued"
                self._mark_leased_or_raise(job_id, worker, now, stage, {
                    "attempts": attempts,
                    "reason": f"PC: {data.get('reason', '')}"[:500], **clear})
            elif outcome == "not_worth_it":
                self._mark_leased_or_raise(job_id, worker, now, "not_worth_it", {
                    "reason": str(data.get("reason", ""))[:500], **clear})
            elif outcome == "changed":
                self._mark_leased_or_raise(job_id, worker, now, "unprobed", {
                    "reason": "changed since scan", "probe_json": None, "score": None,
                    **clear})
            elif outcome == "download_quota":
                self.downloads.block(self.cfg.download_block_secs)
                self._mark_leased_or_raise(job_id, worker, now, "queued", clear)
                self.log("PC hit Drive's download quota; downloads paused for everyone")
            else:
                raise ValueError(f"unknown outcome {outcome!r}")
        self.log(f"{worker} reported {outcome}: {row['path']}")

    def release(self, job_id: int, worker: str) -> None:
        self._seen()
        now = self.now()
        with db.transaction(self.conn):
            row = self._leased_by(job_id, worker, now)
            self._mark_leased_or_raise(job_id, worker, now, "queued",
                                       {"worker": None, "lease_until": None})
        self.log(f"{worker} released: {row['path']}")

    def _requeue_if_still_expired(self, job_id: int, now: float) -> bool:
        """Guarded requeue of a lease that looked expired as of `now`. False if the
        lease was renewed (or the row moved on) since whoever is calling this read the
        row, in which case nothing is touched."""
        with db.transaction(self.conn):
            return self.conn.execute(
                "UPDATE videos SET stage='queued', worker=NULL, lease_until=NULL,"
                " updated_at=? WHERE id=? AND stage='leased' AND lease_until < ?",
                (now, job_id, now),
            ).rowcount == 1

    def expire_leases(self) -> int:
        now = self.now()
        expired = self.conn.execute(
            "SELECT * FROM videos WHERE stage='leased' AND lease_until < ?", (now,)
        ).fetchall()
        n = 0
        for row in expired:
            if not self._requeue_if_still_expired(row["id"], now):
                continue  # renewed since the SELECT above; leave it alone
            n += 1
            self.log(f"lease expired ({row['worker']}): {row['path']}")
            tmp = tmp_path(row["path"])
            try:
                if self.rclone.exists(tmp):
                    self.rclone.delete(tmp)
            except RcloneError as e:
                # Already requeued; the next claim's upload overwrites the tmp anyway.
                self.log(f"lease expired but {tmp} could not be removed yet: {e}")
        return n
