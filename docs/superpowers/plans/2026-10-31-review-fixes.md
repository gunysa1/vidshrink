# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every Important and Minor finding of the 2026-10-31 whole-project review, plus the cheap server-side saving check.

**Architecture:** Small, independent changes to the existing modules; no new modules. One new job-service endpoint (`/upload-permit`) and one new `videos` column (`original_trashed`), both backward compatible with older remote workers and older rows. Every task is TDD against the existing pytest suite (real rclone local remotes, generated clips; never a real Drive).

**Tech Stack:** Python 3.11+, sqlite3, rclone, ffmpeg, pytest.

**Spec:** `docs/superpowers/specs/2026-10-31-review-findings.md`

## Global Constraints

- Tests never touch a real Drive remote; they use the `conn`, `cfg`, `job_service`, `env`, `store` and `make_clip` fixtures in `tests/`.
- Only the server deletes or replaces an original. A remote worker may delete only its own `.vidshrink-tmp.mkv`, and only one it can prove it uploaded.
- Older remote workers must keep working against the new server, and a new worker against an older server (no `/upload-permit`).
- Rows written by older code (`original_trashed` NULL) must never lose the only copy of an encode.
- Match the surrounding code's comment density, naming and idiom. One commit per task, on the `review-fixes` branch.
- Run only the test files a task names while working; the full suite (`.venv/bin/pytest -q`, ~15 min) runs once at the end.

## Review Focus

1. A remote worker on the old code reports `uploaded` without ever asking for a permit: the server accepts it exactly as today (Task 2 test `test_report_uploaded_without_permit_still_accepted`).
2. A new worker against an old server gets 404 from `/upload-permit`: it uploads anyway (Task 2 test `test_upload_proceeds_when_permit_endpoint_missing`).
3. A row left in `swapping` by the old code, original already trashed, `original_trashed` NULL: the tmp is kept and the row flagged, never deleted (Task 7 test `test_swap_without_flag_and_missing_original_keeps_tmp`).
4. Google's per-user rate limit on the server's post-upload md5 check must not block uploads for 6 h (Task 1 test `test_throttled_md5_check_retries_and_does_not_block`).
5. A permit refusal while a worker holds a backlog: it keeps every file and lease and tries again after `retry_after` (Task 2 test `test_upload_waits_when_permit_refused`).

## Rulings made while planning

- Item 12 (bind address, threading): `vidshrink serve --host` already exists; the fix is documentation (Task 15). The single thread stays: no load problem has been seen, and a threaded server would need a lock around the store.
- Item 14 "claim stops at the first candidate over budget": fixed in `jobs._claim` only. The server loop's wait-for-budget behaviour is pinned by `test_budget_exhausted_buffers_then_waits` and stays.
- Item 14 "`_candidates` parses every probe", "`_next_job` LIMIT 500", "a release charges the download budget": left as they are. The first two have no measured cost; the third is the conservative choice (charge at claim).
- Item 7: when the original is missing and the row is not flagged as trashed by this server, the tmp is **kept** and the row goes to "needs attention", as today. Only a flagged row moves the tmp into place. This fixes "a deleted file comes back" without ever deleting a possibly-only copy.

---

### Task 1: Split the rate-limit classification

**Files:**
- Modify: `src/vidshrink/rclone.py` (`RATE_LIMIT_MARKERS`, `RcloneError`, `with_retries`)
- Modify: `src/vidshrink/worker.py` (`finish_upload`, the `e.rate_limited` branch)
- Modify: `src/vidshrink/pc_worker.py` (the three `e.rate_limited` uses: upload stage, `_job_error`, `_retry`)
- Test: `tests/test_rclone.py`, `tests/test_worker.py`, `tests/test_pc_worker.py`

**Interfaces:**
- Produces: `RcloneError.upload_limit -> bool` (markers `"uploadLimitExceeded"`, `"Received upload limit error"`), `RcloneError.throttled -> bool` (markers `"userRateLimitExceeded"`, `"User rate limit exceeded"`, `"rateLimitExceeded"`). `RcloneError.rate_limited` is removed; every caller names the one it means. `UPLOAD_LIMIT_MARKERS` and `THROTTLE_MARKERS` replace `RATE_LIMIT_MARKERS`.

- [ ] **Step 1: Write the failing tests** in `tests/test_rclone.py` (replace `test_rate_limited_detection` and `test_stop_on_upload_limit_error_is_rate_limited`):

