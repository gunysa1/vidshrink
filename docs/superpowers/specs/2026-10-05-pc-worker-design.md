# vidshrink PC worker (NVENC) — Design

Date: 2026-10-05
Status: design approved in brainstorming, pending spec review
Builds on: `2026-09-27-vidshrink-design.md`

## Goal

Speed up the queue by letting the user's Windows PC (i7-8700K, RTX 2080) encode jobs with
NVENC whenever the user switches it on, alongside the always-on server. The server stays the
single source of truth and the only machine that ever deletes or replaces originals.

## Context

- The server (4-core Haswell) runs `vidshrink.service`: x265 `medium`, CRF 22. In the first
  live batch a 1080p remux took up to 10 h (a long film). The projected queue is about
  6 years of server encoding.
- The RTX 2080 (Turing NVENC) encodes HEVC 10-bit with B-frames at roughly 5–10× real time
  for 1080p.
- Network: the PC and the server are on the same home LAN. The internet line is 1 Gbps down
  and 100 Mbps up, and its upload is shared with Plex remote streaming.
- Drive's daily download quota is per account and shared with the Plex mount (see the
  2026-09-29 lockout). All download and upload limits must stay global across both machines.

## Decisions

| Topic | Decision |
|---|---|
| PC encoder | `hevc_nvenc` on the RTX 2080 (not x265 on the CPU) |
| PC OS | Windows |
| Data path | The PC downloads and uploads directly with Drive (its own rclone); the server only coordinates |
| Coordination | A job service on the server (HTTP, LAN only, token); the server's SQLite DB is the source of truth |
| Who deletes | Only the server: it verifies the PC's upload, then does the existing swap |
| PC control | A tray app with manual Start / Stop now / Stop after this file; nothing starts automatically |
| HDR | HDR10 sources stay server-only for now; Dolby Vision is still skipped everywhere |
| Upload limit | PC uploads `--bwlimit 6M` (≈50 Mbps), configurable |

## Components

| Unit | Where | Responsibility |
|---|---|---|
| `jobs` (job service) | server, `vidshrink serve`, run by its own systemd user unit `vidshrink-serve.service` | HTTP API on the LAN: claim, heartbeat, report, release. Token auth. All state changes go through `db` in transactions. |
| DB additions | server `db.py` | New stages `leased` and `remote_ready`; new columns `worker` (TEXT), `lease_until` (REAL), `encoder` (TEXT, `x265`/`nvenc`), `remote_md5` (TEXT). Schema migration via `ALTER TABLE … ADD COLUMN` on connect when the columns are missing. |
| Remote reconcile | server `worker.py` | Handles `remote_ready` rows before its own encoding (verify on Drive → existing `_swap`/`_finish`), and expires leases (`lease_until < now` → delete the tmp if present → `queued`, no attempt used). |
| Scoring per encoder | `scorer.py` | Learned values are keyed per encoder (`ratio:<family>:nvenc`, `speed:<res>:nvenc`; the existing keys stay x265). Starting NVENC values: ratio = x265 ratio × 0.85; speed SD 0.05, 720p 0.08, 1080p 0.15, 4K 0.5 (encode s per video s). |
| NVENC encode | `encode.py` | `build_nvenc_command(src, out, probe, cfg)` (see "NVENC encoding"). |
| `pc_worker` | PC, `vidshrink pc-worker` | The job loop (claim → download → encode → verify → upload → report), with heartbeats on a thread and a stop flag checked between steps and during the encode. |
| `tray` | PC, `vidshrink tray` | `pystray` + `Pillow` tray icon; runs `pc_worker` in a thread; menu Start / Stop now / Stop after this file / Status / Exit. |
| PC config | PC, `%APPDATA%\vidshrink\config.toml` | server URL, token, rclone remote name, workdir, ffmpeg/rclone paths, `bwlimit`, `cq`. |

## Job lifecycle

```
queued ──claim──▶ leased ──PC: download → NVENC → verify → upload tmp──▶ remote_ready ──server: verify + swap──▶ done
                     │
                     └─ no heartbeat past lease_until → server removes any tmp → queued (no attempt used)
```

1. **Claim** (`POST /claim {worker, encoder:"nvenc"}`), in one DB transaction:
   - pick the queued row with the best NVENC score, excluding HDR sources and rows whose
     `final != path` target already exists (the same check `prepare` does);
   - require `download_budget.allows(size)`, otherwise answer `204` with `retry_after`;
   - reserve `size` on the download budget, set `stage='leased'`, `worker`, `encoder='nvenc'`
     and `lease_until = now + 1800`;
   - return `{id, path, size, modtime, probe}`.
2. **Heartbeat** (`POST /heartbeat {id, worker}`): if the row is still `leased` by this
   worker, extend `lease_until`; otherwise answer `{"cancel": true}`. The PC sends one
   every 60 s.
3. **Report** (`POST /report {id, worker, outcome, ...}`):
   - `outcome="uploaded"` with `out_size`, `out_md5`, `encode_secs` → `stage='remote_ready'`;
     the server charges `out_size` to the upload budget;
   - `outcome="failed"` with `reason` → the existing `_fail` attempts logic (stage `queued`
     or `failed`);
   - `outcome="not_worth_it"` with `reason` → `not_worth_it`;
   - `outcome="download_quota"` → block downloads system-wide for 12 h; row back to `queued`
     with no attempt used.
4. **Release** (`POST /release {id, worker}`, used by "Stop now"/Exit): row back to `queued`
   with no attempt used. The PC deletes its own partial tmp on Drive before releasing, if it
   had started uploading.
