# Architecture

## Components

| Module | Role |
|---|---|
| `scanner.py` | Lists the remote (`rclone lsjson -R --fast-list`), probes new or changed files over a throttled `rclone serve http`, and stores results in `state.db`. |
| `probe.py` | Runs ffprobe and turns its JSON into a `Probe` (codec, size, duration, HDR/DV flags, streams). |
| `scorer.py` | Skip rules, and the score: expected GB saved per encode hour. Learned size ratios and encode speeds are kept per encoder. |
| `worker.py` | The server loop: prepare (download), encode, verify, upload a tmp file, swap. It downloads the next file and uploads results in the background while it encodes. It also finishes remote workers' results and recovers after restarts. |
| `transfer.py` | The server worker's background download and upload threads. |
| `encode.py` | ffmpeg command lines for x265 (server and cloud) and NVENC (PC), and the stream mapping (FLAC for PCM, SRT for mov_text/eia_608). |
| `verify.py` | Output checks: codec, duration (within 1 s, or 0.05% on long files), streams (audio before subtitles, as written; an undetermined language may gain a tag), full decode with no errors beyond the source's own copied-audio errors, minimum saving. |
| `jobs.py` | The job service (`JobStore`, HTTP server, `JobClient`): leases, heartbeats, reports, budgets, alerts. |
| `pc_worker.py` | The remote worker used by both the PC (NVENC) and the cloud VM (x265): download, encode, verify and upload stage threads and a batch heartbeat. |
| `local_queue.py` | The remote worker's local job queue and its marker files on disk. |
| `tray.py` | Windows tray app around `pc_worker`. |
| `budget.py` | Daily byte budgets per Pacific day, plus temporary blocks after quota errors. |
| `db.py` | SQLite schema, migrations and guarded stage transitions. |
| `rclone.py` | rclone wrapper: listing, transfers, md5, retries, and classifying errors (quota, rate limit, auth). |

## Stages

```
unprobed → probed → queued ─┬─ server: downloading → downloaded → encoding → verifying → ready → uploading → swapping → done
                            └─ remote: leased → remote_ready → (server) swapping → done
side exits: skipped, not_worth_it, failed, probe_failed, gone
```

- A row moves between stages only through guarded updates, which check the expected
  current stage, so the server loop and the job service can share the database.
- **`downloaded`** is owned by the server and means "downloaded, waiting to encode".
  It lets recovery tell a reusable download from an interrupted one.
- **Restarts.** On startup the server puts in-progress rows back in the queue. The
  first interruption is free. A second interruption in a row counts as a failure.
  - A complete download in `downloaded` is reused if the original on Drive is
    unchanged.
  - A finished encode (`ready`) is kept and uploaded.
  - An encode interrupted mid-way goes back to `downloaded` and counts as an
    interruption. It starts again from the beginning of the file.
- **Attempts.** A file that fails twice (`max_attempts`) is parked as `failed`.

## Pipelining

Every worker runs download, encode and upload as separate stages. One file downloads
while another encodes and a third uploads. There is one download, one encode and one
upload at a time.

- **Server.** Holds at most one downloaded file waiting. The disk check before a
  prefetch counts the output of encodes that haven't finished. Uploads and swaps run
  while it encodes. With `--limit N` it may download one extra file, which is kept for
  the next run.
- **Remote workers.** The download stage claims the next job while another encodes. It
  stops claiming at either limit:
  - `queue_disk_gb`: source files waiting plus outputs not yet uploaded.
  - `queue_hours`: expected encode hours of jobs held but not yet encoded.
  - `queue_upload_gb` (default 25): finished encodes waiting to upload. Past it the
    upload link is the bottleneck, so claiming more would only pile up encodes.
  - The first job into an empty queue is always admitted. `free_bytes` and `max_hours`
    still apply to each claim.
- **Verify** is its own stage between encode and upload, so the next file is checked
  while the previous one uploads and the (capped) upload link never waits on it.

## Reservations

A remote worker may hold several jobs.

- **Incomplete downloads** keep the normal 30-minute lease.
- **Once a download completes,** the worker reports `downloaded`. The lease then
  becomes a reservation of `reserve_days`, renewed by every heartbeat. The server caps
  it at 30 days.
- **If the worker goes quiet,** the file stays reserved for it until the reservation
  runs out. Then the server requeues it for any worker.