```python
def test_upload_limit_detection():
    e = RcloneError(1, "... 403: uploadLimitExceeded ...")
    assert e.upload_limit and not e.throttled
    assert RcloneError(1, "Received upload limit error").upload_limit


def test_throttled_detection():
    e = RcloneError(1, "... 403: userRateLimitExceeded, User rate limit exceeded ...")
    assert e.throttled and not e.upload_limit


def test_with_retries_retries_throttled_but_not_upload_limit():
    calls = []
    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RcloneError(1, "userRateLimitExceeded")
        return "ok"
    assert with_retries(flaky, 3, 0, sleep=lambda s: None) == "ok"
    with pytest.raises(RcloneError):
        with_retries(lambda: (_ for _ in ()).throw(RcloneError(1, "uploadLimitExceeded")),
                     3, 0, sleep=lambda s: None)
```

In `tests/test_worker.py`, next to `test_rate_limit_blocks_uploads`:

```python
def test_throttled_md5_check_retries_and_does_not_block(conn, env, monkeypatch):
    # Google's short-term per-user rate limit on the post-upload md5 check is a
    # retry, not the daily cap: uploads stay unblocked and the row is swapped.
    ...  # arrange one ready row as test_rate_limit_blocks_uploads does; make
         # env.rclone.md5 raise RcloneError(1, "userRateLimitExceeded") once, then
         # return the real md5. Assert: not env.budget.blocked(); row stage == "done".
```

In `tests/test_pc_worker.py`, next to the existing upload-limit test (grep `upload_paused`):

```python
def test_throttled_upload_retries_instead_of_pausing(conn, setup, monkeypatch):
    # rclone.upload raises userRateLimitExceeded once, then succeeds: the job is
    # reported uploaded and no upload_paused report is sent.
```

- [ ] **Step 2: Run them**: `.venv/bin/pytest -q tests/test_rclone.py tests/test_worker.py tests/test_pc_worker.py -k "throttled or upload_limit"` → FAIL (`AttributeError: upload_limit`).

- [ ] **Step 3: Implement.** In `rclone.py` define the two marker tuples and the two properties; drop `rate_limited`. In `with_retries`, raise at once only on `e.upload_limit or e.download_quota or e.auth_failed`. In `worker.finish_upload`, block the budget only on `e.upload_limit`; a throttled error that survives the retries takes the existing `_fail(..., retry_stage="ready")` path. In `pc_worker`, the upload stage pauses only on `e.upload_limit`; `_job_error`'s download branch uses `e.throttled` (same behaviour: release, back off); `_retry` stops early only on `upload_limit`, `download_quota`, `auth_failed`. `grep -rn rate_limited src tests` must come back empty.

- [ ] **Step 4: Run** the same command plus `-k "rate_limit or upload_paused"` → PASS.

- [ ] **Step 5: Commit** `fix(rclone): retry Google's per-user rate limit; only the daily upload cap blocks`.

---

### Task 2: Upload permit for remote workers

**Files:**
- Modify: `src/vidshrink/jobs.py` (`JobStore`, `make_server` `/upload-permit`, `JobClient`)
- Modify: `src/vidshrink/pc_worker.py` (upload stage, before `self.rclone.upload`)
- Test: `tests/test_jobs_store.py`, `tests/test_jobs_http.py`, `tests/test_pc_worker.py`

**Interfaces:**
- Produces: `JobStore.upload_permit(job_id: int, worker: str, out_size: int) -> dict` → `{"ok": True}` or `{"ok": False, "retry_after": float}`; raises `LeaseLost` when the row isn't leased to `worker`, `ValueError` when `out_size <= 0`. Nothing is charged; the `uploaded` report still charges, as today.
- `POST /upload-permit` body `{id, worker, out_size}` → that dict; 409 on `LeaseLost`.
- `JobClient.upload_permit(job_id, worker, out_size) -> dict`.
- Consumes: `self.uploads.allows(n)`, `self.uploads.seconds_until_reset()`, `self._leased_by(...)`.

- [ ] **Step 1: Failing tests.** `tests/test_jobs_store.py`:

```python
def test_upload_permit_refused_when_budget_used_up(conn, cfg, store):
    job = store.claim("pc")["job"]
    store.uploads.add(cfg.daily_upload_limit - 10)
    r = store.upload_permit(job["id"], "pc", 100)
    assert r["ok"] is False and r["retry_after"] > 0
    store.uploads.add(-(cfg.daily_upload_limit - 10))  # or use a fresh day
    assert store.upload_permit(job["id"], "pc", 100) == {"ok": True}


def test_upload_permit_needs_the_lease(conn, store):
    job = store.claim("pc")["job"]
    with pytest.raises(LeaseLost):
        store.upload_permit(job["id"], "other", 100)


def test_report_uploaded_without_permit_still_accepted(conn, store):
    job = store.claim("pc")["job"]
    store.report(job["id"], "pc", "uploaded", out_size=100, out_md5="0" * 32, encode_secs=1.0)
    assert db.get(conn, job["id"])["stage"] == "remote_ready"
```

`tests/test_jobs_http.py`: `test_upload_permit_roundtrip(conn, job_service)` claims via `JobClient`, calls `client.upload_permit(...)` → `{"ok": True}`; after using up the budget through the store (open a second `db.connect(cfg.db_path)` + `Budget`) → `ok` False; wrong worker → `JobServiceError` with `status == 409`.

`tests/test_pc_worker.py`:

```python
def test_upload_waits_when_permit_refused(conn, setup, monkeypatch):
    # client.upload_permit answers {"ok": False, "retry_after": 1} once, then ok:
    # rclone.upload is not called before the second answer; the job ends uploaded.

def test_upload_proceeds_when_permit_endpoint_missing(conn, setup, monkeypatch):
    # client.upload_permit raises JobServiceError("not found", status=404):
    # the upload happens and the job is reported uploaded.
```

- [ ] **Step 2: Run** `-k upload_permit or permit` → FAIL (`AttributeError`).

- [ ] **Step 3: Implement.** Store: check `out_size`, `_leased_by` (raises LeaseLost), then `allows(out_size)`. HTTP: new `elif self.path == "/upload-permit"` using `int(body["id"])`, `body["worker"]`, `int(body["out_size"])`. Client: `_call("POST", "/upload-permit", {...})`. Worker upload stage, right after the `self.log(f"uploading ...")` line and before `uploading = True`: ask via `self._retry_job_call`; on `{"ok": False}` set `self._uploads_paused_until = time.monotonic() + retry_after`, `_set_state(lj, "verified")`, push back to the front of `_to_upload`, return (no report: the server already knows); on `JobServiceError` with `status == 404` proceed; on `LeaseLost` take the existing lease-lost handling of the upload stage.

- [ ] **Step 4: Run** the three test files → PASS.

- [ ] **Step 5: Commit** `feat(jobs): remote workers ask for an upload permit against the daily budget`.

---

### Task 3: Charge server downloads before they start

**Files:**
- Modify: `src/vidshrink/worker.py` (`start_download`, `finish_download`)
- Test: `tests/test_worker.py`

- [ ] **Step 1: Failing test**, next to `test_download_budget_counts_downloads`:

```python
def test_failed_download_is_still_charged(conn, env, monkeypatch):
    # The bytes left the account whether or not rclone finished; charge them when
    # the download starts, not after it succeeds.
    monkeypatch.setattr(env.rclone, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RcloneError(1, "boom")))
    ...  # run one job; assert env.download_budget.used() == row size
```

- [ ] **Step 2: Run** → FAIL (`used() == 0`).
- [ ] **Step 3: Implement.** Move `self.download_budget.add(row["size"])` into `start_download`, inside the `with db.transaction(...)` block, right after `started = db.mark_if(...)` and only when `started`. Remove it from `finish_download`.
- [ ] **Step 4: Run** `tests/test_worker.py -k budget` → PASS.
- [ ] **Step 5: Commit** `fix(worker): charge the download budget when a download starts`.

---

### Task 4: A Drive blip before a download costs no attempt

**Files:**
- Modify: `src/vidshrink/worker.py` (`start_download`)
- Test: `tests/test_worker.py`

- [ ] **Step 1: Failing test**:

```python
def test_transient_error_before_download_keeps_the_row_queued(conn, env, monkeypatch):
    # rclone.exists raises RcloneError twice, then works: the row is downloaded with
    # attempts == 0 and the worker's failure counter is 0.
```

(Use a row whose `final_path` differs from `path`, e.g. an `.mp4`, so `exists(final)` is called.)

- [ ] **Step 2: Run** → FAIL (row `failed`, attempts 1).
- [ ] **Step 3: Implement.** Wrap the existence check: `self._retry(lambda: self.rclone.exists(final))`. In `start_download`'s `except (RcloneError, OSError)` branch, when `e` is an `RcloneError` and not `e.download_quota`, log `f"could not check Drive before downloading {path}, will retry: {e}"`, `self.sleep(self.cfg.retry_base_delay)` and `return False` without `_on_error`. Quota errors keep going to `_on_error`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(worker): a Drive error before the download starts burns no attempt`.

