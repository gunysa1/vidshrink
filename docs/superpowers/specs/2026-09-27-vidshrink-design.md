# vidshrink — Design

Date: 2026-09-27
Status: approved in brainstorming, pending spec review

## Goal

Reclaim Google Drive space by re-encoding the videos that save the most space per
hour of encoding to HEVC, replacing each original in place once automated checks pass.

## Context

- Drive `gdrive:` (rclone remote): a large library,
  mostly `.mkv` under `TV/` and `Movies/`. Plex serves the library.
- Server: 4-core Haswell, 15 GB RAM, Intel iGPU (no HEVC encoder), 285 GB free disk.
  Also runs Plex and tgfs. rclone 1.60.1 installed; ffmpeg is not.
- Converting everything is out of reach on this CPU (years). The tool converts the
  biggest wins first and stops being worthwhile when the remaining candidates score low.

## Decisions

| Topic | Decision |
|---|---|
| Goal | Biggest wins first (ranked by GB saved per encode-hour) |
| Output codec | HEVC via libx265, 10-bit, `.mkv` |
| Originals | Replaced after automated checks; original goes to Drive trash (30-day recovery) |
| CPU use | 24/7, full priority (user accepts Plex/tgfs contention) |
| Architecture | Custom Python pipeline + SQLite + systemd; local download → encode → upload |

## Components

Single Python project at `~/vidshrink`.

| Unit | Responsibility | Depends on |
|---|---|---|
| `db` (`state.db`, SQLite) | One row per video: path, size, modtime, codec, bitrate, resolution, duration, HDR/DV flags, audio/subtitle track list, score, stage, attempts, last error. Single source of truth. | — |
| `scanner` | `rclone lsjson -R` to list videos; `ffprobe` each file via a local read-only `rclone serve http` (ranged reads, headers only); parallel; re-probes only files whose size or modtime changed. | rclone, ffprobe |
| `scorer` | Applies skip rules, computes score, learns ratios from finished encodes. | db |
| `worker` | Takes the highest-scored queued file and moves it through the stages, saving the stage after every step. | rclone, ffmpeg, db |
| `budget` | Tracks bytes uploaded per Pacific-time day. | db |
| `cli` | `vidshrink scan`, `run [--dry-run] [--limit N]`, `status`. | all |

## Stages (per file)

```
queued → downloading → encoding → verifying → uploading → swapping → done
failure → failed (reason recorded; retried per the rules below)
skip rules → skipped (reason recorded)
check 4 fails → not_worth_it
```

1. **downloading:** `rclone copyto gdrive:<path> work/<id>.src`
2. **encoding:** ffmpeg to `work/<id>.out.mkv`
3. **verifying:** the checks below
4. **uploading:** `rclone copyto` to `gdrive:<dir>/<stem>.vidshrink-tmp.mkv`; confirm size and checksum
5. **swapping:** `rclone deletefile` the original (Drive trash is on, so it goes to trash), then
   `rclone moveto` the temp file to `<dir>/<stem>.mkv` (server-side rename)
6. **done:** record actual bytes saved and encode speed; delete the work files

**Resuming after a crash:** on startup, each file stuck in an in-progress stage is resumed
or cleaned up. For `swapping`, check the actual state first: if both the original and the
temp file exist, redo the swap from the start; if only the temp file exists, finish the
rename; if only the final file exists, mark it done.

## Scoring

**Skipped** (with a recorded reason) when any of these is true:

- The video codec is already HEVC or AV1.
- The file has a Dolby Vision stream.
- The size is under 200 MB or over 120 GB.
- The video bitrate is under a floor for its resolution: SD < 0.8 Mbps,
  720p < 1.2 Mbps, 1080p < 2.5 Mbps, 4K < 8 Mbps.
- The path is under an excluded prefix (default: `Torrent/`, `Private/`).

**Score** = `size × savings_ratio(codec) / (duration × speed_factor(resolution))`,
in GB saved per encode-hour.

- Starting ratios: H.264 0.55; MPEG-2, VC-1, MPEG-4 ASP (Xvid/DivX), WMV 0.70.
- Starting speed factors (encode seconds per second of video): SD 0.5, 720p 1.0, 1080p 1.7, 4K 6.5.
- After each finished encode, the ratio for its codec and the speed factor for its resolution
  are updated as a running average of real results.

## Encoding

```
ffmpeg -i src -map 0 -c copy -c:v libx265 -preset medium -crf 22 -pix_fmt yuv420p10le
       -x265-params <HDR10 metadata when present> -map_chapters 0 out.mkv
```

- 4K uses CRF 24.
- Audio, subtitles, chapters and attachments (embedded fonts) are copied unchanged.
- For HDR10, the colour primaries, transfer, matrix, mastering display and content light
  level are carried into x265 via `-x265-params`.
- Any extra video streams (such as cover art) are copied, not encoded.

## Checks before the swap

All must pass, otherwise the file is marked failed or `not_worth_it` and the original is untouched.

1. The output duration is within ±1 s of the source.
2. The audio and subtitle streams match the source in count, order, codec and language tag.
3. A full decode (`ffmpeg -v error -i out -f null -`) produces no error output.
4. The output is at least 20% smaller than the source. Otherwise the file is marked `not_worth_it`.
5. After upload, the remote size and checksum (`rclone md5sum`) match the local output.

## Guardrails

- **Upload budget:** at most 700 GB per Pacific-time day (assumed reset time; Google does not document it — if uploads hit `userRateLimitExceeded` early, the worker treats it as "budget exhausted" and backs off 6 h). When it's used up, the worker
  keeps encoding until 2 finished outputs are waiting, then sleeps until the reset.
- **Retries:** download and upload retry with exponential backoff, up to 5 attempts.
  An encode or check that fails twice is marked `failed` permanently.
- **Disk:** before downloading, the worker requires free space of at least 2.2× the
  source size plus a 20 GB reserve; otherwise it waits.
- **Dry run:** `run --dry-run` stops after uploading the temp file and never deletes the original.
- **One worker at a time:** a lock file prevents two workers from running at once.

## Running it

- `vidshrink.service` (systemd user unit, `Restart=on-failure`) runs `vidshrink run`.
- `vidshrink scan` is run by hand, or from an optional weekly timer.
- `vidshrink status` shows space saved so far, the queue, the top candidates, failures,
  and today's upload budget.

## Testing

pytest, with no Drive access needed:

- **scorer:** skip rules and ordering, using made-up probe data.
- **crash recovery:** simulate a crash at each stage, including both partial-swap states.
- **checks:** small real clips generated with ffmpeg (extra or missing tracks, truncated
  files), each of which must fail the right check.
- **budget:** the running daily total and the reset at midnight Pacific.
- **full run:** the whole worker against an rclone `local:` remote in a temp folder.

## Rollout (each step approved by the user)

1. `apt install ffmpeg`
2. **Scan only**, then a report: candidate count, projected TB saved, projected encode time.
   This decides whether the project is worth running.
3. **Dry run on the top 3 files**; the user compares quality.
4. **Live run capped at 10 files**; checked in Plex.
5. **Live without a cap** under systemd.

## Out of scope

GPU encoding, multiple machines, a web interface, upgrading rclone.
