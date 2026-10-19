# Pipelined Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the PC/cloud daily download caps. Let every worker (PC, cloud VM,
home server) download, encode and upload in parallel, so its encoder never idles,
and keep downloaded work across restarts.

**Architecture:**
- **Job service.** It learns to hold several leases per worker and turns a lease
  into a long reservation once its download completes. It also gains a batch
  heartbeat and a `/resume` call.
- **Remote worker.** Its one-job-at-a-time loop becomes three stage threads
  (claim+download → encode → verify+upload+report) linked by a local job queue
  that lives on disk as marker files. A new module, `local_queue.py`, owns that
  queue.
- **Server worker.** It keeps all database work on its main thread. Downloads and
  uploads move into a background transfer helper, polled from the existing encode
  tick.

**Tech Stack:** Python 3.11+, SQLite (WAL), rclone, ffmpeg, pytest, stdlib
`threading`/`http.server`.

**Spec:** `docs/superpowers/specs/2026-10-19-pipelined-workers-design.md`

## Global Constraints

- **Python and imports:**
  - Python 3.11+.
  - `pc_worker.py` and anything it imports stay importable on Windows: no
    `fcntl`, no `vidshrink.worker`. `tests/test_cli_windows_import.py` must keep
    passing.
- **Subprocesses** go through `procflags.spawn_kwargs` (below-normal priority on
  Windows).
- **Tests:**
  - Tests never touch a real Drive; use the existing local rclone remotes and
    `make_clip`.
  - Never run two pytest sessions at once. The full suite takes about 13 minutes.
- **Safety:**
  - Only the server deletes or replaces originals.
  - A remote worker deletes only its own provable tmp: `_delete_tmp_if_ours`,
    which requires the path to equal `tmp_path(job["path"])` and the md5 to match.
- **Budgets:**
  - Main account download budget stays at 500 GB/day.
  - Upload budget stays at 700 GB/day.
  - The PC and cloud download budgets become uncapped (limit `None`), still
    recorded per Pacific day.
  - `downloadQuotaExceeded` still blocks that account for `download_block_secs`
    (12 h).
- **Reservation lengths** (worker setting `reserve_days`): PC 7, cloud VM 1.
  Downloads not yet complete keep the 30-minute `lease_secs`.
- **Queue limits:**
  - `queue_disk_gb`: PC 300, cloud VM 120.
  - `queue_hours`: 24 on both.
  - The first job into an empty queue is always allowed past `queue_hours`.
- **Old clients:** a worker on today's code (single `id` heartbeat, `resume=True`
  claim, no `downloaded` report) must keep working against the new server, with
  exactly today's 30-minute behaviour.
- **Server database access:** each thread uses its own SQLite connection. The
  server worker does all database writes on its main thread, and background
  threads only run rclone transfers.

## Review Focus

1. **Job service unreachable when a remote worker starts.**
   - Expected: local downloaded and encoded files are kept, and resume is
     retried.
   - Files are deleted only on an explicit per-job refusal from `/resume`.
   - Test: Task 5, `test_resume_unreachable_keeps_local_files`.
2. **Lease sweep while the PC is off.**
   - Expected: `expire_leases()` (run every 60 s by `serve` and by the server
     worker) leaves a reserved row alone until its `lease_until`, which is up to
     7 days away.
   - Test: Task 2, `test_expire_leases_keeps_reservation`.
3. **Disk fills even though `queue_disk_gb` isn't reached.** For example, D: has
   less free space than the setting allows.
   - Expected: the downloader also respects the real free space. It sends
     `free_bytes` and won't claim when `size*disk_factor + disk_reserve > free`.
   - Test: Task 3, `test_admits_respects_real_free_space`.
4. **Upload limit hit with several encoded files waiting.**
   - Expected: no encoded file is thrown away. The uploader pauses until the
     server's upload block lifts, then uploads them.
   - Test: Task 4, `test_upload_limited_keeps_encoded_files`.
5. **Original changed on Drive while the PC was off.**
   - Expected: `/resume` refuses that job, and its local files are deleted.
   - Test: Task 2, `test_resume_refuses_changed_original`.
   - Test: Task 5, `test_refused_resume_deletes_local_files`.

---

### Task 1: Uncapped worker download budgets