---

### Task 5: Remote tmp deletes check one entry, size and md5

**Files:**
- Modify: `src/vidshrink/pc_worker.py` (`_delete_tmp_if_ours` and its callers)
- Test: `tests/test_pc_worker.py`

**Interfaces:**
- Produces: `_delete_tmp_if_ours(self, job: dict, tmp: str | None, md5: str | None, size: int | None = None) -> None`. Deletes only when `self.rclone.entries(tmp)` has exactly one entry, its md5 matches, and (when `size` is given) its `Size == size`.

- [ ] **Step 1: Failing tests**:

```python
def test_tmp_delete_refuses_duplicate_names(conn, setup, monkeypatch):
    # rclone.entries(tmp) returns two entries: nothing is deleted.

def test_tmp_delete_checks_size(conn, setup, monkeypatch):
    # md5 matches but Size differs from the verified out_size: not deleted.
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Use `entries()`; pass `out_size` from the upload stage (`_delete_tmp_if_ours(job, tmp, md5, out_size)`), and from `_pause_uploads`/`_abandon` where `self._verified.get(lj.id)` is available. Log `refusing to delete {tmp}: {n} entries` / `size differs`.
- [ ] **Step 4: Run** `tests/test_pc_worker.py -k tmp` → PASS.
- [ ] **Step 5: Commit** `fix(pc_worker): delete a tmp only when it is the one file of our size and md5`.

---

### Task 6: Validate `encoder`

**Files:**
- Modify: `src/vidshrink/jobs.py` (`claim`, `resume`), `src/vidshrink/pc_worker.py` (`load_pc_config`)
- Test: `tests/test_jobs_store.py`, `tests/test_jobs_http.py`, `tests/test_pc_worker.py` (or `tests/test_cli_pc.py`, wherever `load_pc_config` is tested)

- [ ] **Step 1: Failing tests**:

```python
def test_claim_rejects_unknown_encoder(conn, store):
    with pytest.raises(ValueError):
        store.claim("pc", encoder="x265")

def test_unknown_encoder_is_400(conn, job_service):  # /claim and /resume

def test_pc_config_rejects_unknown_encoder(tmp_path):
    # encoder = "x264" in the TOML -> ValueError naming the allowed values
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `if encoder not in scorer.REMOTE_ENCODERS: raise ValueError(f"unknown encoder {encoder!r}; one of {scorer.REMOTE_ENCODERS}")` at the top of `JobStore.claim` and `JobStore.resume`; the HTTP layer already maps `ValueError` to 400. Same check in `load_pc_config`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(jobs): reject unknown encoders on claim, resume and in the worker config`.

---

### Task 7: Move the tmp into place only when this server trashed the original

**Files:**
- Modify: `src/vidshrink/db.py` (`MIGRATIONS` += `("original_trashed", "INTEGER")`)
- Modify: `src/vidshrink/worker.py` (`_swap`, `_swap_failed`)
- Test: `tests/test_worker.py`, `tests/test_recover.py`, `tests/test_db.py`

- [ ] **Step 1: Failing tests**:

```python
def test_original_deleted_during_encode_is_not_replaced(conn, env, make_clip):
    # Remove the original from the local remote while the row is `ready`, then
    # run: the final path is NOT created, the tmp stays on the remote, the row is
    # in `swapping` with reason starting "swap: needs attention".

def test_swap_resumes_after_crash_once_original_is_trashed(conn, env):
    # Set original_trashed=1 on a `swapping` row whose original is gone and whose
    # tmp exists (as a crash between delete and move leaves it): run → `done`,
    # final exists.

def test_swap_without_flag_and_missing_original_keeps_tmp(conn, env):
    # Same layout, original_trashed NULL (a row from older code): the tmp is kept,
    # the row stays `swapping` with "needs attention".

def test_migration_adds_original_trashed(tmp_path):  # tests/test_db.py
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** In `_swap`, before `self._retry(lambda: self.rclone.delete(path))`: `db.mark(self.conn, row["id"], "swapping", original_trashed=1)`. In the `originals` empty branch: `if not row["original_trashed"]: raise SwapError("original missing but not trashed by this server; not replacing it")`. `_swap_failed` is unchanged: that error lands in its "original absent" branch, which keeps the tmp and flags the row. Rows re-read inside `_reconcile_swaps` carry the flag.
- [ ] **Step 4: Run** the three files → PASS.
- [ ] **Step 5: Commit** `fix(worker): replace a missing original only when this server trashed it`.