5. **Server finishes** a `remote_ready` row in its loop, before preparing its own work:
   - the remote tmp must exist exactly once, with size == `out_size` and md5 == `out_md5`;
     otherwise delete the tmp and count a failed attempt;
   - then run the existing `_swap` (identity check of the original, duplicate names, target
     re-check) and `_finish` (the learned values go to the NVENC keys).
6. **Lease expiry**: the server loop and `serve` both sweep `leased` rows with
   `lease_until < now`: delete the tmp if present, then `queued`, no attempt used, logged.

The PC never deletes anything on Drive except its own `<stem>.vidshrink-tmp.mkv` for a job
it is abandoning.

## Splitting work between the machines

- The PC claims by NVENC score; the server keeps using its x265 score.
- The server skips queued rows whose expected x265 encode time is over `pc_handover_hours`
  (default 6) while a PC worker has been seen in the last `pc_absent_days` (default 3). After
  that the server takes them again.
- `leased` and `remote_ready` rows are not `queued`, so neither worker can pick them up.

## NVENC encoding

```
ffmpeg -hide_banner -nostdin -y -v error [-fflags +genpts for AVI] -i src
  -map 0 -map -0:d -c copy
  -c:v:{vi} hevc_nvenc -preset:v:{vi} p7 -tune:v:{vi} hq -profile:v:{vi} main10
  -pix_fmt:v:{vi} p010le -rc:v:{vi} vbr -cq:v:{vi} {CQ} -b:v:{vi} 0
  -spatial-aq:v:{vi} 1 -temporal-aq:v:{vi} 1 -rc-lookahead:v:{vi} 32
  -bf:v:{vi} 4 -b_ref_mode:v:{vi} middle
  [-c:s:N srt for mov_text] -max_muxing_queue_size 4096 out.mkv
```

- **CQ** comes from the PC config. Its value is chosen by a one-off calibration
  (`vidshrink calibrate`, run on the PC): 3 queued SDR files, each encoded at CQ 20/22/24/26
  and with x265 CRF 22, each scored with VMAF (`libvmaf`) against the source. The highest CQ
  whose mean VMAF is within 0.5 of x265's wins. The table is shown to the user, who confirms.
- The same `verify.compare` + `decode_errors` checks run on the PC before uploading
  (duration ±1 s, tracks, hevc, clean decode, ≥ 20% smaller).

## Budgets and bandwidth

- All accounting stays on the server: the download budget is charged at claim (the source
  size), the upload budget at report (`out_size`). The existing 500 GB/day download and
  700 GB/day upload limits are global.
- PC rclone uploads use `--bwlimit 6M` by default; downloads run unthrottled within the
  budget.

## Security

- `serve` binds to the server's LAN address (config `serve_addr`, default `0.0.0.0:8765`
  with LAN-only expectations documented; no port forwarding). Every request needs
  `Authorization: Bearer <token>`; the token is in `~/vidshrink/serve.token` (generated with
  `secrets.token_urlsafe(32)`, mode 600). Anything else gets a 401.
- No API call can cause the server to delete or overwrite an original without the server's
  own Drive-side verification.

## PC control (tray)

- Nothing starts automatically. A Start-menu shortcut runs `vidshrink tray`.
- Menu:
  - **Start**: begin the loop.
  - **Stop now**: set the stop flag, kill ffmpeg, delete local work files and any partial
    remote tmp, release the job; the GPU is free within seconds.
  - **Stop after this file**: finish the current job, then idle.
  - **Status**: current file, stage, progress, the shared download total today, jobs done
    this session.
  - **Exit**: Stop now, then quit.
- Icon colour: grey = off, green = working, amber = finishing the last file.
- Windows shutdown or logoff: handled as Stop now. Abrupt power loss: lease expiry.

## Failure handling

| Situation | Result |
|---|---|
| PC off or crashed mid-job | Lease expires after 30 min → tmp removed → `queued`, no attempt used |
| Reported md5/size ≠ Drive | No swap; tmp deleted; one failed attempt |
| Server restarts mid-job | Leases persist in the DB; heartbeats resume |
| `downloadQuotaExceeded` on the PC | Reported; downloads blocked system-wide for 12 h |
| The same file keeps failing on the PC | 2 attempts → `failed` (existing rule) |
| Job cancelled on the server | The heartbeat answers `cancel` → the PC stops and cleans up |

## Testing (automated; no Drive, no GPU)

- `jobs`: a real HTTP server on a temp DB; token enforced; concurrent claims never return the
  same row; budget reserve and limit; heartbeat extends the lease and answers cancel;
  report/release transitions; lease expiry deletes the tmp on a local rclone remote.
- Server reconcile: a size or md5 mismatch never swaps; a match swaps through the existing
  checks; NVENC learned keys are updated.
- `pc_worker`: the full loop against a local rclone remote and the real job service, with
  the encode step swapped for libx265 `-preset ultrafast` (no GPU in tests); Stop now
  releases; Stop after this file finishes first; download_quota reporting.
- `build_nvenc_command`: exact argv.
- `tray`: state machine and menu actions (no real tray).

## Rollout (each step approved by the user)

1. Server: install and start `vidshrink-serve.service` next to `vidshrink.service` (no behaviour change until a PC connects).
2. PC setup (guided): Python, the vidshrink package, rclone + a copy of the server's
   rclone.conf, an ffmpeg build with NVENC and libvmaf, the token, the tray shortcut;
   connection test (claim → release).
3. `vidshrink calibrate` on 3 files → the user picks CQ.
4. One real PC job → the user checks it in Plex.
5. Normal use from the tray.

## Out of scope

Idle-based auto mode, HDR on the PC, more than one PC, access from outside the LAN, GPU on
the server.
