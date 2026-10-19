# vidshrink cloud worker (x265 on Oracle Cloud) — Design

Date: 2026-10-19
Status: draft, pending the user's review
Builds on: `2026-09-27-vidshrink-design.md`, `2026-10-05-pc-worker-design.md`

## Intent

Add encoding capacity at no cost. A free cloud VM runs a headless x265 worker. It leases
jobs from the home server's job service and processes them end to end, like the PC does.
The first host is an **Oracle Cloud Always Free** ARM instance (the user already has an
Oracle account). A later step may reuse the same worker on a **Google Cloud $300 / 90-day
trial** VM; that is out of scope here, but nothing in this design should prevent it.

Why: the queue is years of encoding long, the server is a 4-core Haswell, and the PC only
works while the user has it on. A free machine that encodes around the clock adds steady
throughput.

## Goals

- A cloud worker that claims, downloads, encodes (x265), verifies, uploads and reports, all
  unattended. It starts on boot, survives reboots and restarts, and needs no clicks.
- Its own Drive download account and its own daily download budget, so it can't spend Plex's
  quota (main account) or the PC's.
- No change to how the server or the PC behave, except where this design says so.
- $0 spent.

## Non-goals

- The Google Cloud trial worker (a follow-up; same code, different host).
- GPU encoding in the cloud.
- More than one cloud worker at a time.
- Any change to the swap, the delete rules or the verification rules.

## Anti-goals (a failure even if it "works")

- The cloud worker can delete or overwrite an original, or anything other than its own
  provable `<stem>.vidshrink-tmp.mkv`.
- The cloud worker spends the main account's download quota, so Plex gets locked out again
  (see the 2026-09-29 incident).
- The job service is reachable from the public internet.
- The user gets an Oracle bill. Only Always Free shapes and storage may be used.
- Losing the cloud VM means revoking the server's or the PC's Drive access.

## Decisions made with the user

| Topic | Decision |
|---|---|
| Host | Oracle Cloud Always Free first; Google trial possibly later |
| Encoder | x265 on the CPU |
| Downloads | Through a **new Google account** with access to the library, with its own budget |
| Uploads | The cloud VM uploads itself, through the **main account** |
| Main-account credential | Each cloud VM gets its **own, separate authorization** of the main account, never a copy of the server's or the PC's token, so it can be revoked alone |
| Swap and deletes | Unchanged: only the server swaps and deletes originals |

## Host

- Shape: `VM.Standard.A1.Flex`, 4 OCPU, 24 GB RAM (the whole Always Free A1 allowance).
- OS: Ubuntu 24.04 LTS (arm64). *Builder's call; any current arm64 Debian or Ubuntu is fine.*
- Storage: one boot volume of ~200 GB (the whole Always Free block-storage allowance). The
  worker's workdir lives on it.
- Network: no inbound ports open in the security list except SSH. SSH accepts keys only.
  *My call:* once Tailscale works, SSH is restricted to the tailnet as well.
- The user creates the instance in the Oracle console. A1 capacity is often unavailable;
  retrying or trying another availability domain is expected. Upgrading the account to Pay
  As You Go makes capacity easier to get and keeps Always Free resources free, but it allows
  charges if a non-free resource is created. That is the user's decision, not part of this
  design.
- Oracle may reclaim Always Free instances that stay idle (low CPU, network and memory over
  7 days). A busy worker won't be idle; an empty queue could lead to reclamation. That's
  acceptable: re-creating the VM follows the setup steps again.

## Networking: reaching the job service

- The server and the VM join the user's **Tailscale** tailnet (free plan). The worker talks
  to `http://<server tailnet name or IP>:8765`. Tailscale's WireGuard encrypts the link; the
  job service stays plain HTTP with its bearer token, as on the LAN.
- No port forwarding on the home router, no public exposure of 8765.
- `serve` keeps binding to `0.0.0.0:8765`, which includes the Tailscale interface.
  *My call:* no change unless the builder finds a reason.
- The cloud VM uses the **same job-service token** as the PC (there is only one token
  today). *My call:* separate per-worker tokens are out of scope; rotating the token is done
  by hand if a worker is ever lost.

## Drive credentials on the VM

Two rclone remotes on the VM, configured by the user (the browser step of `rclone
authorize` happens on a machine with a browser):

1. **Download remote**, e.g. `gdrive-cloud:`: the new Google account. The user shares the
   library folders with it as **Viewer**. Read-only is enough, because this remote is used
   only for downloads.
