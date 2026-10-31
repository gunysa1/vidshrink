# Whole-project review findings, 2026-10-31

Review of the whole tree at `9adde3d` by a fresh code reviewer, requested with
`/superpowers review this project`. The reviewer read every module except `tray.py`
and `calibrate.py`, the deploy units, the README and the architecture and operations
docs, and ran nothing against Drive or the live database. This file is the spec for
the fixes in `docs/superpowers/plans/2026-10-31-review-fixes.md`; the user asked for
all of them ("Fix all").

**Verdict.** The safety design holds: no path trashes an original without the
size+modtime and md5 checks, and no path lets a remote worker delete anything but its
own tmp. One Critical issue (fixed the same day in `verify.py`: the output's video
errors could be excused by the source's audio errors, and counts were ignored).

## Important

1. **The 700 GB upload budget doesn't apply to remote uploads at upload time.**
   `jobs.py` checks `uploads.allows(1)` only at claim and charges only after the
   `uploaded` report; `pc_worker.py` uploads with no budget check. Reservations can
   last 7 days and cloud workers have no bandwidth limit, so remote workers can push
   past 700 GB toward Google's 750 GB hard limit. Fix: the remote worker asks the
   server before each upload; a "no" is treated like `upload_paused`.
2. **Rate-limit classification mixes the short-term per-user rate limit with the
   daily upload cap.** `rclone.py` lists `userRateLimitExceeded` / `User rate limit
   exceeded` as `rate_limited`; `with_retries` never retries them, so a server
   download or check burns an attempt, and an upload blocks everyone for 6 h. With
   `--drive-stop-on-upload-limit` the real daily cap shows as `uploadLimitExceeded` /
   "Received upload limit error". Fix: split into `upload_limit` (daily-cap markers,
   blocks) and `throttled` (back off and retry; no block, no attempt).
3. **The server's download budget undercounts.** `finish_download` charges
   `row["size"]` only after success; `with_retries` restarts a full copy up to 5
   times, so a flaky 50 GB file can pull ~250 GB from the Plex-shared account
   uncharged. Fix: charge before starting, as the job service does.
4. **A transient Drive error burns an attempt before any download starts.**
   `worker.py` calls `self.rclone.exists(final)` with no retry; an `RcloneError`
   there goes to `_fail`. Fix: retry it, and on `RcloneError` requeue without an
   attempt.

## Minor

5. **Remote tmp delete checks md5 only, with a timing window.** README promises
   "size and md5". `exists` → `md5` → `deletefile` by name. Fix: list with
   `entries()`, require exactly one entry, check Size and md5 together.
6. **The job service doesn't validate `encoder`** on `/claim` and `/resume`; an
   unknown value pollutes the learned tables and breaks the "SWAP NEEDS ATTENTION"
   path. `PCConfig.encoder` silently means x265 for anything but `"nvenc"`. Fix:
   require `encoder in scorer.REMOTE_ENCODERS` on both sides.
7. **A missing original lets the encode come back.** When `entries(path)` is empty
   at swap time the tmp is moved into place anyway; needed for crash recovery, but
   it also restores a file the user deleted during the encode. Fix: record that the
   server itself trashed the original before deleting it, and move without an
   original only then.
8. **Orphan tmp files pile up.** `_swap_failed` with the original present, and a
   final upload failure, leave the tmp on Drive; scan hides tmp files. Fix: delete
   them server-side.
9. **A dry run still deletes on Drive.** `expire_leases()` runs in a dry run and
   deletes other workers' tmp files. Fix: skip it when `dry_run`.
10. **The lock covers the workdir, not the database.** `vidshrink run --workdir X`
    next to the service skips the lock and `recover()` requeues the live service's
    rows. Fix: lock next to `db_path`.
11. **Trash pinning drops for other remote names.** `cli.py` sets `rclone_args=()`
    when the remote isn't named `gdrive:`, dropping `--drive-use-trash=true` for a
    Drive remote with another name. Fix: decide from the remote's type.
12. **The job service listens on every interface** over plain HTTP; it is also
    single-threaded and makes Drive calls inside requests. Fix: make the bind
    address configurable (default unchanged, documented), and move the expiry
    deletes out of the request path.
13. **A "changed" file can loop forever** (never costs an attempt). Fix: count an
    attempt when the listing hasn't changed since the last "changed".
14. Smaller: claim stops at the first candidate over budget; `_candidates` parses
    every probe JSON per claim; `_next_job` looks at only 500 rows; `report failed`
    labels every reason "PC:"; stale `jobs.py` docstring; `TMP_SUFFIX` defined
    twice; `local_queue.forget` lazily imports `pc_worker`; calibrate ignores
    `cfg.download_account`; a release charges the download budget for nothing.
15. **Docs vs code:** the `operations.md` requeue SQL lacks `and stage='failed'`;
    "scan never touches `failed` rows" is wrong (`upsert_listing` resets a failed row
    whose size or modtime changed); README's "size and md5" for remote deletes.

## Recommendations taken

- The server rejects an `uploaded` report whose `out_size > size × (1 − min_saving)`.

## Recommendations left to the operator

- Give the cloud and PC upload remotes rclone's `drive.file` scope so Google itself
  limits them to files they created. Needs re-authorizing each remote; documented,
  not done in code.