- **The server leaves other work to the PC** as before while the PC is seen. Files
  reserved by a switched-off PC are not available to others until it returns or the
  reservation ends.

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
| downloads, PC account | no cap | the PC claims a job (recorded only) |
| downloads, cloud account | no cap | the cloud worker claims a job (recorded only) |

- Status shows the uncapped accounts as "X GB (no cap)".
- A `downloadQuotaExceeded` from an account blocks that account's budget for 12 h.
- An upload rate-limit error blocks uploads for everyone for 6 h.

## Job service API

All endpoints need `Authorization: Bearer <token>`. The token is in
`~/vidshrink/serve.token` on the server; print it with `vidshrink token`. Bodies are
JSON.

| Endpoint | Body | Answer |
|---|---|---|
| `GET /health` | none | `{"ok": true}` |
| `POST /claim` | `worker`, `encoder` (`nvenc`/`x265-cloud`), `download_account` (`main`/`pc`/`cloud`), `resume`, optional `free_bytes` (int), `max_hours` (float) | `{"job": {...} or null, "retry_after": s, "download_used": n, "download_limit": n}`. The job carries `expected_hours`. |
| `POST /heartbeat` | `ids` (list; a single `id` still works), `worker`, `download_account` | `{"cancel": [ids], ...}`. Sent every 60 s; renews every id listed. |
| `POST /resume` | `worker`, `encoder`, `download_account`, `jobs`: a list of `{id, size, modtime, reserve_secs}` | `{"resumed": [ids], "refused": [ids]}` |
| `POST /report` | `id`, `worker`, `outcome` (`uploaded`, `downloaded`, `upload_paused`, `failed`, `not_worth_it`, `changed`, `download_quota`, `upload_limited`), plus `out_size`, `out_md5`, `encode_secs` for `uploaded` and `reserve_secs` for `downloaded` | `{"ok": true}`; 409 if the lease was lost |
| `POST /release` | `id`, `worker` | `{"ok": true}`; the job goes back to `queued` with no attempt used |
| `POST /alert` | `worker`, `message` | `{"ok": true}`; shown in `vidshrink status` until that worker's next successful claim |

- **`downloaded`.** The worker holds the whole original. The lease becomes a
  reservation of `reserve_secs` (at most 30 days).
- **`upload_paused`.** The worker hit Drive's upload limit and keeps its encoded file.
  The job keeps its lease and reservation, and uploads are blocked for everyone.
- **Lease expiry.** A lease with no heartbeat for 30 min expires. The server deletes
  any tmp file the worker left and puts the job back in the queue.
- **Resume.** After a worker restarts, it offers its finished downloads and encodes
  to `/resume`.
  - The server confirms a job if the row is still leased to that worker, or is back in
    `queued` and no one has claimed it, and the original is unchanged. That means the
    row's size and modtime match what the claim sent. No Drive call is made.
  - It refuses a job that another worker took, or that is done or failed.
  - The worker deletes local files only for refused ids.
- **Old clients.** A worker that sends a single `id`, resumes with `resume=true` on
  claim, or never reports `downloaded` gets today's behaviour: a 30-minute lease and no
  reservation.

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
| `queue_disk_gb` | 300 | 120 |
| `queue_hours` | 24 | 24 |
| `queue_upload_gb` | 25 | 25 |
| `reserve_days` | 7 | 1 |

- `queue_disk_gb`, `queue_hours` and `queue_upload_gb` limit how much work a worker prefetches. See
  Pipelining.
- `reserve_days` is how long a finished download stays reserved for the worker.
- **Marker files.** The workdir holds `<id>.job.json`, `<id>.downloaded` and
  `<id>.encoded` for each job. A restart uses them to resume. The source is deleted
  once `<id>.encoded` is written. `<id>.uploaded` (out size, md5, encode seconds)
  marks a finished upload whose report the server hasn't confirmed: the worker keeps
  the job and retries only the report, and uploads again only if the tmp on Drive is
  no longer its own.
- **Disk.** The queue limit and the free-space check both count the output still to
  be written for every downloaded or encoding job, as the server does.

- **Local faults.** If the worker hits a problem of its own (a dead Drive login, a
  full disk, a missing tool), it releases its job with no attempt used, sends an
  alert and stops.
- **On the cloud.** The systemd unit doesn't restart a clean stop like that. Fix the
  cause, then start the unit again.