**Files:**
- Modify: `src/vidshrink/budget.py` (`Budget.limit: int | None`)
- Modify: `src/vidshrink/config.py` (`daily_pc_download_limit`,
  `daily_cloud_download_limit`)
- Modify: `src/vidshrink/cli.py` (status lines for the PC and cloud account
  downloads)
- Modify: `src/vidshrink/tray.py` (`status_text` when `download_limit` is
  `None`)
- Test: `tests/test_budget.py`, `tests/test_cli.py`, `tests/test_tray.py`,
  `tests/test_jobs_store.py`

**Interfaces:**
- Produces: `Budget(conn, limit: int | None, ...)`.
  - With `limit=None`, `allows(n)` is False only while `blocked()`.
  - `seconds_until_reset()` is unchanged.
- Produces: claim and heartbeat responses carry `"download_limit": None` for
  uncapped accounts.

- [ ] **Step 1: Write failing tests**
  - `test_uncapped_budget_allows_any_amount`: `Budget(conn, None, kind="pc_download")`;
    after `add(10**13)`, `allows(10**13)` is True.
  - `test_uncapped_budget_still_blocks`: after `block(3600)`, `allows(1)` is
    False.
  - `test_status_shows_no_cap`: with PC and cloud usage of 168 GB and 34 GB,
    `status_report` contains `"PC account downloaded today: 168 GB (no cap)"`
    and `"Cloud account downloaded today: 34 GB (no cap)"`.
  - `test_tray_status_uncapped`: with `download_today == (2e11, None)`,
    `status_text()` shows `"200 GB today"` and no `"/"`.
  - `test_claim_not_refused_by_pc_volume`: with 900 GB already charged to
    `pc_download` today, a `download_account="pc"` claim still returns a job.
