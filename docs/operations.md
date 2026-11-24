# Operations

## Daily check

```bash
.venv/bin/vidshrink status
journalctl --user -u vidshrink -n 50 --no-pager -o cat
```

`status` shows:
- counts per stage, the queue and projections
- jobs leased to remote workers, and results waiting for the server
- when each worker was last seen, and any worker alerts (`ALERT <worker>: …`)
- today's upload and download totals per account; the PC and cloud accounts show
  "X GB (no cap)"
- a worker's tray line (or log) shows every stage at once, for example
  "Encoding X (3 waiting) · Uploading Y"

## Restarting the server worker

Only one `vidshrink run` can run per database. A second one stops at once, whatever
`--workdir` it is given. The lock is `state.lock` next to `state.db`.

An encode that's running when `vidshrink.service` stops is thrown away; the
first interruption costs no attempt. It goes back to `downloaded` and starts again
from the beginning. A finished download or encode is kept. Restart between jobs
where you can.

```bash
journalctl --user -u vidshrink -f      # wait for a "done:" or "not worth it:" line
systemctl --user restart vidshrink
```

`vidshrink-serve` can be restarted at any time. Workers retry until it's back,
and leases are kept in the database.

## Updating the code

```bash
git pull
systemctl --user restart vidshrink-serve
# then restart vidshrink between jobs, as above
```

Deploy in this order:

1. The server first. Restart `vidshrink-serve` any time, and `vidshrink` between jobs.
2. Then the cloud VMs and the PC. Old remote workers keep working against the new
   server, one job at a time, until they are updated.

Remote workers update separately:

- **PC:** `git pull` in its checkout, then restart the tray or `pc-worker`. Restart
  between jobs, or use Stop after this file first.
- **Cloud VMs:** `git -C ~/vidshrink pull`. Before restarting, make sure the VM's
  `config.toml` sets `queue_disk_gb` (120 on Oracle, 100 on GCP), `queue_hours = 24`
  and `reserve_days = 1` (the defaults are the PC's 300 GB and 7 days, too much for
  the VMs' disks). Then restart the unit between jobs (see
  [cloud-worker-setup.md](cloud-worker-setup.md)).

## Stopping a remote worker

- **Stop now** cancels the running download, encode and upload. Finished downloads
  and encodes stay on disk and stay reserved. Unfinished downloads are released.
- **Stop after this file** stops claiming, finishes the current encode and uploads
  every encoded file. Downloaded files stay on disk and stay reserved.
- **On the next start** the worker offers its finished downloads and encodes to the
  server and encodes them before downloading anything new. It deletes local files only
  for jobs the server refuses (taken by another worker, done or failed). If the
  server is unreachable it retries.
- **Reservations end** after `reserve_days` (PC 7, cloud 1). Then the server requeues
  the file for any worker.

## Requeueing failed files

Look at why they failed first:

```bash
sqlite3 -readonly ~/vidshrink/state.db \
  "select id, reason, path from videos where stage='failed' order by updated_at desc limit 20"
```

To retry one, put it back as `queued` with attempts reset. Stop `vidshrink` first,
or do it between jobs:

```bash
sqlite3 ~/vidshrink/state.db \
  "update videos set stage='queued', attempts=0, reason=NULL where id=<ID> and stage='failed'"
```

If the source changed or its probe looks wrong, set `stage='unprobed'` instead, and
the next `vidshrink scan` probes it again.

Common reasons:

- `rclone exit 3` (directory not found): the file was moved or renamed on Drive,
  for example by Sonarr or Radarr. `vidshrink scan` adds it under its new path, but
  scan re-probes a `failed` row only when its size or modtime changed on Drive, so
  retire the old row by hand once `rclone lsf` confirms the old path is gone:
  `update videos set stage='gone' where id=<ID> and stage='failed'`.
- `duration X -> Y` with Y well short of X: usually a bad source whose video ends
  before its stated length. Replace the file on Drive.
- `decode errors` naming the source's own faults: a damaged source. Replace it, or
  leave it failed.
- `size on Drive differs from the download again`: Drive's size changed between the
  scan and the download twice in a row with the same listing, so the row is parked
  instead of downloading forever. Check the file on Drive. A real change on Drive
  (new size or modtime) resets attempts by itself.
- `upload shows no saving: ...`: a worker reported an upload that isn't at least 20%
  smaller. The server parked the row and removed the tmp file on Drive.
- `upload: ...`: the last upload attempt failed. The row is parked and the tmp file
  on Drive is removed.
- `status` names the worker in a remote failure: `<worker>: <reason>`, not always
  `PC`.
- `[null @ ...] non monotonically increasing dts` from a build before 2026-10-30:
  the decode check itself rounded timestamps when the video starts between frame
  ticks, not a fault in the output. Update the worker and requeue.

## Worker alerts