2. **Upload remote**, `gdrive:`: a **new authorization** of the main account, made for this
   VM only.
   - It must use its **own OAuth client ID**, different from the one the server's and PC's
     rclone configs use. Google revokes access per app (client ID), so this is what makes
     "revoke only the cloud VM" possible. The user creates the client in their own Google
     Cloud project; the builder documents the steps.
   - Its root must point at the same Drive root as the server's `gdrive:`, so paths match.
   - **Its OAuth consent screen must be published ("In production"), not "Testing".** In
     Testing, Google expires refresh tokens after 7 days, and an unattended worker would lose
     upload access every week. A published but unverified app is fine for the user's own
     account (Google shows an "unverified app" warning once, at authorization). The server's
     `gdrive:` also uses a custom client ID, and its token has needed reconnecting before,
     which may be the same 7-day cause; checking that is a separate follow-up, not part of
     this work.
   - If either remote's token stops working (`invalid_grant` or similar), the worker must
     not churn: it stops claiming, logs a clear "re-authorize the `<remote>` remote" error,
     and the server's `vidshrink status` should make it visible (e.g. the worker's last
     report or heartbeat carries the error). *My call; exact mechanism is the builder's.*

The worker is configured with `remote = "gdrive:"` and `download_remote = "gdrive-cloud:"`,
the same split the PC uses (`pc_worker.PCConfig`). The download remote is set up the same
way as the PC's existing download remote, whose recipe lives in the PC's rclone config (copy
its non-secret options, not its token). A setup step must prove the paths line up: the same
known file listed through `gdrive:` and `gdrive-cloud:` with the same path, size and md5.

## The worker

### Reuse, don't fork

The cloud worker is the existing `pc_worker` job loop run headless on Linux, with the
encoder made configurable. Concretely:

- A new worker setting `encoder`: `"nvenc"` (the PC's default, unchanged) or `"x265"`.
  With `"x265"` the encode step uses the existing x265 command builder (`encode.build_command`,
  the same CRF, preset and parameters the server uses) instead of `build_nvenc_command`.
- The worker sends its `encoder` on claim, so the server scores candidates for it.
- A new download-account label (e.g. `"cloud"`) with its own budget; see "Server changes".
- Runs as a **systemd service** (system unit or user unit with lingering, builder's call)
  that runs `vidshrink pc-worker` with the cloud config, restarts on failure and starts on
  boot. *My call:* the command name stays `pc-worker`; renaming it is out of scope.
- No `bwlimit` on uploads (the home line isn't involved). *My call:* keep the setting, set
  it to unlimited in the cloud config.
- Priority: the VM is dedicated, so normal priority is fine.

### HDR

The PC excludes HDR sources because of NVENC. x265 on the cloud handles HDR10 exactly like
the server does, so **an x265 claimant may take HDR10 sources**. Dolby Vision stays skipped
everywhere, as today. *My call; it falls out of using the server's x265 path.*

### Disk

The worker must never take a job it can't fit. Today the server checks
`size × disk_factor + disk_reserve` against free space before downloading. *My call:* the
cloud worker sends its usable free bytes with each claim, and the server only offers rows
whose `size × disk_factor + disk_reserve` fits. The claim API may gain this optional field;
workers that don't send it (the PC) behave as today.

### Job length

The server already keeps jobs over `pc_handover_hours` away from itself. The cloud worker
gets the mirror rule. *My call:* a claim from the cloud worker only offers rows whose
expected encode time on the cloud (from its own learned speeds) is at most
`cloud_max_hours`, default 24, sent with the claim or set in server config (builder's
call). Before the cloud has learned speeds, its default speeds are the server's x265
defaults × 1.5 (a guess that an A1 core is slower than a Haswell core at x265), corrected
by learning after the first jobs. Same CRF, preset and parameters as the server; no
cloud-specific preset.

## Server changes

1. **A third download budget.** `JobStore._download_budget` learns a new account (e.g.
   `"cloud"`) backed by a `Budget` of kind `cloud_download`, with a new config limit
   `daily_cloud_download_limit`. *My call:* start at 750 GB, like the PC's. A
   `download_quota` report on this account blocks only this account, like the PC's. `vidshrink
   status` shows its daily total like the PC line.
2. **x265 claims.** `JobStore._candidates(encoder)` currently skips every HDR source (an
   NVENC rule). For an x265 claim it must not. Every other eligibility rule
   (`scorer.skip_reason`: excluded folders, size limits, efficient codecs, Dolby Vision,
   interlaced, unsupported codecs, low bitrate) is already applied before a row becomes
   `queued` and is encoder-independent, so nothing else needs splitting.
3. **Learned values.**
   - **Ratio:** the cloud worker uses the server's exact x265 settings, so its size ratios
     should feed and use the same `ratio:<family>` keys as the server.
   - **Speed:** an Ampere A1 core is not a Haswell core. Cloud speeds must be learned under
     their own keys, so they don't distort the server's estimates (which drive
     `pc_handover_hours`) and vice versa.
   - *Builder's call how* (e.g. a distinct `encoder` value such as `"x265-cloud"` for speed
     keys while ratios map to `x265`), as long as both properties hold and the `encoder`
     column stays meaningful for `_finish`/`_finish_remote`.
4. **"PC seen" must mean the PC.** `JobStore._seen()` writes `PC_SEEN_KEY` on every claim
   and heartbeat. The server's `_next_job` uses it to leave jobs over `pc_handover_hours` to
   the PC. A cloud worker must not refresh that key, or the server would leave long jobs for
   a PC that isn't there. *My call:* only claims and heartbeats from NVENC workers refresh
   it. `encoder` and `download_account` are handled independently everywhere (budget
   decisions follow the account, scoring and PC-seen follow the encoder), and log messages
   name the worker rather than saying "PC". Whether the server should also hand
   long jobs to the cloud worker is out of scope; it keeps its current rule.
5. **Remote reconcile is unchanged.** A cloud upload ends as `remote_ready` with
   `out_size`/`out_md5` and goes through the existing `_finish_remote` → `_swap`, exactly
   like a PC upload.

## Budgets and limits

- Downloads: charged at claim to the `cloud` budget.
- Uploads: charged at report to the existing global upload budget (700 GB/day on the main
  account), shared by everyone, as today. An `upload_limited` report blocks uploads for
  everyone, as today.
- Oracle egress: Always Free includes 10 TB/month; a few TB of uploads is well inside it.

## Failure handling

Everything the PC already handles applies unchanged: the lease expires after 30 min without
heartbeats; the first interruption is free; an md5/size mismatch on Drive costs an attempt
and never swaps; the server's heartbeat answer `cancel` stops the job; a download-quota
error blocks only the cloud account. New cases:

| Situation | Result |
|---|---|
| VM rebooted or reclaimed mid-job | Lease expires or the worker resumes its own lease on restart (existing `resume` claim) |
| Tailscale link down | Heartbeats fail; past the lease the worker abandons the job; it retries claims until the link returns |
| Disk too small for a job | Never claimed, or released with no attempt (see "Disk") |
| Reclaimed or rebooted with work in flight | Nothing special: lease rules apply (first interruption free, second costs an attempt); no keep-alive |
| A Drive token expires | Worker stops claiming and reports "re-authorize"; no attempts are spent |
| Cloud VM lost or compromised | The user revokes the cloud OAuth client in the main Google account and removes the VM from the tailnet; rotating the job-service token is a manual step |

## Testing (automated; no Drive, no Oracle)

- `jobs`: claim with `encoder="x265"` returns HDR10 rows, `"nvenc"` still never does;
  `download_account="cloud"` charges and blocks only the cloud budget; cloud claims don't
  refresh `PC_SEEN_KEY`; learned speed for the cloud never changes the server's speed keys,
  while ratios are shared.
- `pc_worker` with `encoder="x265"`: the full loop against a local rclone remote and the real
  job service, using the x265 command builder with a fast preset override in tests; the disk
  guarantee.
- Existing PC tests still pass unchanged (the PC's defaults don't change).
- Tests never touch the real `gdrive:`.

## Rollout (each step approved by the user)

1. Server code: the changes above, merged and deployed (no behaviour change until a cloud
   worker connects).
2. User: create the A1 instance (Ubuntu arm64, 4 OCPU, 24 GB, ~200 GB boot volume) and give
   SSH access. Adding an SSH key is an access change, done only on the user's direct
   instruction.
3. Tailscale on the server and the VM; a connection test from the VM to the job service.
4. User: create the new Google account and share the library folders with it as Viewer;
   create the separate OAuth client; authorize both rclone remotes on the VM.
5. VM setup: arm64 ffmpeg (BtbN n9.0.2 `linuxarm64-gpl`, matching the server's version),
   rclone, Python and the vidshrink package, the cloud worker config and the systemd unit.
6. One real cloud job, watched end to end; the user checks the result in Plex.
7. Normal unattended running.

## Left to the builder

- The exact names (`cloud`, `x265-cloud`, config keys, unit name).
- Systemd unit type (system or user with lingering).
- How speed keys are separated and how `PC_SEEN_KEY` is restricted, within the stated
  requirements.
- The disk-guarantee mechanism.
- The setup documentation for Oracle, Tailscale, the OAuth client and rclone.
