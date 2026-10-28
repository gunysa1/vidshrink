# vidshrink

vidshrink shrinks a large Google Drive video library by re-encoding it to HEVC.
Files that save the most space per hour of encoding go first. Each original is
replaced in place, and only after automated checks pass. The replaced original goes
to Drive's trash, not straight to deletion.

It is built for one library on one Drive account, accessed through
[rclone](https://rclone.org). It runs unattended for months alongside a Plex server
that reads the same Drive.

## How it works

```
                 ┌──────────────── home server ────────────────┐
 gdrive: ◀──────▶│ vidshrink run    x265 encodes, swaps, deletes│
  (rclone)       │ vidshrink serve  job service (HTTP + token)  │
                 │ state.db         SQLite, the source of truth │
                 └───────────▲──────────────────▲───────────────┘
                     LAN     │                  │ SSH tunnels
                 ┌───────────┴─────┐   ┌────────┴─────────────┐
                 │ Windows PC       │   │ cloud VMs            │
                 │ NVENC, tray app  │   │ Oracle A1, GCP spot  │
                 └──────────────────┘   │ x265, systemd unit   │
                                        └──────────────────────┘
```

1. **Scan.** `vidshrink scan` lists the library, probes each new or changed file
   with ffprobe and ranks it by expected GB saved per encode hour.
2. **Encode.** The server's worker (`vidshrink run`) takes the best job, downloads
   it, encodes it with x265 and verifies the result: duration, every stream, a full
   clean decode, and at least 20% smaller. Then it uploads a temporary file next to
   the original.
3. **Swap.** Only the server swaps: it checks that the original is unchanged,
   moves it to Drive's trash, and renames the temporary file into its place.
4. **Remote workers.** A Windows PC (NVENC) and cloud VMs (x265) can lease jobs from
   the server's job service. They download, encode, verify and upload the temporary
   file themselves, then report back. The server checks the upload on Drive and does
   the swap. Remote workers never delete or replace originals.
5. **Pipelined.** Every worker downloads the next file and uploads the last result
   while it encodes, so the encoder never waits. Remote workers keep finished
   downloads and encodes across a restart and stay reserved for them.

More detail: [docs/architecture.md](docs/architecture.md).

## Safety rules

- Only the server ever deletes or replaces an original, and only after its own
  checks on Drive. Deletes go to the Drive trash.
- A remote worker may delete only its own `.vidshrink-tmp.mkv`, and only one it can
  prove it uploaded (size and md5 match its report).
- Daily Drive budgets are kept per account (uploads 700 GB; main-account downloads
  500 GB, shared with Plex). The PC and cloud download accounts have no daily cap,
  because Plex never reads through them. A `downloadQuotaExceeded` error pauses that
  account's downloads for 12 h.
- Dolby Vision, interlaced, already-efficient (HEVC/AV1) and low-bitrate files are
  skipped.

## Requirements

- Python 3.11 or newer
- rclone with a configured Google Drive remote (default name `gdrive:`)
- ffmpeg and ffprobe on `PATH`; the server uses a BtbN n9.0.2 static build
- Linux with systemd user units for the server. The PC worker runs on Windows, the
  cloud worker on Linux (x86 or ARM).

## Quick start (server)

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q                          # tests never touch a real Drive

.venv/bin/vidshrink scan                     # list, probe and rank (read-only on Drive)
.venv/bin/vidshrink status                   # queue, projections, failures, budgets
.venv/bin/vidshrink run --dry-run --limit 3  # encode and upload tmp files only
.venv/bin/vidshrink run --limit 10           # replace 10 files
```

To run it as a service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/vidshrink.service deploy/vidshrink-serve.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vidshrink vidshrink-serve
sudo loginctl enable-linger "$USER"          # keep running after logout
journalctl --user -u vidshrink -f
```

Settings live in `src/vidshrink/config.py` (`Config`): remote name, excluded
folders, CRF and preset, size limits, daily budgets and lease times. Folders to skip
besides `Torrent/` go one per line in `~/vidshrink/excluded.txt`, which stays out of
the repo.

## Commands

| Command | Where | What it does |
|---|---|---|
| `vidshrink scan` | server | list, probe and rank the library |
| `vidshrink run [--limit N] [--dry-run]` | server | encode, verify and swap, best first |
| `vidshrink status` | server | stages, queue, workers, alerts, today's budgets |
| `vidshrink serve` | server | job service for remote workers |
| `vidshrink token` | server | print the job-service token |
| `vidshrink pc-worker [--config PATH]` | PC / cloud | headless remote worker |
| `vidshrink tray` | PC | tray app with Start / Stop now / Stop after this file |
| `vidshrink calibrate` | PC | pick the NVENC CQ that matches x265 quality (VMAF) |
| `vidshrink local FOLDER [--dry-run] [--limit N] [--cq N]` | any | compress a local folder (not the library) in place, with the same checks; originals move to `FOLDER/_vidshrink_originals/` |

## Documentation

- [docs/architecture.md](docs/architecture.md): components, job lifecycle, scoring,
  budgets and the job-service API
- [docs/operations.md](docs/operations.md): day-to-day running, restarts, requeues
  and troubleshooting
- [docs/pc-setup.md](docs/pc-setup.md): set up the Windows NVENC worker
- [docs/cloud-worker-setup.md](docs/cloud-worker-setup.md): set up a cloud x265
  worker (Oracle Cloud Always Free, or a Google Cloud spot VM)
- `docs/superpowers/specs/`: design documents, one per feature
- `docs/superpowers/plans/`: the implementation plans built from them

## Tests

```bash
.venv/bin/pytest -q
```

The suite uses local rclone remotes and short generated clips. It never touches a
real Drive. The full run takes about 14 minutes.
