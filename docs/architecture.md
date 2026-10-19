# Architecture

## Components

| Module | Role |
|---|---|
| `scanner.py` | Lists the remote (`rclone lsjson -R --fast-list`), probes new or changed files over a throttled `rclone serve http`, and stores results in `state.db`. |
| `probe.py` | Runs ffprobe and turns its JSON into a `Probe` (codec, size, duration, HDR/DV flags, streams). |
| `scorer.py` | Skip rules, and the score: expected GB saved per encode hour. Learned size ratios and encode speeds are kept per encoder. |
| `worker.py` | The server loop: prepare (download), encode, verify, upload a tmp file, swap. It also finishes remote workers' results and recovers after restarts. |
| `encode.py` | ffmpeg command lines for x265 (server and cloud) and NVENC (PC), and the stream mapping (FLAC for PCM, SRT for mov_text/eia_608). |
| `verify.py` | Output checks: codec, duration within 1 s, streams, clean full decode, minimum saving. |
| `jobs.py` | The job service (`JobStore`, HTTP server, `JobClient`): leases, heartbeats, reports, budgets, alerts. |
| `pc_worker.py` | The remote-worker loop used by both the PC (NVENC) and the cloud VM (x265). |
| `tray.py` | Windows tray app around `pc_worker`. |
| `budget.py` | Daily byte budgets per Pacific day, plus temporary blocks after quota errors. |
| `db.py` | SQLite schema, migrations and guarded stage transitions. |
| `rclone.py` | rclone wrapper: listing, transfers, md5, retries, and classifying errors (quota, rate limit, auth). |

## Stages

```
unprobed → probed → queued ─┬─ server: downloading → encoding → verifying → ready → uploading → swapping → done
                            └─ remote: leased → remote_ready → (server) swapping → done
side exits: skipped, not_worth_it, failed, probe_failed, gone
```

- A row moves between stages only through guarded updates, which check the expected
  current stage, so the server loop and the job service can share the database.
- **Restarts.** On startup the server puts in-progress rows back in the queue. The
  first interruption is free. A second interruption in a row counts as a failure.
- **Attempts.** A file that fails twice (`max_attempts`) is parked as `failed`.

## Scoring

```
score = (size_GB × expected_ratio) / (duration × expected_speed / 3600)
```

The ratio (fraction saved) and the speed (encode seconds per video second) start
from defaults and are refined from finished jobs:

| Encoder | Ratio keys | Speed keys | Default speed |
|---|---|---|---|
| `x265` (server) | `ratio:<family>` | `speed:<res>` | built-in table |
| `nvenc` (PC) | `ratio:<family>:nvenc` | `speed:<res>:nvenc` | NVENC table |
| `x265-cloud` (cloud) | shared with `x265` | `speed:<res>:x265-cloud` | x265 table × 1.5 |

The cloud worker uses the server's exact x265 settings, so it shares the server's
size ratios. Its CPU is different, so it learns its own speeds.

## Splitting work between workers

- **Server.** Takes the best `queued` row by x265 score. While the PC has been seen in
  the last 3 days, it leaves SDR jobs expected to take over 6 h of x265 time to the PC.
- **PC.** Claims by NVENC score. It never takes HDR, which stays with x265.
- **Cloud.** Claims by its own x265 score, HDR10 included. It only takes jobs that
  fit its free disk (`size × 2.2 + 20 GB`) and are expected to finish within
  `max_hours` (24 h by default).
- **One worker per file.** Rows sharing a final name (same basename, different
  extension) are never worked on at the same time.

## Budgets

| Budget | Limit per Pacific day | Charged when |
|---|---|---|
| uploads (main account) | 700 GB | a result is uploaded (server, PC or cloud) |
| downloads, main account | 500 GB (shared with Plex) | the server downloads, or a worker without its own account |
| downloads, PC account | 750 GB | the PC claims a job |
| downloads, cloud account | 750 GB | the cloud worker claims a job |

- A `downloadQuotaExceeded` from an account blocks that account's budget for 12 h.
- An upload rate-limit error blocks uploads for everyone for 6 h.

## Job service API

All endpoints need `Authorization: Bearer <token>`. The token is in
`~/vidshrink/serve.token` on the server; print it with `vidshrink token`. Bodies are
JSON.

| Endpoint | Body | Answer |
|---|---|---|
| `GET /health` | none | `{"ok": true}` |
| `POST /claim` | `worker`, `encoder` (`nvenc`/`x265-cloud`), `download_account` (`main`/`pc`/`cloud`), `resume`, optional `free_bytes` (int), `max_hours` (float) | `{"job": {...} or null, "retry_after": s, "download_used": n, "download_limit": n}` |
| `POST /heartbeat` | `id`, `worker`, `download_account` | `{"cancel": bool, ...}`. Sent every 60 s; the lease lasts 30 min. |
| `POST /report` | `id`, `worker`, `outcome` (`uploaded`, `failed`, `not_worth_it`, `changed`, `download_quota`, `upload_limited`), plus `out_size`, `out_md5`, `encode_secs` for `uploaded` | `{"ok": true}`; 409 if the lease was lost |
| `POST /release` | `id`, `worker` | `{"ok": true}`; the job goes back to `queued` with no attempt used |
| `POST /alert` | `worker`, `message` | `{"ok": true}`; shown in `vidshrink status` until that worker's next successful claim |

- **Lease expiry.** A lease with no heartbeat for 30 min expires. The server deletes
  any tmp file the worker left and puts the job back in the queue.
- **Resume.** After a worker restarts, its first claim asks to resume. If it still
  holds a live lease, it gets that job back, so finished downloads and encodes are
  reused.

## Remote-worker settings

`pc_worker.PCConfig` is read from a TOML file. On Windows it's at
`%APPDATA%\vidshrink\config.toml`, on Linux at `~/.config/vidshrink/config.toml`.

| Key | PC | Cloud |
|---|---|---|
| `server`, `token` | LAN URL | Tailscale URL |
| `remote` | `gdrive:` (main account, uploads) | `gdrive:` (its own authorization) |
| `download_remote` | second account | download-only account (Viewer) |
| `download_account` | (derived: `pc`) | `cloud` |
| `encoder` | `nvenc` (default) | `x265-cloud` |
| `cq` | from calibration | unused |
| `bwlimit` | `6M` (upload cap) | `""` (no limit) |
| `max_hours` | 0 (off) | 24 |
| `disk_check` | false | true |

- **Local faults.** If the worker hits a problem of its own (a dead Drive login, a
  full disk, a missing tool), it releases its job with no attempt used, sends an
  alert and stops.
- **On the cloud.** The systemd unit doesn't restart a clean stop like that. Fix the
  cause, then start the unit again.
