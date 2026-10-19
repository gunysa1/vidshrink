# Pipelined workers and uncapped worker downloads

Status: draft, 2026-10-19

## Why

The PC is the fastest worker, and it spends most of its running time not encoding.
On the six recent days it ran all day, it reached its 750 GB download cap at about
17:00–18:00 Pacific and then sat idle until the cap reset. Of those ~17 working
hours, only ~6 were spent encoding. The rest went to downloading, verifying and
uploading one file after another. Uploads alone are capped at 6 MB/s to keep the
home uplink usable for Plex.

The user runs the PC about half the time. The goal is that, whenever a worker is
on, its encoder never waits for a download, an upload or the server. Estimated
effect: the PC's output goes up about 2–3×, bringing the whole library from
about 6½ months to about 3–4 months.

## Goals

1. **No daily download caps on the PC and cloud accounts.** These accounts belong
   to vidshrink alone (Plex never reads through them), so the only limit is
   Google's. Google doesn't publish a daily download limit.
2. **Encode continuously.** On every worker (PC, cloud VM, and the home server's
   own x265 worker), downloading, encoding, verifying and uploading run as
   separate stages, so one file downloads while another encodes and a third
   uploads. The encoder starts the next file the moment it finishes one.
3. **Survive the PC being switched off.** Files already downloaded when a remote
   worker stops are kept. When it starts again, it encodes them before
   downloading anything new. (The user's decision.)

## Non-goals

- Two encodes at once on one machine. One encode already uses the GPU or all CPU
  cores.
- Changing the main account's 500 GB daily download cap. Plex shares that account.
- Changing the 700 GB daily upload budget. It sits under Google's real
  750 GB/day upload limit.
- Changing the PC's 6 MB/s upload cap. At ~17% output size, that is enough for
  about 3 TB of source a day.
- Changing which files are worth encoding, or how they are scored.

## Anti-goals

These count as failure even if throughput goes up:

- **Breaking the safety rules.**
  - Only the server deletes or replaces originals, and only after its own checks.
  - A remote worker deletes only its own provable `.vidshrink-tmp.mkv`.
- **Leasing a file to two workers at once**, or two rows racing for the same
  final path. The existing busy-stem guard and lease checks still apply to every
  claim.
- **A worker hoarding work.** It must not reserve so many files that other
  workers idle or the best files wait on a machine that is switched off.
- **Filling a disk.** That includes the VM's boot volume and the server's root
  filesystem.
- **Losing finished work on restart.** A finished encode, an encode waiting to
  upload, or a completed download must not be redone because of a restart, a
  Stop, or a server restart.
- **Locking Plex out.** Nothing here may increase downloads on the main account.

## Design

### 1. Download budgets

- **PC and cloud accounts** (`download_account` "pc" and "cloud"): no daily cap.
  - Usage is still recorded per Pacific day, so status shows how much was
    downloaded.
  - A `downloadQuotaExceeded` from Google still pauses that account for 12 hours,
    exactly as today.
  - Status shows each of these as "X GB today (no cap)".
- **Main account:** unchanged at 500 GB. It is still charged when a download
  starts, as today.
- **Uploads:** unchanged at 700 GB.

### 2. Remote worker pipeline (PC and cloud VM)

The remote worker becomes a set of stages, each with its own thread and a queue
of local jobs between it and the next:

```
claim+download ──▶ [downloaded] ──▶ encode ──▶ [encoded] ──▶ verify+upload+report
```

- **Concurrency:** one download at a time, one encode at a time, one upload at a
  time.
- **Verify:** runs in the upload stage, before uploading. On the PC, the CPU
  decode then overlaps with NVENC. On the VM it competes with x265 for CPU,
  which is accepted.
- **Prefetching:** the download stage claims and downloads the next job while
  another encodes. It stops claiming when either of two limits is reached:
  - **disk:** total size of files on disk (sources waiting plus outputs not yet
    uploaded) would exceed `queue_disk_gb`.
  - **hours:** expected encode hours of jobs held but not yet encoded would
    exceed `queue_hours`.
- **The two limits are new remote-worker settings.**

  | Machine | `queue_disk_gb` | `queue_hours` |
  |---|---|---|
  | PC | 300 (D: has ~590 GB free) | 24 |
  | Cloud VM | 120 (200 GB boot volume) | 24 |

  These are defaults the user can change.
- **One job may exceed the hours limit.** The first job claimed into an empty
  queue is always allowed, so a single long film can still be claimed.
- **Existing claim filters stay.** `free_bytes` and `max_hours` are still sent on
  each claim. `max_hours` still limits one job's length; `queue_hours` limits
  the total.
- **Tray status** shows all stages at once. Example: "Encoding X (42%) ·
  3 downloaded waiting · Uploading Y".
- **Stop now** cancels the running download, encode and upload. Completed
  downloads and completed encodes are kept on disk.
- **Stop after this file** stops claiming. It finishes the current encode and
  uploads every encoded file. Downloaded but unencoded files stay on disk for
  next time.

### 3. Holding several jobs, and reservations while switched off

Today a remote worker holds at most one lease (30 minutes, renewed by a
heartbeat every 60 s). With a queue it holds several.

- **Every held job is heartbeated.** A heartbeat may cover several job ids in one
  call.
- **Once a job's download completes,** the worker tells the server. From then on
  that job's lease is a **reservation**, renewed by every heartbeat.
  - Its length is set per worker by a new setting, `reserve_days`: PC 7, cloud
    VM 1. *My call:* the VM is always on, so its reservation only matters if it
    dies, and a day is enough then.
  - If the worker goes quiet, the file stays reserved for it until `reserve_days`
    runs out.
  - After that the server requeues it for any worker, as with an expired lease
    today.
- **Jobs claimed but not yet fully downloaded** keep the normal 30-minute lease.
  If the worker vanishes mid-download, the job returns to the queue quickly.
- **On start,** the worker lists the job ids in its workdir that have a
  completed-download marker (or a finished-encode marker). It asks the server to
  resume all of them.
  - **The server confirms a job when** the row is still leased to this worker,
    or has gone back to `queued` and no one has claimed it since. *My call:*
    finished work isn't thrown away just because a reservation ran out.
  - **"Original unchanged"** means the row's size and modtime still match the
    values sent with the original claim. The worker also checks its local file's
    size against them. No Drive call is made on the main account.
  - **Confirmed jobs** come back first and go straight into the encode (or
    upload) queue.
  - **Jobs the server refuses** have their local files deleted. A job is refused
    only when another worker has since taken it, or it is done or failed.
- **Partial downloads** are deleted on start, and their leases are released.
- **The server leaves long jobs to the PC** while the PC is seen
  (`pc_handover_hours`, `pc_absent_days`). That rule is unchanged. Files reserved
  by a switched-off PC simply aren't available to others until it comes back or
  the 7 days pass.

### 4. Home server pipeline

The server's own x265 worker gets the same separation, inside its existing
single process:

- **Downloads ahead:** it downloads the next file while the current one encodes,
  holding at most **one** downloaded file waiting. It's already encoding nearly
  all the time, so one is enough.
- **Uploads and swaps** happen alongside encoding, not between encodes.
- **Remote results** (`remote_ready`) are still verified and swapped during
  encodes, as today.
- **New stage `downloaded`:** *my call.* It is owned by the server and sits
  between `downloading` and `encoding`, meaning "downloaded, waiting to encode".
  It lets `recover()` tell a reusable download from an interrupted one. A `ready`
  encode is already kept and uploaded after a restart today.
- **Disk:** the existing `_wait_for_disk` rule still governs each download,
  counting all files on disk. The server has ~240 GB free.
- **Budgets:** it stays on the main account's 500 GB budget. At ~100 GB/day it is
  far from that cap.
- **Restart:** a restart keeps a completed download and a finished encode.
  - Today the interrupted encode is thrown away and the row requeued.
  - After this change, a completed download waiting to be encoded is reused
    after a restart if the original on Drive hasn't changed.
  - An encode interrupted mid-way still restarts from the beginning of that file.

## Constraints

- **Same rules as the rest of vidshrink:**
  - Python 3.11+.
  - The remote worker stays importable on Windows (no `fcntl`, no
    `vidshrink.worker`).
  - Subprocesses use `procflags.spawn_kwargs` (below-normal priority on Windows).
- **Tests never touch a real Drive.** No two pytest sessions run at once.
- **The job service stays backward compatible for one release.** A remote worker
  still on the old code (one job at a time) must keep working against the new
  server, so the PC can be updated after the server.
- **Interrupting a running server encode costs its progress.** Deploy the server
  part between jobs.
- **All API changes are additive.** An old one-job worker gets exactly today's
  behaviour: 30-minute leases and no reservations.

## Left to the builder

- **Thread and queue structure** inside the remote worker and the server worker.
- **API shape:** the multi-job heartbeat and resume calls, and the
  "download complete" notice (a new report outcome or a heartbeat field).
- **Marker files:** the exact on-disk names, extending the existing `.downloaded`
  and `.encoded` markers.
- **Whether Stop now also aborts an in-flight upload,** or lets an upload that is
  nearly done finish.
- **Thread safety in the server process:** one SQLite connection per thread, or
  all database writes on the main thread.
- **How "expected encode hours" is computed** for `queue_hours`: the server can
  return it with each claim.