---

### Task 8: Remove orphan tmp files on the server side

**Files:**
- Modify: `src/vidshrink/worker.py` (`_swap_failed`, `finish_upload`)
- Test: `tests/test_worker.py`

- [ ] **Step 1: Failing tests**:

```python
def test_swap_failure_with_original_present_removes_tmp(conn, env):
    # Make the swap fail with the original present (e.g. a changed modtime): row
    # `failed`, tmp gone from the remote.

def test_final_upload_failure_removes_tmp(conn, env, monkeypatch):
    # rclone.upload raises a plain RcloneError on both attempts (max_attempts=2):
    # row `failed`, no tmp left on the remote.
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** In `_swap_failed`'s `original_present` branch, delete the tmp with `self._retry`, swallowing `RcloneError` with a log line. In `finish_upload`, after `self._fail(row, f"upload: {e}", retry_stage="ready")`, if `db.get(self.conn, vid)["stage"] == "failed"` delete the tmp the same way.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(worker): remove the tmp on Drive when a job is parked as failed`.

---

### Task 9: A dry run deletes nothing on Drive

**Files:**
- Modify: `src/vidshrink/worker.py` (the `self.jobs.expire_leases()` call in `run`)
- Test: `tests/test_worker.py` or `tests/test_worker_remote.py`

- [ ] **Step 1: Failing test**: `test_dry_run_does_not_expire_leases`: lease a row to "pc" with `lease_until` in the past and a tmp on the remote; `run(dry_run=True, limit=0)` → row still `leased`, tmp still there.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `if not dry_run: self.jobs.expire_leases()`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(worker): a dry run leaves other workers' leases and tmp files alone`.

---

### Task 10: Lock the database, not the workdir

**Files:**
- Modify: `src/vidshrink/worker.py` (`_lock` and its call site), `src/vidshrink/cli.py` if it builds the lock path
- Test: `tests/test_worker.py`

**Interfaces:**
- Produces: `_lock(lock_path: Path)`; called with `cfg.db_path.with_suffix(".lock")`. `AlreadyRunning(str(lock_path))`.

- [ ] **Step 1: Failing test**: `test_two_workdirs_one_db_cannot_run_together`: hold the lock for `cfg.db_path`, then `Worker(...).run(...)` with a different `workdir` raises `AlreadyRunning`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; update any test that asserts the old `.lock` location.
- [ ] **Step 4: Run** `tests/test_worker.py -k lock or running` → PASS.
- [ ] **Step 5: Commit** `fix(worker): one running worker per database, whatever its workdir`.

---

### Task 11: Keep the pinned Drive flags for every remote

**Files:**
- Modify: `src/vidshrink/cli.py` (the `if not cfg.remote.startswith("gdrive:")` block)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Failing test**: `test_rclone_args_kept_for_any_remote_name`: build the config through the CLI path with `--remote other:` and assert `"--drive-use-trash=true" in cfg.rclone_args`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Delete the block; rclone accepts backend flags for other backends (`test_default_rclone_args_are_pinned_and_accepted` already proves it on a local remote).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(cli): never drop --drive-use-trash for a Drive remote with another name`.

---

### Task 12: "Changed since scan" costs an attempt the second time

**Files:**
- Modify: `src/vidshrink/worker.py` (`finish_download` size mismatch), `src/vidshrink/jobs.py` (`report` outcome `changed`)
- Test: `tests/test_worker.py`, `tests/test_jobs_store.py`

- [ ] **Step 1: Failing tests**: `test_changed_twice_is_failed` in both files: first "changed" → `unprobed`, `attempts == 1`; with `attempts` already `max_attempts - 1` → `failed`, reason `"size on Drive differs from the download again"`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Both places: `attempts = row["attempts"] + 1`; if `attempts >= cfg.max_attempts` mark `failed` with that reason (clear lease fields in `jobs`), else mark `unprobed` as today but with `attempts=attempts`. `db.upsert_listing` already resets `attempts` when Drive's size or modtime really changed, so a genuine change still gets fresh attempts.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix: a file whose Drive size keeps differing from its download is parked, not re-downloaded forever`.

---

### Task 13: The server refuses an upload that doesn't show the saving

**Files:**
- Modify: `src/vidshrink/jobs.py` (`report`, outcome `uploaded`)
- Test: `tests/test_jobs_store.py`