- [ ] **Step 2: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_budget.py tests/test_cli.py tests/test_tray.py tests/test_jobs_store.py -q`
- [ ] **Step 3: Implement.**
  - Set both config defaults to `None`, with a comment: "own accounts, Plex
    never reads through them; only Google's limit applies (2026-10-19)".
  - Keep the type annotation `int | None`.
- [ ] **Step 4: Run the same command and confirm it passes.**
- [ ] **Step 5: Commit.**
  - Message: `feat(budget): PC and cloud download accounts are uncapped`

### Task 2: Job service — multiple leases, reservations, batch heartbeat, resume

**Files:**
- Modify: `src/vidshrink/db.py` (add migration `("reserve_secs", "REAL")`)
- Modify: `src/vidshrink/jobs.py` (`JobStore`, `make_server`, `JobClient`)
- Test: `tests/test_jobs_store.py`, `tests/test_jobs_http.py`

**Interfaces:**
- Produces, in the server and its HTTP layer:
  - `JobStore.claim(...)`:
    - Unchanged signature.
    - The returned `job` dict gains `"expected_hours": float`, from
      `scorer.expected_hours(conn, probe, encoder)`.
  - `JobStore.report(job_id, worker, "downloaded", reserve_secs=float)`:
    - The row stays `leased`.
    - It sets `reserve_secs = min(reserve_secs, 30*86400)` and
      `lease_until = now + reserve_secs`.
    - It raises `LeaseLost` if the row isn't leased to that worker.
    - It is idempotent.
  - `JobStore.heartbeat(job_id, ...)` (old shape): extends by the row's
    `reserve_secs` if set, else by `lease_secs`.
  - `JobStore.heartbeat_many(ids: list[int], worker, download_account) -> dict`:
    - Returns `{"cancel": [ids not validly leased], "download_used": ...,
      "download_limit": ...}`.
    - Extends each valid id the same way as `heartbeat`.
  - `JobStore.resume(worker, encoder, download_account, jobs: list[dict]) -> dict`:
    - Each item in `jobs` is `{"id", "size", "modtime", "reserve_secs"}`.
    - Returns `{"resumed": [job dicts as in claim, plus "expected_hours"],
      "refused": [ids]}`.
    - **Confirms a job when** its row size and modtime equal the sent values,
      and either:
      - it is `leased` to this worker (any `lease_until`, even expired but not
        yet swept), or
      - it is `queued` and its final path isn't busy.
    - **A confirmed job** becomes leased to the worker with
      `lease_until = now + reserve_secs` and `reserve_secs` stored. No download
      budget is charged.
    - **Everything else is refused.**
  - `expire_leases()`: unchanged rule (`lease_until < now`). Reserved rows
    expire only when their reservation does.
  - `report()` outcomes `failed`, `not_worth_it`, `changed`, `download_quota`,
    `upload_limited` and `uploaded` clear `reserve_secs` back to NULL along with
    the worker and lease, and so does `release`.
  - `report(..., "upload_paused")`:
    - Blocks uploads for `rate_limit_block_secs`.
    - Keeps the lease and reservation.
    - It's for a pipelined worker that keeps its encoded file.
- Produces, over HTTP:
  - `POST /heartbeat` with `{"ids": [...]}` returns `heartbeat_many`. With
    `{"id": n}` it behaves as today.
  - `POST /resume` takes `{"worker", "encoder", "download_account", "jobs": [...]}`.
    It returns 400 if `jobs` isn't a list of objects with integer `id` and
    `size` and a string `modtime`.
- Produces, in `JobClient`:
  - `heartbeat_many(ids, worker, download_account="main") -> dict`
  - `resume(worker, encoder, download_account, jobs) -> dict`
  - `report(..., "downloaded", reserve_secs=...)` uses the existing `report`
    passthrough.

- [ ] **Step 1: Write failing store tests in `tests/test_jobs_store.py`.**
  - `test_claim_returns_expected_hours`
  - `test_worker_can_hold_several_leases`: two claims by the same worker, both
    rows `leased`.
  - `test_downloaded_report_sets_reservation`: `lease_until` is about
    `now + 7*86400`, and the row is still `leased`.
  - `test_reserve_secs_capped_at_30_days`
  - `test_expire_leases_keeps_reservation`:
    - At `now + 3 days`, the row is still `leased`.
    - At `now + 7 days + 1 s`, it is `queued`, and `reserve_secs` is NULL.
  - `test_old_heartbeat_extends_reserved_row_by_reservation`
  - `test_heartbeat_many_reports_lost_ids`: one valid id, plus one released id
    that comes back in `cancel`.
  - `test_resume_leased_row`
  - `test_resume_queued_unclaimed_row_releases_to_worker_without_download_charge`
  - `test_resume_refuses_row_taken_by_other_worker`
  - `test_resume_refuses_done_row`
  - `test_resume_refuses_changed_original`: the row's size differs from the
    sent size.
  - `test_upload_paused_keeps_lease_and_blocks_uploads`
  - `test_release_clears_reservation`
- [ ] **Step 2: Write failing HTTP tests in `tests/test_jobs_http.py`.**
  - `test_http_heartbeat_ids`
  - `test_http_resume_roundtrip`
  - `test_http_resume_bad_jobs_is_400`
  - `test_old_client_single_heartbeat_unchanged`: lease extended by exactly
    1800 s.
- [ ] **Step 3: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_jobs_store.py tests/test_jobs_http.py -q`
- [ ] **Step 4: Implement in `db.py` and `jobs.py`.**
  - Every state change stays inside `db.transaction` and keeps its lease guard
    in the UPDATE's WHERE clause, as `_update_leased` does today.
  - `resume` re-checks the busy-stem guard inside the transaction, like `_claim`.
- [ ] **Step 5: Run the same command and confirm it passes.** Then run
  `tests/test_worker_remote.py tests/test_pc_worker.py -q` to confirm old
  clients are unaffected.
- [ ] **Step 6: Commit.**
  - Message: `feat(jobs): several leases per worker, download reservations, batch heartbeat, resume`

### Task 3: Local job queue (remote worker, pure logic)

**Files:**
- Create: `src/vidshrink/local_queue.py`
- Modify: `src/vidshrink/pc_worker.py`
  - `PCConfig` fields `queue_disk_gb: float = 300.0`, `queue_hours: float = 24.0`
    and `reserve_days: float = 7.0`.
  - `clean_workdir(workdir, keep_ids: set[int])` replaces `keep_id`.
- Test: `tests/test_local_queue.py` (new), `tests/test_pc_worker.py` (calls to
  `clean_workdir`)

