# vidshrink

Re-encodes the videos on `gdrive:` that save the most space per hour of encoding to HEVC,
and replaces each original in place after automated checks. The original goes to Drive's trash.

Design: `docs/superpowers/specs/2026-09-27-vidshrink-design.md`

## Setup

vidshrink needs `ffmpeg`/`ffprobe` on `PATH`. Either install the distro package:

```bash
sudo apt-get install -y ffmpeg
```

or drop a static build in `~/.local/bin` (useful when apt isn't available yet). The
systemd unit below adds `~/.local/bin` to `PATH` so either source works.

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q
```

## Use

Before the first live run, optionally list Drive's duplicate names (Drive allows several
files with the same name in one folder; vidshrink skips those paths). This changes nothing:

```bash
rclone dedupe --dedupe-mode list gdrive:
```

```bash
.venv/bin/vidshrink scan                    # list + probe + rank (read-only on Drive)
.venv/bin/vidshrink status                  # queue, projections, failures, today's upload
.venv/bin/vidshrink run --dry-run --limit 3 # upload *.vidshrink-tmp.mkv next to originals
.venv/bin/vidshrink run --limit 10          # replace 10 files
```


## Drive download quota (shared with Plex)

Google limits how much one account can download per day, and the Plex `rclone mount`
uses the same account. vidshrink therefore caps its own downloads at
`daily_download_limit` (500 GB per Pacific day by default):

- `scan` reads only what ffprobe asks for (1 MB chunks, no read-ahead), measures the
  bytes actually served, and stops when the cap is reached, leaving the rest for the
  next scan.
- `run` charges each whole-file download to the same cap and waits for the next day
  when it is used up.
- If Drive answers `downloadQuotaExceeded`, both stop downloading for 12 h.

`vidshrink status` shows "Downloaded today". In September 2026 an uncapped scan used up
the quota and locked Plex out for about a day.

## Service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/vidshrink.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vidshrink
sudo loginctl enable-linger "$USER"         # keep running after logout
journalctl --user -u vidshrink -f
```
