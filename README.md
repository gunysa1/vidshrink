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

```bash
.venv/bin/vidshrink scan                    # list + probe + rank (read-only on Drive)
.venv/bin/vidshrink status                  # queue, projections, failures, today's upload
.venv/bin/vidshrink run --dry-run --limit 3 # upload *.vidshrink-tmp.mkv next to originals
.venv/bin/vidshrink run --limit 10          # replace 10 files
```

## Service

```bash
mkdir -p ~/.config/systemd/user
cp deploy/vidshrink.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vidshrink
sudo loginctl enable-linger "$USER"         # keep running after logout
journalctl --user -u vidshrink -f
```
