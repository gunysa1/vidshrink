# Cloud Worker (x265 on Oracle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a headless Linux VM run the existing `pc_worker` job loop with x265, its own download account and its own learned speeds, without changing how the server or the PC behave.

**Architecture:** The server's job service learns a third encoder (`x265-cloud`) and a third download account (`cloud`). Claims get two optional filters (free disk, max expected hours) and a new `/alert` endpoint. `PCConfig` gains settings that turn the same worker into the cloud worker. Deployment adds a systemd user unit and a setup guide. The VM setup itself is done by the controller with the user after the code is merged (the "Ops" section, not subagent tasks).

**Tech Stack:** Python 3.12, SQLite, stdlib `http.server`, rclone, ffmpeg (BtbN n9.0.2), systemd user units, pytest.

**Spec:** `docs/superpowers/specs/2026-10-19-cloud-worker-design.md`

## Global Constraints

- The PC's behaviour must not change: a claim, heartbeat, report or release without the new fields behaves exactly as today, and every existing test passes unchanged (except where a task says to update one).
- Encoder names: `"x265"` (server), `"nvenc"` (PC), `"x265-cloud"` (cloud). Download accounts: `"main"`, `"pc"`, `"cloud"`.
- The cloud worker uses the server's x265 command (`encode.build_command` with a default `Config()`): same CRF 22 / 4K CRF 24, preset `medium`, same params.
- Ratios learned by `x265-cloud` share the `x265` keys (`ratio:<family>`); speeds use their own keys (`speed:<res>:x265-cloud`), default `DEFAULT_SPEED[res] * 1.5`.
- `daily_cloud_download_limit = 750_000_000_000`, budget kind `"cloud_download"`.
- Only NVENC activity refreshes `PC_SEEN_KEY`.
- Tests never touch the real `gdrive:`. Run tests with `PYTHONPATH=src .venv/bin/python -m pytest -q` (in a worktree). Never run two pytest sessions at once.
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01BRHGWzPMU4kbc7jwAhq2zv`

## Review Focus

1. A claim whose `free_bytes` fits no candidate returns `{"job": None}` with a retry delay, never an error, and charges no download budget. Test in Task 3.
2. An old client (the PC today) that sends no `free_bytes`/`max_hours` gets exactly today's answer. Test in Task 3.
3. A cloud heartbeat or report during a long job does not refresh `PC_SEEN_KEY`, so the server keeps long jobs. Test in Task 2.
4. A Drive login failure on the cloud stops the worker, releases the job with no attempt used, and leaves an alert that `vidshrink status` shows, and a later successful claim clears it. Tests in Tasks 3, 4, 5.
5. An HDR10 source is offered to `x265-cloud` and never to `nvenc`. Test in Task 2.

---

### Task 1: Scorer and server know the `x265-cloud` encoder

**Files:**
- Modify: `src/vidshrink/scorer.py` (`ENCODERS`, `_key`, `ratio_for`, `speed_for`)
- Modify: `src/vidshrink/worker.py:500` (the "kept" message branches on `== "nvenc"`)
- Test: `tests/test_scorer_encoders.py`, `tests/test_worker_remote.py`

**Interfaces:**
- Produces: `scorer.CLOUD = "x265-cloud"`, `scorer.ENCODERS = ("x265", "nvenc", "x265-cloud")`, `scorer.REMOTE_ENCODERS = ("nvenc", "x265-cloud")`, `scorer.CLOUD_SPEED_FACTOR = 1.5`. Existing `ratio_for/speed_for/learn_result/expected_hours/encoder_tables(conn, encoder)` accept `"x265-cloud"`.

- [ ] **Step 1: Write failing tests** in `tests/test_scorer_encoders.py`:
  - `test_cloud_ratio_shares_x265_keys`: `learn_result(conn, probe_h264_1080p, 10e9, 4e9, 3600, "x265-cloud")` then `ratio_for(conn, "h264", "x265") == pytest.approx(0.6)` and `ratio_for(conn, "h264", "x265-cloud") == pytest.approx(0.6)`.
  - `test_cloud_speed_has_own_keys`: after learning an `x265-cloud` result with `encode_secs = 2 * duration`, `speed_for(conn, "1080p", "x265-cloud") == pytest.approx(2.0)` and `speed_for(conn, "1080p", "x265") == scorer.DEFAULT_SPEED["1080p"]`; and the reverse (learning `x265` leaves the cloud speed at its default).
  - `test_cloud_default_speed_is_slower`: on an empty DB, `speed_for(conn, r, "x265-cloud") == pytest.approx(scorer.DEFAULT_SPEED[r] * 1.5)` for every `r`.
- [ ] **Step 2: Run** `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_scorer_encoders.py` — expect the three new tests to FAIL.
- [ ] **Step 3: Implement.** In `_key`, ratio keys for `x265-cloud` map to the plain x265 key; speed keys get the `:x265-cloud` suffix (split `_key` into a ratio and a speed variant, or pass the kind; implementer's choice). `speed_for` defaults: `DEFAULT_SPEED_NVENC` for nvenc, `DEFAULT_SPEED[res] * CLOUD_SPEED_FACTOR` for cloud, else `DEFAULT_SPEED`. `ratio_for` default for cloud = the x265 default. In `worker.py`, the swap-needs-attention message uses `row["encoder"] in scorer.REMOTE_ENCODERS` (the encode lives as the Drive tmp for any remote worker).
- [ ] **Step 4: Add** to `tests/test_worker_remote.py` a test that a `remote_ready` row with `encoder="x265-cloud"` is finished like an nvenc row and its learned speed lands under `speed:1080p:x265-cloud` (reuse that file's existing remote-finish fixture/pattern).
- [ ] **Step 5: Run the full suite**; expect all pass.
- [ ] **Step 6: Commit** `feat(scorer): x265-cloud encoder shares x265 ratios, learns its own speeds`.

### Task 2: Job service: cloud budget, HDR for x265 claims, PC-seen only for NVENC

**Files:**
- Modify: `src/vidshrink/config.py` (add `daily_cloud_download_limit`)
- Modify: `src/vidshrink/jobs.py` (`__init__`, `_download_budget`, `_seen`, `_candidates`, `claim`, `heartbeat`, `report`, `release`)
- Test: `tests/test_jobs_store.py`

**Interfaces:**
- Consumes: `scorer.CLOUD`, `scorer.REMOTE_ENCODERS` (Task 1).
- Produces: `JobStore.cloud_downloads: Budget` (kind `"cloud_download"`); `_download_budget("cloud")`; `JobStore._seen(worker: str, encoder: str | None) -> None`, which always writes kv `f"seen:{worker}"` = now and writes `PC_SEEN_KEY` only when `encoder == "nvenc"`. Constant `jobs.WORKER_SEEN_PREFIX = "seen:"`.

- [ ] **Step 1: Write failing tests** in `tests/test_jobs_store.py`:
  - `test_cloud_account_has_its_own_budget`: `store.claim("cloud-vm", "x265-cloud", "cloud")` charges `store.cloud_downloads.used() == size` and leaves `store.downloads.used() == 0` and `store.pc_downloads.used() == 0`.
  - `test_cloud_download_quota_blocks_only_cloud`: report `download_quota` with `download_account="cloud"`, then `store.cloud_downloads.blocked()` is True and `store.downloads.blocked()` and `store.pc_downloads.blocked()` are False.
  - `test_hdr_offered_to_x265_cloud_not_nvenc`: one queued HDR10 row only (probe with `color_transfer="smpte2084"`, matching how `Probe.is_hdr` decides); `store.claim("pc")["job"] is None`; `store.claim("cloud-vm", "x265-cloud", "cloud")["job"]["id"] == hdr_id`.
  - `test_cloud_activity_does_not_refresh_pc_seen`: claim, heartbeat and report `failed` as `cloud-vm` with `x265-cloud`; `db.kv_get(conn, PC_SEEN_KEY) is None`; `db.kv_get(conn, "seen:cloud-vm") == str(store.clock.t)`.
  - `test_pc_heartbeat_still_refreshes_pc_seen`: an nvenc claim, then advance the clock and heartbeat; `PC_SEEN_KEY` equals the new time.
- [ ] **Step 2: Run** `tests/test_jobs_store.py`; expect the new tests to FAIL.
- [ ] **Step 3: Implement.** `_candidates` skips HDR only when `encoder == "nvenc"`. `heartbeat`/`report`/`release` call `_seen(worker, row_encoder)`, where `row_encoder` is the job row's `encoder` column (read the row; the `uploaded` idempotent path may call it with the existing row). The `download_quota` log line names the worker and account instead of saying "PC" (e.g. `f"{worker}'s Drive account ({download_account}) hit its download quota; its downloads paused"`; keep the "paused for everyone" wording for `main`). The `upload_limited` log names the worker.
- [ ] **Step 4: Run the full suite**; update only assertions that hard-code the old "PC" log wording, if any.
- [ ] **Step 5: Commit** `feat(jobs): cloud download budget, HDR for x265 claims, PC-seen only for NVENC`.

### Task 3: Claim filters, alerts, HTTP and client

**Files:**
- Modify: `src/vidshrink/jobs.py` (`claim`, `_claim`, new `alert`, `make_server` `/claim` and `/alert`, `JobClient.claim`, new `JobClient.alert`)
- Test: `tests/test_jobs_store.py`, `tests/test_jobs_http.py`

**Interfaces:**
- Consumes: Task 2's `_seen`.
- Produces:
  - `JobStore.claim(worker, encoder="nvenc", download_account="main", resume=False, free_bytes: int | None = None, max_hours: float | None = None) -> dict`
  - `JobStore.alert(worker: str, message: str) -> None` writes kv `f"alert:{worker}"` = `json.dumps({"t": now, "message": message[:500]})` and logs it. A claim that returns a job deletes `alert:{worker}`.
  - `jobs.ALERT_PREFIX = "alert:"`
  - HTTP: `/claim` accepts optional `free_bytes` (int) and `max_hours` (float); `POST /alert {worker, message}` → `{"ok": true}`.
  - `JobClient.claim(..., free_bytes=None, max_hours=None)` sends the fields only when not None; `JobClient.alert(worker, message) -> None`.

- [ ] **Step 1: Write failing tests** in `tests/test_jobs_store.py`:
  - `test_claim_skips_jobs_that_do_not_fit_free_bytes`: rows of 10 GB and 50 GB; with `cfg.disk_factor = 2.2`, `cfg.disk_reserve = 20e9`, `free_bytes = 60e9` → the 10 GB row is leased (10e9 * 2.2 + 20e9 = 42e9 ≤ 60e9; 50 GB needs 130e9), even if the 50 GB row scores higher.
  - `test_claim_no_fit_returns_no_job_and_charges_nothing`: only a 50 GB row, `free_bytes = 60e9` → `job is None`, `retry_after == jobs.NO_WORK_RETRY_SECS`, `store.cloud_downloads.used() == 0`, the row is still `queued`.
  - `test_claim_max_hours_filters_by_encoder_speed`: a 2 h 1080p row and a 20 h 1080p row (by duration); `max_hours=12` with `x265-cloud` (default speed 1.7 * 1.5 = 2.55 s/s) → 2 h × 2.55 = 5.1 h fits, 20 h × 2.55 = 51 h doesn't; only the short one is offered.
  - `test_claim_without_filters_is_unchanged`: same rows, no `free_bytes`/`max_hours` → the best-scoring row, as today.
  - `test_alert_is_stored_and_cleared_by_next_claim`: `store.alert("cloud-vm", "re-authorize gdrive:")` → kv `alert:cloud-vm` has that message; a successful claim by `cloud-vm` deletes it; a claim that returns no job leaves it.
- [ ] **Step 2: Write failing tests** in `tests/test_jobs_http.py` (reuse the real-HTTP fixture there): `/claim` with `free_bytes`/`max_hours` reaches the store (job filtered as above); `/alert` stores the alert; `/alert` without the token → 401.
- [ ] **Step 3: Run** both files; expect the new tests to FAIL.
- [ ] **Step 4: Implement.** In `_claim`, filtering happens per candidate inside the existing loop, before the download-budget check, with `continue` (skip, try the next). Expected hours use `scorer.expected_hours(conn, probe, encoder)`. Add `db.kv_delete(conn, key)` if `db` has no delete helper.
- [ ] **Step 5: Run the full suite**; expect all pass.
- [ ] **Step 6: Commit** `feat(jobs): claim filters for free disk and max hours; worker alerts`.

### Task 4: `vidshrink status` shows the cloud worker

**Files:**
- Modify: `src/vidshrink/cli.py` (`status_report`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `jobs.WORKER_SEEN_PREFIX`, `jobs.ALERT_PREFIX` (Tasks 2–3), `cfg.daily_cloud_download_limit`.

- [ ] **Step 1: Write failing tests** in `tests/test_cli.py`:
  - `test_status_shows_cloud_downloads`: after charging 12e9 to a `Budget(conn, cfg.daily_cloud_download_limit, now, kind="cloud_download")`, the report contains `"Cloud account downloaded today: 12 / 750 GB"`.
  - `test_status_shows_worker_last_seen`: kv `seen:cloud-vm` = now − 7200 → report contains `"cloud-vm last seen: 2.0 h ago"`.
  - `test_status_shows_alerts`: kv `alert:cloud-vm` set via `JobStore.alert` → report contains `"ALERT cloud-vm"` and the message.
  - No cloud lines at all on a DB with no cloud activity (existing status tests keep passing).
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** Lines go after the existing "PC last seen" / download lines. The cloud download line shows only when the cloud budget has usage today or is blocked (append `" (PAUSED: Drive download quota hit)"` when blocked, like the PC line). One "last seen" line per `seen:` key. Read kv keys by prefix with one query (`SELECT key, value FROM kv WHERE key LIKE ?`).
- [ ] **Step 4: Run the full suite**; expect all pass.
- [ ] **Step 5: Commit** `feat(status): cloud downloads, per-worker last seen, worker alerts`.

### Task 5: Worker settings that make `pc-worker` the cloud worker

**Files:**
- Modify: `src/vidshrink/pc_worker.py` (`PCConfig`, `_bwlimit`, `pc_rclone`, `pc_download_rclone`, `PCWorker.__init__`, `run`, `_do_job` auth-failure branch, default encode)
- Test: `tests/test_pc_worker.py`

**Interfaces:**
- Consumes: `JobClient.claim(..., free_bytes, max_hours)`, `JobClient.alert` (Task 3).
- Produces: `PCConfig` fields `encoder: str = "nvenc"`, `download_account: str = ""` (empty = today's rule: `"pc"` if `download_remote` else `"main"`), `max_hours: float = 0.0` (0 = don't send), `disk_check: bool = False` (True = send `free_bytes = shutil.disk_usage(workdir).free`). `bwlimit = ""` means no `--bwlimit` argument at all.

- [ ] **Step 1: Write failing tests** in `tests/test_pc_worker.py` (use `fake_pc`/`FakeClient`; extend `FakeClient.claim` to record kwargs and add `alert`):
  - `test_cloud_config_claims_as_x265_cloud`: `fake_pc(..., encoder="x265-cloud", download_account="cloud", max_hours=24.0, disk_check=True)` → the recorded claim has `encoder="x265-cloud"`, `download_account="cloud"`, `max_hours=24.0`, and `free_bytes` an int > 0.
  - `test_pc_defaults_send_no_new_fields`: default config → claim kwargs have `free_bytes is None` and `max_hours is None`, encoder `"nvenc"`.
  - `test_x265_encoder_uses_server_command`: with `encoder="x265-cloud"` and no `encode` passed, the worker's encode builds `encode.build_command(src, out, probe, Config())` (monkeypatch `run_encode_cancellable` to capture the argv; assert `"libx265"` in it and `build_nvenc_command` is not called).
  - `test_auth_failure_sends_alert_and_stops`: rclone download raises `RcloneError` with `auth_failed=True` → the job is released (no `failed` report), `client.alerts == [("<worker>", <message containing "re-authorize">)]`, and `run()` returns.
  - `test_empty_bwlimit_adds_no_flag`: `pc_rclone(PCConfig(server="x", token="y", bwlimit=""))` has no `"--bwlimit"` in its args; the default `"6M"` still gives `"6M:off"`.
  - `test_load_pc_config` (existing) still passes; add the new keys to a config file and check they load.
- [ ] **Step 2: Run**; expect FAIL.
- [ ] **Step 3: Implement.** `run` passes `self.cfg.encoder`. The default encode is `_nvenc` for `"nvenc"`, else an `_x265` method running `build_command(src, out, probe, Config())` through `run_encode_cancellable`. The auth-failure branch calls `self.client.alert(worker, f"Drive login failed; re-authorize the rclone remote, then restart the worker: {e}")` (best-effort: swallow `JobServiceError`) before raising `_LocalFault`. Update the module docstring: it now also runs headless on Linux.
- [ ] **Step 4: Run the full suite**; expect all pass.
- [ ] **Step 5: Commit** `feat(pc-worker): encoder, account, max-hours and disk-check settings for the cloud worker`.

### Task 6: Deployment files and setup guide

**Files:**
- Create: `deploy/vidshrink-cloud-worker.service`
- Create: `docs/cloud-worker-setup.md`

- [ ] **Step 1: Write the unit**, modelled on `deploy/vidshrink.service`: `Description=vidshrink cloud worker (x265)`, `After/Wants=network-online.target tailscaled.service`, `Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin`, `ExecStart=%h/vidshrink/.venv/bin/vidshrink pc-worker --config %h/.config/vidshrink/config.toml`, `Restart=on-failure`, `RestartSec=60`, `WantedBy=default.target`. A clean exit (local fault, e.g. a dead Drive login) must not restart it, which `on-failure` gives.
- [ ] **Step 2: Write `docs/cloud-worker-setup.md`** covering, in order: Oracle instance facts (A1.Flex 4/24, Ubuntu 24.04 aarch64, 200 GB, on-demand, only SSH inbound); `loginctl enable-linger ubuntu`; Tailscale on server and VM and the `/health` test (`curl -H "Authorization: Bearer $TOKEN" http://<server-tailnet>:8765/health`); BtbN `ffmpeg-n9.0.2-…-linuxarm64-gpl` into `~/.local/opt` with symlinks in `~/.local/bin`; rclone arm64; Python venv and `pip install -e`; the new Google account shared as **Viewer**; a **separate OAuth client** in the user's Google Cloud project with the consent screen **published (In production)**; `rclone authorize "drive" "<client_id>" "<client_secret>"` on a machine with a browser and pasting the token on the VM (never copying a token file); the path-alignment check (same file through `gdrive:` and `gdrive-cloud:`: same path, size, md5 via `rclone lsjson --hash`); the config file:
  ```toml
  server = "http://<server-tailnet-name>:8765"
  token = "<job-service token>"
  remote = "gdrive:"
  download_remote = "gdrive-cloud:"
  download_account = "cloud"
  encoder = "x265-cloud"
  max_hours = 24.0
  disk_check = true
  bwlimit = ""
  worker_name = "oracle-a1"
  workdir = "/home/ubuntu/vidshrink-work"
  ```
  and how to revoke (remove the OAuth client's access in the main Google account, remove the VM from the tailnet, rotate `serve.token` by hand).
- [ ] **Step 3: Commit** `docs(deploy): cloud worker unit and setup guide`.

---

## Ops (controller with the user, after the branch is merged; each step approved)

These change real systems, so they are not subagent tasks.

1. Deploy on the server: restart `vidshrink-serve` and (after its current encode, like before) `vidshrink`.
2. Tailscale on the server and the VM (the user logs both in); `/health` from the VM.
3. The user: the new Google account, Viewer share, the OAuth client in production, and the browser half of both `rclone authorize` runs.
4. VM install per `docs/cloud-worker-setup.md`; the path-alignment check.
5. Start the unit; watch one job through to `done`; the user checks it in Plex.
6. Then restrict SSH to the tailnet (security-list change, the user's call).