- [ ] **Step 1: Failing test**: `test_report_uploaded_too_large_is_failed_and_tmp_removed`: claim; put a tmp on the remote; report `uploaded` with `out_size = row size * 0.9`; the row is `failed` with reason `"upload shows no saving: ..."`, the tmp is gone, `uploads.used()` unchanged, no exception.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Before marking `remote_ready`: `if out_size > row["size"] * (1 - self.cfg.min_saving)`: mark `failed` (attempts = `max_attempts`, lease fields cleared, that reason), delete the tmp (`exists` then `delete`, swallow `RcloneError` with a log), log, return.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(jobs): refuse an uploaded result larger than the required saving`.

---

### Task 14: Small items in one commit

**Files:**
- Modify: `src/vidshrink/jobs.py` (`_claim` budget `continue`; `"PC: "` → `f"{worker}: "`; module docstring), `src/vidshrink/local.py` (import `TMP_SUFFIX` from `vidshrink.rclone`), `src/vidshrink/local_queue.py` + `src/vidshrink/pc_worker.py` (move `_NUMBERED_PATTERNS` into `local_queue`; `pc_worker` imports it), `src/vidshrink/cli.py` (`_calibrate`: `pcfg.download_account or ("pc" if pcfg.download_remote else "main")`)
- Test: `tests/test_jobs_store.py`, `tests/test_calibrate.py` or `tests/test_cli_pc.py`

- [ ] **Step 1: Failing tests**: `test_claim_skips_a_candidate_over_budget_for_a_smaller_one` (two queued rows, budget fits only the smaller: it is leased); update `test_report_failed_twice_marks_failed` to expect `"pc: ..."` (the worker name); `test_calibrate_uses_configured_download_account`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the edits above. `grep -n "from vidshrink.pc_worker import" src/vidshrink/local_queue.py` must come back empty.
- [ ] **Step 4: Run** the named files plus `tests/test_local.py tests/test_local_queue.py tests/test_pc_worker.py` → PASS.
- [ ] **Step 5: Commit** `refactor: small review items (budget skip, failure labels, shared constants, calibrate account)`.

---

### Task 15: Documentation

**Files:**
- Modify: `docs/operations.md`, `docs/architecture.md`, `docs/pc-setup.md`, `docs/cloud-worker-setup.md`, `README.md`

- [ ] **Step 1: Edit.**
  - `operations.md`: requeue SQL gets `and stage='failed'`; replace "scan never touches `failed` rows" with: scan re-probes a failed row only when its size or modtime changed on Drive; add the `uploadLimitExceeded` vs `userRateLimitExceeded` distinction under worker alerts; add "A remote worker's upload waits for a permit when the day's 700 GB is used up (`vidshrink status` shows the budget)".
  - `architecture.md`: API table gains `POST /upload-permit` (`id`, `worker`, `out_size` → `{"ok": true}` or `{"ok": false, "retry_after": s}`; 409 if the lease was lost; older workers skip it); Budgets section: remote uploads ask before uploading; Stages: `changed since scan` costs an attempt the second time; `original_trashed` in the swap description; one lock per database.
  - `pc-setup.md` §0: `vidshrink serve --host <LAN IP>` binds the service to one interface; the unit file's default is `0.0.0.0`.
  - `cloud-worker-setup.md` §4: recommend scope `drive.file` for the upload remote (`gdrive:`) of a cloud VM or the PC, so Google itself limits the token to files that worker created; note it must be set when the remote is authorized, and that the server's own remote keeps full scope.
  - `README.md` Safety rules: unchanged wording ("size and md5") now matches the code; add "A worker asks the server before each upload, so the 700 GB/day applies to every upload."
- [ ] **Step 2: Check** `git diff --stat` touches only docs; read each changed paragraph once.
- [ ] **Step 3: Commit** `docs: review fixes (upload permit, rate limits, lock, drive.file scope, serve --host)`.

---

### Task 16: Full suite, merge, deploy

- [ ] **Step 1:** `.venv/bin/pytest -q` on the branch → `N passed`, 0 failed (≈15 min).
- [ ] **Step 2:** Merge `review-fixes` into `master` (fast-forward), push.
- [ ] **Step 3:** Deploy in the documented order: `systemctl --user restart vidshrink-serve`; `vidshrink` between jobs; `git -C ~/vidshrink pull` on both VMs and restart each unit as its next encode starts; ask PC-claude to pull and restart the tray between jobs.
- [ ] **Step 4:** Check `vidshrink status` and the three workers' logs for one claim, one permit and one swap each.