| Alert | Fix |
|---|---|
| `Drive login failed for <remote>` | On that machine, run `rclone config reconnect <remote>`, then restart the worker. |
| `Local fault, worker stopped: …` | Fix the cause (disk space, permissions, a missing ffmpeg), then restart the worker. |
| `Library folder TV/ is not visible through <remote>` | That machine's download login can no longer see the library, usually because the folder was un-shared from that account. Share Movies and TV with it again (deep folders can take a few minutes to show up), then restart the worker. The job it was on is released without using an attempt. A single missing file in a visible library still fails as before. |

An alert clears on that worker's next successful claim.

Drive has two different upload errors, and they are handled differently:

| Drive error | Meaning | What happens |
|---|---|---|
| `uploadLimitExceeded` ("Received upload limit error") | the daily upload cap | the worker reports `upload_paused` and keeps its file; uploads are blocked for everyone for 6 h |
| `userRateLimitExceeded`, `rateLimitExceeded` ("User rate limit exceeded") | a short-term throttle | retried up to five times with backoff and never blocks uploads; if it keeps failing on the server, the attempt counts like any other failure. A remote worker uses no attempt: on a download it releases the job, on an upload it keeps the file and its lease and tries again after its idle wait. The md5 check after an upload is retried too. |

A remote worker asks the server for a permit before every upload. When the day's
700 GB is used up, its upload waits for a permit (`vidshrink status` shows the
budget). The worker keeps the file and its lease and asks again after the wait the
server names. Nothing is lost.

What the rclone error behind a failed Drive login means:

- `invalid_grant`: the refresh token expired or was revoked. A client whose consent
  screen is still in "Testing" does this every 7 days; publish it, then reconnect.
- `invalid_client`: the remote's `client_id`/`client_secret` don't match the client
  that issued its token, or the secret was reset. It shows about an hour after the
  change. Reconnect the remote with the client now in its config.

## Cloud VMs

- **Tunnel fails with "remote port forwarding failed for listen port 8765".** A dead
  SSH session still holds the port on the VM. Find it with `ss -ltnp | grep 8765` on
  the VM, `kill` that `sshd` pid, then `systemctl --user restart vidshrink-tunnel`
  (or `vidshrink-tunnel-gcp`) on the server.
- **GCP VM unreachable ("Connection timed out").** Google stopped the spot VM. The
  Cloud Scheduler job `vm-autostart` starts it within 10 minutes, and the tunnel
  reconnects by itself. If the External IP shown in the console isn't the reserved
  static one, re-attach it (see [cloud-worker-setup.md](cloud-worker-setup.md)).
- **Stopping the GCP VM on purpose:** pause `vm-autostart` first, or it starts the VM
  again.

## Swap needs attention

A row that shows "swap: needs attention" has a finished upload, but its original is
gone from Drive and this server didn't record trashing it. The server records
`original_trashed` just before it trashes the original, and moves the tmp file into
place when the original is missing only if that flag is set. Otherwise the tmp stays
on Drive, because moving it in could bring back a file you deleted on purpose.
Check the folder on Drive. If you deleted the original, delete the tmp too. If the
encode is what you want, rename the tmp into place by hand. The final name is the
original's name with a `.mkv` extension (`final_path`). Once a file there has the
encode's md5, the server finishes the row on its own.

After upgrading, a row that an older version left in `swapping` with the original
already trashed shows this once. Look at each one a single time.

When a swap fails with the original still present, or the final upload attempt
fails and the row is parked as `failed`, the server removes the tmp file on Drive.
The exception is a swap where this server had already trashed the original
(`original_trashed`), or a row an older version left in `swapping` (which may have
trashed it without recording so): the tmp is kept, because the encode may be the
only copy.

A dry run (`run --dry-run`) leaves other workers' leases and tmp files alone. It
still cleans up its own interrupted uploads.

## ffmpeg

- The server uses BtbN n9.0.2 from `~/.local/opt/ffmpeg-9.0.2-linux64-gpl`,
  linked from `~/.local/bin/ffmpeg` and `~/.local/bin/ffprobe`.
- 7.0.2 is kept in `~/.local/opt/ffmpeg-7.0.2-amd64-static` for rollback; repoint
  the two symlinks to switch back.
- 7.0.2's ffprobe crashed on some MPEG-TS and MKV files, which is why the server
  moved to 9.0.2.

## Drive download quota

Plex reads the same main account, so its daily download quota is shared:

- The server caps its own downloads at 500 GB per Pacific day. A download counts
  when it starts, so a failed or interrupted one still counts. When its next job is
  too big for what is left of the day, the server waits for the reset. A remote
  claim skips a job too big for its account's budget and takes a smaller one.
- The PC and the cloud workers download through their own accounts. Those have no
  daily cap, and usage is still recorded.
- A `downloadQuotaExceeded` error pauses that account's downloads for 12 h.
- In September 2026 an uncapped scan used up the quota and locked Plex out for
  about a day.

## Duplicate names

Drive allows several files with the same name in one folder. vidshrink skips such
paths. To list them without changing anything:

```bash
rclone dedupe --dedupe-mode list gdrive:
```
