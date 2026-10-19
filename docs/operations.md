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
- today's upload and download totals per account

## Restarting the server worker

An encode that's running when `vidshrink.service` stops is thrown away; the
first interruption costs no attempt. Restart between jobs where you can.

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

Remote workers update separately:

- **PC:** `git pull` in its checkout, then restart the tray or `pc-worker`.
- **Cloud VM:** `git -C ~/vidshrink pull`, then restart the unit (see
  [cloud-worker-setup.md](cloud-worker-setup.md)).

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
  "update videos set stage='queued', attempts=0, reason=NULL where id=<ID>"
```

If the source changed or its probe looks wrong, set `stage='unprobed'` instead, and
the next `vidshrink scan` probes it again.

## Worker alerts

| Alert | Fix |
|---|---|
| `Drive login failed for <remote>` | On that machine, run `rclone config reconnect <remote>`, then restart the worker. |
| `Local fault, worker stopped: …` | Fix the cause (disk space, permissions, a missing ffmpeg), then restart the worker. |

An alert clears on that worker's next successful claim.

## ffmpeg

- The server uses BtbN n9.0.2 from `~/.local/opt/ffmpeg-9.0.2-linux64-gpl`,
  linked from `~/.local/bin/ffmpeg` and `~/.local/bin/ffprobe`.
- 7.0.2 is kept in `~/.local/opt/ffmpeg-7.0.2-amd64-static` for rollback; repoint
  the two symlinks to switch back.
- 7.0.2's ffprobe crashed on some MPEG-TS and MKV files, which is why the server
  moved to 9.0.2.

## Drive download quota

Plex reads the same main account, so its daily download quota is shared:

- The server caps its own downloads at 500 GB per Pacific day.
- The PC and the cloud worker download through their own accounts, with their own
  budgets.
- A `downloadQuotaExceeded` error pauses that account's downloads for 12 h.
- In September 2026 an uncapped scan used up the quota and locked Plex out for
  about a day.

## Duplicate names

Drive allows several files with the same name in one folder. vidshrink skips such
paths. To list them without changing anything:

```bash
rclone dedupe --dedupe-mode list gdrive:
```
