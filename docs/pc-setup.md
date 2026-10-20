# Setting up the Windows PC worker

The PC worker claims encode jobs from the server over the LAN, encodes them with
NVENC, uploads the result, and reports back. This guide sets it up on a Windows PC.

## 0. Prerequisite: the server's job service

Before setting up the PC, make sure the job service is installed and running on
the server (the box running the main `vidshrink` pipeline, with its `state.db`).

**Order matters.** First restart the main pipeline onto the new code, and only
then enable the job service:

1. Update the code on the server, then restart the pipeline:

   ```bash
   systemctl --user restart vidshrink
   ```

   Do this **before** enabling `vidshrink-serve`. The old pipeline code doesn't
   know about PC leases, so it can pick up a row the PC has leased and overwrite
   that lease.

   Restart between jobs where you can. An encode that's running when the service
   stops is thrown away and done again from the start after the restart. To find
   a gap, watch `journalctl --user -u vidshrink -f` and restart right after a
   `done:` or `not worth it:` line. Or run `vidshrink status` and check that
   nothing is in `downloading` / `encoding` / `verifying`.

2. Then enable the job service:

   ```bash
   systemctl --user enable --now vidshrink-serve
   ```

The unit file is `deploy/vidshrink-serve.service`; copy it to
`~/.config/systemd/user/vidshrink-serve.service` on the server first. Check it
came up with `systemctl --user status vidshrink-serve` and `journalctl --user -u
vidshrink-serve -f`.

The service listens on port 8765. **Keep it LAN-only** — it binds `0.0.0.0` so
every device on the LAN can reach it, but do not port-forward 8765 (or anything
else needed to reach it) from your router. It is not designed to be exposed to
the internet; the bearer token is the only access control and there's no rate
limiting against brute force.

## 1. Install Python