**Interfaces:**
- Produces, in `local_queue.py` (no imports from `vidshrink.worker`):
  - `@dataclass class LocalJob`:
    - Fields: `job: dict` (the claim's job dict, including `expected_hours`) and
      `state: str`.
    - `state` is one of `"downloading"`, `"downloaded"`, `"encoding"`,
      `"encoded"`, `"uploading"`.
    - Properties `id`, `src`, `out` and `log` hold workdir paths, named
      `<id>.src`, `<id>.out.mkv` and `<id>.log`.
  - Markers:
    - `<id>.job.json`: the job dict, written atomically (write-then-`os.replace`)
      when the claim is accepted.
    - `<id>.downloaded`: existing format, the size.
    - `<id>.encoded`: existing format, the encode seconds.
  - `class LocalQueue(workdir: Path, queue_disk_bytes: int, queue_hours: float,
    disk_factor: float, disk_reserve: int)`:
    - `scan() -> tuple[list[LocalJob], list[int]]`:
      - Reads `*.job.json`.
      - Returns resumable jobs: `encoded` when `.encoded` is valid and `out`
        exists; `downloaded` when `.downloaded` is valid and the size of `src`
        equals `job["size"]`.
      - Also returns the ids of incomplete jobs, whose files the caller deletes.
    - `admits(size: int, expected_hours: float, free_bytes: int) -> bool`:
      - False if the bytes on disk plus `size` would exceed `queue_disk_bytes`.
      - False if `size*disk_factor + disk_reserve > free_bytes`.
      - False if the sum of `expected_hours` over jobs not yet `encoded`, plus
        this job's, exceeds `queue_hours`, unless no job is waiting to be encoded.
      - Bytes on disk come from `src` and `out` sizes in the workdir.
    - `forget(job_id)`: deletes every file of that id, using the same name
      patterns as `clean_workdir`.
    - `held_ids() -> set[int]`.
- Produces, in `pc_worker.py`: `clean_workdir(workdir, keep_ids: set[int]) ->
  list[Path]` keeps every listed id and also deletes stray `<id>.job.json`
  files.

- [ ] **Step 1: Write failing tests in `tests/test_local_queue.py`.**
  - `test_scan_finds_downloaded_and_encoded`
  - `test_scan_reports_partial_download_as_incomplete`: `.job.json` and `src`
    are present but `.downloaded` is missing.
  - `test_scan_ignores_corrupt_job_json`
  - `test_admits_disk_cap`
  - `test_admits_hours_cap_allows_first_job_into_empty_queue`: one 30-hour job
    into an empty queue is admitted, and a second 1-hour job is refused.
  - `test_admits_respects_real_free_space`
  - `test_forget_deletes_only_that_id`
  - `test_clean_workdir_keeps_all_held_ids`
- [ ] **Step 2: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_local_queue.py -q`
- [ ] **Step 3: Implement.** Add `r"^(\d+)\.job\.json(?:\.tmp)?$"` to
  `_NUMBERED_PATTERNS`.
- [ ] **Step 4: Run the same command and confirm it passes.** Then run
  `tests/test_pc_worker.py tests/test_cli_windows_import.py -q`.
- [ ] **Step 5: Commit.**
  - Message: `feat(pc-worker): on-disk local job queue with disk, free-space and hours limits`

### Task 4: Remote worker pipeline (fresh claims)

**Files:**
- Modify: `src/vidshrink/pc_worker.py` (`PCWorker.run` and `_do_job` are split
  into stage threads)
- Test: `tests/test_pc_worker.py` (rewrite the affected tests to the new flow)

**Interfaces:**
- Consumes: Task 2's `JobClient.heartbeat_many`, `report("downloaded",
  reserve_secs=...)`, `report("upload_paused")` and the claim's
  `job["expected_hours"]`.
- Consumes: Task 3's `LocalQueue` and `LocalJob`.
- **Produces, inside `PCWorker`:**
  - **Threads:**
    - `_downloader`: claims while `LocalQueue.admits(...)` holds, using the
      size and `expected_hours` from the claim response. Downloads one at a
      time.
    - `_encoder`: one at a time, oldest `downloaded` first.
    - `_uploader`: verifies, uploads and reports, one at a time.
    - `_heartbeat`: one `heartbeat_many` covering every held id, every
      `heartbeat_secs`.
  - **Claiming when the queue is full:**
    - The downloader only claims when `admits(row size, row hours, free)` is
      True for the worst case.
    - The size isn't known before claiming, so it claims with
      `free_bytes = min(real free, queue_disk_bytes - bytes on disk)` and
      `max_hours = remaining hours`. It passes `cfg.max_hours` if that is
      lower, and omits `max_hours` entirely when the queue holds no job
      waiting to be encoded.
  - **After a download completes:** write `.downloaded`, then send
    `report("downloaded", reserve_secs=cfg.reserve_days*86400)`.
  - **A cancelled id from the heartbeat** cancels only that job's stage work and
    forgets it locally. Its tmp is deleted only through `_delete_tmp_if_ours`.
  - **Upload limit hit:** report `upload_paused`, keep `.encoded` and `out`, and
    wait `rate_limit_block_secs` before trying uploads again. Don't use
    `upload_limited`, which requeues the job.
  - **Local faults:** a `_LocalFault` in any stage stops all stages and sends
    the alert, as today.
  - **Every other per-job outcome** (`failed`, `not_worth_it`, `changed`,
    `download_quota`, the auth fault, the poison-job guard) behaves as today
    for that one job.
  - **`self.stage` and `self.current_path`** are replaced by `status_line() ->
    str`, for example `"Encoding X (3 waiting) · Uploading Y"`. Keep `done`
    and `download_today`.
  - **`run()`:** returns when all stage threads have exited.

- [ ] **Step 1: Write failing tests in `tests/test_pc_worker.py`.**
  - `test_pipeline_downloads_next_while_encoding`:
    - The encode fn blocks on an Event.
    - Assert a second job reaches `downloaded` locally, and the server row is
      reserved, before the first encode is released.
  - `test_queue_hours_limits_prefetch`:
    - `queue_hours=1` and jobs of about 0.6 h each.
    - At most 2 jobs are held while the first encodes.
  - `test_queue_disk_limits_prefetch`
  - `test_heartbeat_many_covers_all_held_jobs`
  - `test_server_cancel_drops_only_that_job`
  - `test_upload_limited_keeps_encoded_files`:
    - The first upload raises a rate-limited RcloneError.
    - The row is still leased, `out` still exists, and uploads are blocked
      server-side.
  - `test_full_job_then_server_swaps` (kept): still ends `done` after the
    server swaps.
- [ ] **Step 2: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_pc_worker.py -q`
- [ ] **Step 3: Implement.**
  - Keep the existing safety helpers unchanged and call them per job:
    `_report_uploaded`, `_delete_tmp_if_ours`, `_safe_tmp`, `_retry`,
    `_retry_job_call`.
  - Rewrite the existing tests in `test_pc_worker.py` that assume one job at a
    time. Keep each test's intent and change only how it drives the worker.
- [ ] **Step 4: Run the same command and confirm it passes.** Then run
  `tests/test_cli_pc.py tests/test_cli_windows_import.py -q`.
- [ ] **Step 5: Commit.**
  - Message: `feat(pc-worker): pipeline download, encode and upload as separate stages`

### Task 5: Remote worker restart resume and Stop semantics; tray

**Files:**
- Modify: `src/vidshrink/pc_worker.py` (startup, `stop`)
- Modify: `src/vidshrink/tray.py` (`status_text` uses `status_line()`)
- Test: `tests/test_pc_worker.py`, `tests/test_tray.py`

**Interfaces:**
- Consumes: Task 2's `JobClient.resume`, and Task 3's `LocalQueue.scan` and
  `forget`.
- **Produces, on startup:**
  1. `scan()`.
  2. `forget` each incomplete id, and `release` each one on the server (best
     effort; ignore `LeaseLost`).
  3. Call `resume` with the remaining jobs. Retry on `JobServiceError` every
     `idle_secs` until it answers or Stop is pressed. Delete nothing while
     unanswered.
  4. Queue each resumed job at its local state (`downloaded` goes to the
     encoder, `encoded` goes to the uploader), and `forget` each refused id.
  5. Only then start the downloader.
- **Produces, for Stop now (`stop(now=True)`):**
  - It cancels the running download, encode and upload.
  - It deletes partial `src` and `out` files of the cancelled stage, keeps all
    `.downloaded` and `.encoded` jobs, and doesn't release them; their
    reservations stay.
  - It releases only jobs that never finished downloading.
- **Produces, for Stop after this file (`stop(now=False)`):**
  - The downloader stops claiming and aborts an unfinished download, releasing
    that job.
  - The encoder finishes its current file and then stops.
  - The uploader drains every encoded job and then stops.
  - `downloaded` jobs stay on disk, reserved.
- **Produces, in the tray:** `status_text()` shows `worker.status_line()`, and
  `status_text(short=True)` keeps the existing tooltip length limit.

- [ ] **Step 1: Write failing tests.**
  - `test_restart_resumes_downloaded_job_without_redownload`: after a restart,
    the download rclone is never called for that id.
  - `test_restart_resumes_encoded_job_straight_to_upload`
  - `test_resume_unreachable_keeps_local_files`:
    - The job service is down for the first two resume calls.
    - Files stay, and the third call resumes them.
  - `test_refused_resume_deletes_local_files`
  - `test_partial_download_released_on_start`
  - `test_stop_now_keeps_downloaded_jobs_reserved`
  - `test_stop_after_drains_uploads_keeps_downloaded`
  - `test_tray_shows_status_line`
- [ ] **Step 2: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_pc_worker.py tests/test_tray.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command and confirm it passes.** Then run
  `tests/test_cli_pc.py tests/test_cli_windows_import.py -q`.
- [ ] **Step 5: Commit.**
  - Message: `feat(pc-worker): resume downloaded and encoded jobs after restart; stop keeps reserved work`

### Task 6: Server worker — `downloaded` stage and restart reuse

**Files:**
- Modify: `src/vidshrink/db.py`
  - Add `"downloaded"` to `IN_PROGRESS`, after `"downloading"`, so the busy-stem
    guard covers it.
- Modify: `src/vidshrink/worker.py`
  - Split `prepare()` into `start_download(row) -> bool` (marks `downloading`),
    `finish_download(row) -> bool` (checks size, marks `downloaded`) and
    `encode_and_verify(row)` (`encoding` → `verifying` → `ready`, `failed` or
    `not_worth_it`).
  - Update `recover()`.
- Test: `tests/test_recover.py`, `tests/test_worker.py`

**Interfaces:**
- Produces: `Worker.start_download(row) -> bool`, `Worker.finish_download(row) ->
  bool` and `Worker.encode_and_verify(row) -> None`.
  - Each keeps the error handling `prepare()` has today: disk too small, missing
    tool, download quota, changed since scan.
  - `prepare(row)` stays as a wrapper that calls all three in order, so the
    existing tests keep passing until Task 7.
- **Produces, in `recover()`, for a `downloaded` row:**
  - It stays `downloaded` if `_src(id)` exists with size equal to `row["size"]`.
  - Otherwise it goes back to `queued` with the existing free-first-interruption
    rule.
  - `downloading`, `encoding` and `verifying` rows keep today's handling, with
    one change: an `encoding` or `verifying` row whose src is complete goes back
    to `downloaded`, not `queued`, and its partial output is deleted. This
    counts as an interruption for the existing rule.

- [ ] **Step 1: Write failing tests.**
  - `test_recover_keeps_complete_download`
  - `test_recover_requeues_truncated_download`
  - `test_recover_interrupted_encode_with_complete_src_goes_to_downloaded`
  - `test_downloaded_row_blocks_same_stem_claim` (in `test_jobs_store.py`)
- [ ] **Step 2: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_recover.py tests/test_worker.py tests/test_jobs_store.py -q`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the same command and confirm it passes.**
- [ ] **Step 5: Commit.**
  - Message: `feat(worker): downloaded stage, reuse complete downloads after restart`

### Task 7: Server worker — background download and upload

**Files:**
- Create: `src/vidshrink/transfer.py`
- Modify: `src/vidshrink/worker.py` (`run()`, `_encode_tick`, `upload_and_swap`)
- Test: `tests/test_transfer.py` (new), `tests/test_worker.py`

**Interfaces:**
- Consumes: Task 6's `start_download`, `finish_download` and `encode_and_verify`.
- Produces, in `transfer.py`: `class Transfer(fn: Callable[[], None])`.
  - It starts a daemon thread that runs `fn`.
  - `done() -> bool`.
  - `result() -> None` re-raises `fn`'s exception in the caller.
  - `cancel()` is used only at shutdown.
  - `fn` must not touch the database.
- **Produces, in the `Worker` loop.** All database work stays on the main
  thread:
  - **Prefetch:** at most one `Transfer` downloading and at most one row in
    `downloaded` waiting. Start a download when there is none, the waiting
    count is 0, `_next_job()` returns a row, the download budget allows it and
    `_wait_for_disk` passes without waiting.
  - **Upload:** at most one `Transfer` uploading a `ready` row, when the upload
    budget allows it.
    - When it finishes, the main thread checks the md5 and swaps, using today's
      `upload_and_swap` logic split around the transfer.
  - **During an encode:** `_encode_tick` polls both transfers. It finishes
    completed ones (`finish_download`, or the md5 check and swap), starts new
    ones and services `remote_ready` as today. It never raises.
  - **Between encodes:** if a row is `downloaded`, call `encode_and_verify` on
    it. Otherwise wait in `idle_poll_secs` steps for the download transfer.
- **The existing budget-sleep, failure-breaker, `max_ready`, dry-run and
  `swap_retry_secs` behaviour is unchanged.** In dry-run mode, uploads still
  end in `dry_run` and nothing is swapped.

- [ ] **Step 1: Write failing tests.**
  - `test_transfer_reports_success_and_exception`
  - `test_server_downloads_next_during_encode`:
    - The fake encode waits until the second row is `downloaded`, with a
      timeout.
    - The test fails if it never happens.
  - `test_server_uploads_during_encode`:
    - A `ready` row exists before the encode starts.
    - It reaches `done` before the encode returns.
  - `test_server_download_quota_in_background_blocks_and_requeues`
  - `test_dry_run_still_never_swaps`
- [ ] **Step 2: Run them and confirm they fail.**
  - Run: `.venv/bin/pytest tests/test_transfer.py tests/test_worker.py tests/test_recover.py -q`
- [ ] **Step 3: Implement.** Then remove the `prepare()` wrapper if no caller
  remains.
- [ ] **Step 4: Run the full suite and confirm it passes.**
  - Run: `.venv/bin/pytest -q` (one session only, about 13 min).
- [ ] **Step 5: Commit.**
  - Message: `feat(worker): download next file and upload results while encoding`

### Task 8: Docs and deploy settings

**Files:**
- Modify: `README.md` (how-it-works and safety bullets on budgets)
- Modify: `docs/architecture.md`
  - Pipeline stages.
  - Reservations.
  - The `/heartbeat` `ids`, `/resume` and `downloaded`/`upload_paused` API.
  - Budgets.
- Modify: `docs/operations.md`
  - Status lines.
  - What Stop now and Stop after do with downloaded files.
  - Deploy order: the server first and between jobs, then the PC and VM.
- Modify: `docs/cloud-worker-setup.md`
  - The example `config.toml` gains `queue_disk_gb = 120`, `queue_hours = 24`
    and `reserve_days = 1`.
- Modify: `docs/pc-setup.md`
  - The defaults `queue_disk_gb = 300`, `queue_hours = 24` and
    `reserve_days = 7`.

- [ ] **Step 1: Edit the docs.**
  - Match the existing style: short sentences and bullets, no personal paths or
    names.
- [ ] **Step 2: Check the links.**
  - Run: `/usr/bin/grep -rn "](docs/\|](\.\./" README.md docs/*.md`.
  - Every target must exist.
- [ ] **Step 3: Commit.**
  - Message: `docs: pipelined workers, reservations, uncapped worker downloads`

## Rollout (after the plan, by the controller, with the user)

These steps are not part of any task:

1. **Merge and push.**
2. **Server:** `git pull`, restart `vidshrink-serve`, then restart `vidshrink`
   between jobs. Watch for a `done:` line that names the server's own current
   file.
3. **VM:** `git pull`, add the three settings to `config.toml`, then restart the
   unit between jobs.
4. **PC:** the user restarts the tray when convenient. Old code keeps working
   until then.
