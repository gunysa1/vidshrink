"""Leases encode jobs to remote workers (the PC). The server's DB stays the source of
truth; only the server verifies uploads and replaces originals."""
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.paths import final_path, is_valid_md5, tmp_path
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
        if self.uploads.blocked():
            return {"job": None, "retry_after": self.uploads.seconds_until_reset()}
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
                # Re-check right before claiming: another row may have taken this
                # final_path/tmp_path stem since the busy set above was computed.
                if final in self._busy_final_paths():
                    taken = False
                else:
                    taken = db.mark_if(self.conn, row["id"], ("queued",), "leased",
                                       worker=worker, encoder=encoder, final_path=final,
                                       reason=None, lease_until=self.now() + self.cfg.lease_secs)
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
            if outcome == "uploaded":
                # Idempotent: if a PC's earlier "uploaded" attempt actually committed
                # here but the response never made it back (dropped connection, 5xx,
                # timeout), its retry must not raise LeaseLost (the lease is already
                # cleared by the first attempt) and must not charge the upload budget
                # a second time. We only recognize this as the *same* upload: same
                # worker, same reported md5 -- otherwise something is genuinely wrong
                # and it's safer to raise.
                existing = db.get(self.conn, job_id)
                if existing is not None and existing["stage"] == "remote_ready" \
                        and existing["worker"] == worker:
                    if existing["out_md5"] == data.get("out_md5"):
                        self.log(f"{worker} re-reported uploaded (already recorded): "
                                 f"{existing['path']}")
                        return
                    raise LeaseLost(
                        f"job {job_id} is already remote_ready for {worker} with a "
                        "different md5")
            row = self._leased_by(job_id, worker, now)
            clear = {"worker": None, "lease_until": None}
            if outcome == "uploaded":
                out_size, out_md5 = int(data["out_size"]), data["out_md5"]
                if out_size <= 0 or not is_valid_md5(out_md5):
                    raise ValueError(
                        f"invalid uploaded report: out_size={out_size} out_md5={out_md5!r}")
                self._mark_leased_or_raise(job_id, worker, now, "remote_ready", {
                    "out_size": out_size, "out_md5": out_md5,
                    "encode_secs": float(data["encode_secs"]), "reason": None,
                    # Keep `worker` (don't clear it) so a retried duplicate report of
                    # this same success can still be recognized as idempotent above.
                    "worker": worker, "lease_until": None})
                self.uploads.add(out_size)
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
            elif outcome == "upload_limited":
                self.uploads.block(self.cfg.rate_limit_block_secs)
                self._mark_leased_or_raise(job_id, worker, now, "queued", clear)
                self.log("PC hit Drive's upload limit; uploads paused for everyone")
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


class JobServiceError(Exception):
    """`status` is the HTTP status code when known (e.g. 400, 500), or None for a
    connection failure (couldn't reach the service at all) — both are used by
    callers to decide whether a retry is worth it: None/5xx is transient, a 4xx
    (other than 409, which raises LeaseLost instead) is not."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


REQUEST_TIMEOUT_SECS = 30
MAX_REQUEST_BODY_BYTES = 1 << 20  # 1 MiB
SWEEP_INTERVAL_SECS = 60


def load_or_create_token(path: Path) -> str:
    path = Path(path)
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    except FileExistsError:
        # Another process created it first; read and return its token
        return path.read_text().strip()
    return token


class _SweepingHTTPServer(HTTPServer):
    """Sweeps expired leases on its own, so a lease still expires even if no PC
    ever calls in to trigger JobStore.claim()'s sweep. service_actions() is
    called by serve_forever() on every poll, in the server's own thread -- the
    same thread that owns the store's sqlite3 connection -- so this is safe
    without any extra locking."""

    def __init__(self, *args, store: JobStore, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = store
        self._last_sweep = -SWEEP_INTERVAL_SECS

    def service_actions(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < SWEEP_INTERVAL_SECS:
            return
        self._last_sweep = now
        try:
            self._store.expire_leases()
        except Exception as e:  # never let a sweep blip kill serve_forever
            self._store.log(f"lease sweep failed: {e!r}")


def make_server(store: JobStore, token: str, host: str, port: int) -> HTTPServer:
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SECS

        def log_message(self, fmt, *args):  # keep journald quiet; JobStore logs events
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "").encode()
            if hmac.compare_digest(got, expected):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._authorized():
                return
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_REQUEST_BODY_BYTES:
                    code = 400 if length < 0 else 413
                    self._send(code, {"error": "payload too large" if code == 413 else "bad request: invalid content length"})
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/claim":
                    self._send(200, store.claim(body["worker"], body.get("encoder", "nvenc")))
                elif self.path == "/heartbeat":
                    self._send(200, store.heartbeat(int(body["id"]), body["worker"]))
                elif self.path == "/report":
                    extra = {k: v for k, v in body.items() if k not in ("id", "worker", "outcome")}
                    store.report(int(body["id"]), body["worker"], body["outcome"], **extra)
                    self._send(200, {"ok": True})
                elif self.path == "/release":
                    store.release(int(body["id"]), body["worker"])
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except LeaseLost as e:
                self._send(409, {"error": str(e)})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
                self._send(400, {"error": f"bad request: {e}"})
            except Exception as e:  # keep serving; the PC retries
                store.log(f"job service error on {self.path}: {e!r}")
                self._send(500, {"error": repr(e)})

    return _SweepingHTTPServer((host, port), Handler, store=store)


class JobClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 409:
                raise LeaseLost(detail) from None
            raise JobServiceError(f"HTTP {e.code} on {path}: {detail}", status=e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise JobServiceError(f"cannot reach job service at {self.base}: {e}") from None

    def health(self) -> dict:
        return self._call("GET", "/health")

    def claim(self, worker: str, encoder: str = "nvenc") -> dict:
        return self._call("POST", "/claim", {"worker": worker, "encoder": encoder})

    def heartbeat(self, job_id: int, worker: str) -> dict:
        return self._call("POST", "/heartbeat", {"id": job_id, "worker": worker})

    def report(self, job_id: int, worker: str, outcome: str, **data) -> None:
        self._call("POST", "/report", {"id": job_id, "worker": worker, "outcome": outcome, **data})

    def release(self, job_id: int, worker: str) -> None:
        self._call("POST", "/release", {"id": job_id, "worker": worker})