Install Python 3.12 or newer from [python.org](https://www.python.org/downloads/).
During setup, tick **"Add python.exe to PATH"**.

## 2. Get the code and install it

Copy the repo to the PC — either `git clone` it from the server over SSH, or
copy a zip of it — then create a virtualenv and install it:

```powershell
py -m venv %LOCALAPPDATA%\vidshrink\venv
%LOCALAPPDATA%\vidshrink\venv\Scripts\pip install -e .[pc]
```

## 3. Install rclone

Install [rclone for Windows](https://rclone.org/downloads/). Copy the server's
rclone config (`~/.config/rclone/rclone.conf`) to the PC at
`%APPDATA%\rclone\rclone.conf` so the PC can reach the same `gdrive:` remote.

## 4. Install ffmpeg with NVENC and VMAF support

Download an ffmpeg build with `hevc_nvenc` and `libvmaf` compiled in — the
gyan.dev "full" build works. Unzip it somewhere (e.g.
`%LOCALAPPDATA%\vidshrink\ffmpeg\bin`) and confirm both are present:

```powershell
ffmpeg -encoders | findstr nvenc
ffmpeg -filters | findstr vmaf
```

Both commands should print at least one line. If `ffmpeg` isn't on PATH, you'll
set `ffmpeg_dir` in the config below instead of adding it to PATH yourself.

## 5. Get a token and write the config

On the server, print (or create) the job-service token:

```bash
vidshrink token
```

Create `%APPDATA%\vidshrink\config.toml` on the PC:

```toml
server = "http://<server-LAN-IP>:8765"
token = "<the token from vidshrink token>"
cq = 23
# Only needed if ffmpeg isn't on PATH:
ffmpeg_dir = 'C:\Users\you\AppData\Local\vidshrink\ffmpeg\bin'
# A fast local disk with 300 GB free for queued source and output files:
workdir = 'D:\vidshrink'
# Download-ahead limits (these are the defaults):
queue_disk_gb = 300    # source files waiting plus results not yet uploaded
queue_hours = 24       # expected encode hours of jobs held but not yet started
reserve_days = 7       # how long finished downloads stay reserved while the PC is off
# Optional: cap the PC's upload bandwidth so Plex stays usable, e.g. "6M"
# (uploads only). To cap downloads too, give "UP:DOWN", e.g. "6M:40M" (MiB/s).
bwlimit = "6M"
```

**Windows paths in TOML:** use single quotes (as above) or forward slashes
(`"D:/vidshrink"`) for any path containing backslashes. A double-quoted TOML
string treats `\` as an escape character, so `"D:\vidshrink"` is invalid TOML
(and `"D:\\vidshrink"` is easy to get wrong) — single-quoted strings are
literal and avoid the whole problem.

## 6. Connection test

Run the worker in the foreground:

```powershell
%LOCALAPPDATA%\vidshrink\venv\Scripts\vidshrink pc-worker
```

As soon as it prints something like `downloading ...`, press Ctrl-C. This does
a "Stop now": the in-progress job is abandoned and released back to the queue
immediately (not left to wait out the lease). On the server, `vidshrink status`
should show that file back in the queue within a few seconds.

## 7. Calibrate the NVENC quality setting

Run:

```powershell
%LOCALAPPDATA%\vidshrink\venv\Scripts\vidshrink calibrate
```

This claims a few jobs, downloads them, cuts a short sample from each, encodes
each sample at several NVENC CQ values (and once with the server's x265
settings as the reference), scores them with VMAF, and prints a table plus a
suggested `cq`. Set `cq` in `config.toml` to the suggested value.

`calibrate` claims real jobs from the queue — the same ones `pc-worker` would
have encoded — and downloads each one's **full source file**, not just the
60-second sample it cuts from it. Each of those downloads is charged in full
against the server's shared 500 GB/day Drive download budget, which Plex also
relies on, so running `calibrate` with a larger `--samples` (or on several
large files) eats into that budget like any other job. The jobs themselves are
returned to the queue right after their sample is cut — `calibrate` doesn't
remove them from the queue or touch anything on Drive — so running it doesn't
cost encode time on the server, only download quota.

Calibrate releases each job back to the queue as soon as it has cut that job's
sample (it never leases a job through the whole measurement step, which can
run long), and it never deletes anything from Drive — only its own local
sample files once it's done.

## Optional: a separate Google account for downloads

Drive's daily download quota is per Google user. By default the PC downloads with
the same account the server uploads with, so PC jobs eat into the 500 GB/day that
Plex also relies on. Giving the PC its own, second Google account for downloads
only keeps the two separate:

1. Create a free Google account. In the Drive web UI (on the main account), share
   the `Movies` and `TV` folders with it as **Viewer**.

2. On the PC, run `rclone config` to create a new remote, e.g. `gdrive-pc`, type
   `drive`, scope `drive.readonly`, and in the advanced options set
   `shared_with_me = true`, so that `Movies/...` paths resolve from the "Shared
   with me" root. Log in with the second account in the browser.

3. Test it:

   ```powershell
   rclone lsd gdrive-pc:
   ```

   This should list `Movies` and `TV`.

4. In `config.toml` add:

   ```toml
   download_remote = "gdrive-pc:"
   ```

   Leave `remote` as it is (the main account) — it's still needed for uploads,
   md5 checks, existence checks and deletes, so every file stays owned by the
   main user.

With `download_remote` set, the PC's downloads count against a separate budget
on the PC's own account, which has no daily cap (usage is still recorded), and
the main account's 500 GB/day is left free for the server and Plex. On
2026-10-07 the PC account downloaded 300 GB with no download-quota error on
the main account (probed every 15 min for a day), so Google charges these
downloads to the PC account, not the owner. A `downloadQuotaExceeded` from
Google still pauses the account for 12 h. `vidshrink status` on the server shows both:
`Downloaded today` is the main account, `PC account downloaded today` is the
PC's.

## 8. Run it in the background: a tray icon

Create a Start-menu shortcut (or put one in the Startup folder to run at
login) pointing at:

```
%LOCALAPPDATA%\vidshrink\venv\Scripts\pythonw.exe -m vidshrink.cli tray
```

`pythonw.exe` (not `python.exe`) runs without a console window.

## 9. Using the tray icon

The tray icon's menu:

- **Start** — begin claiming and working jobs.
- **Stop now** — cancel the running download, encode and upload and stop.
  Finished downloads and encodes stay on disk and stay reserved. Unfinished
  downloads are released back to the queue immediately.
- **Stop after this file** — stop claiming, finish the current encode, upload
  every encoded file, then stop. Downloaded files stay on disk and reserved.
- **Exit** — same as Stop now, then closes the tray icon.

The tray shows every stage at once, for example "Encoding X (3 waiting) ·
Uploading Y". The PC downloads ahead while it encodes, so the workdir holds
several files. On the next Start it offers its finished downloads and encodes to
the server and encodes them first. It deletes them only if the server says
another worker took the job, or it is done or failed.

**Logging off or shutting down the PC is handled on a best-effort basis**: the
tray tries to stop cleanly (like Stop now) on logoff/shutdown, but Windows
doesn't guarantee that handler runs to completion. If the PC goes away mid-job
without releasing it, that's fine — the job isn't lost. The server's lease
expires automatically after 30 minutes with no heartbeat, at which point it
returns the job to the queue (and cleans up any partial upload) so another
worker — or this one, next time it's on — can pick it up.
