# Setting up a cloud worker (x265)

A cloud worker is the PC worker run headless on a Linux VM. It claims jobs from the
server through an SSH tunnel, encodes them with software x265, uploads the result, and
reports back. It only claims work while its unit runs.

Two run in production:

| Worker | VM | Paid by |
|---|---|---|
| `oracle-a1` | Oracle `VM.Standard.A1.Flex`, 4 OCPU, 24 GB, aarch64 | Always Free |
| `gcp-t2d` | Google Cloud `t2d-standard-8` spot, us-central1-c, x86_64 | free-trial credit |

Rules for the whole guide:

- Never copy a token or `rclone.conf` between machines. Every remote is authorized fresh.
- Nothing secret goes in the repo. Tokens and client secrets stay on the machines.

## 1. The instance

### Oracle

- Shape `VM.Standard.A1.Flex`, 4 OCPU, 24 GB RAM.
- Ubuntu 24.04 LTS, aarch64, user `ubuntu`.
- 200 GB boot volume (the workdir lives on it).
- Security list: only SSH inbound. Port 8765 is never opened.

### Google Cloud (spot)

- Machine `t2d-standard-8` (8 vCPU, 32 GB), provisioning model **Spot**, zone
  `us-central1-c`. Ubuntu 24.04 LTS, x86_64, 145 GB balanced disk.
- Firewall: only SSH inbound.
- **Reserve a static external IP** and attach it (VPC network → IP addresses). Without
  one, every restart after a spot stop gives the VM a new IP and the tunnel breaks.
  Editing the machine type can detach it; check the External IP column afterwards and
  re-attach it under Edit → Network interfaces. Google also bills a reserved IP that
  isn't attached to a running VM.
- **Restart after a spot stop.** Google stops a spot VM when it needs the capacity and
  does not start it again. A Cloud Scheduler job does:
  1. IAM & Admin → Roles: create a role `VM starter` with `compute.instances.get`,
     `compute.instances.start` and `compute.zoneOperations.get`.
  2. IAM & Admin → Service accounts: create `vm-starter` with that role. It needs no
     key (new organisations block key creation anyway).
  3. Cloud Scheduler → Create job: name `vm-autostart`, region `us-central1`, frequency
     `*/10 * * * *`, target HTTP `POST`
     `https://compute.googleapis.com/compute/v1/projects/<project>/zones/us-central1-c/instances/<vm>/start`,
     empty body, auth header **OAuth token** as `vm-starter`, scope
     `https://www.googleapis.com/auth/cloud-platform`.

  Starting a running VM does nothing, so the job just runs every 10 minutes. To keep
  the VM stopped on purpose, pause the job first.

Either VM: let user services run without a login session:

```bash
sudo loginctl enable-linger "$USER"
```

## 2. Reaching the job service

Two options. Either way, nothing at home is opened to the internet.

### Option A: SSH tunnel from the server (no sudo, used in production)

The server dials out to the VM and forwards the VM's `127.0.0.1:8765` to its own job
service. The VM can reach only that one port. On the server, add an SSH alias for the VM
(key authentication) to `~/.ssh/config`:

```
Host vidshrink-cloud
    HostName <vm public IP>
    User ubuntu
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```

Then install and start the tunnel unit:

```bash
cp deploy/vidshrink-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now vidshrink-tunnel
```

A second VM gets its own alias and its own copy of the unit. The GCP one is
`vidshrink-gcp` / `vidshrink-tunnel-gcp`:

```bash
sed 's/vidshrink-cloud$/vidshrink-gcp/; s/the cloud worker/the GCP worker/' \
  deploy/vidshrink-tunnel.service > ~/.config/systemd/user/vidshrink-tunnel-gcp.service
systemctl --user daemon-reload && systemctl --user enable --now vidshrink-tunnel-gcp
```

On the VM, `curl http://127.0.0.1:8765/health` should now answer 401 (no token), and
200 with `-H "Authorization: Bearer $TOKEN"`. In the worker config, use
`server = "http://127.0.0.1:8765"`.

### Option B: Tailscale (server and VM)

Install Tailscale on the server and on the VM and log both into the same tailnet
(`sudo tailscale up`; open the printed link in a browser). Then, on the VM, check the job
service is reachable. Print the token on the server with `vidshrink token`.

```bash
TOKEN='<job-service token>'
curl -H "Authorization: Bearer $TOKEN" http://<server-tailnet-name>:8765/health
```

It should answer with the health JSON. `vidshrink-serve` already binds `0.0.0.0:8765`,
which includes the Tailscale interface, so nothing changes on the server. Once this works
you can restrict SSH in the firewall to the tailnet.

## 3. Tools on the VM

### ffmpeg

Use the same version as the server: BtbN `n9.0.2`, the GPL build for the VM's CPU
(`linuxarm64` on Oracle, `linux64` on GCP). Take the tarball from the BtbN
FFmpeg-Builds releases page, or for x86_64 copy the server's
`~/.local/opt/ffmpeg-9.0.2-linux64-gpl` over SSH.

```bash
mkdir -p ~/.local/opt ~/.local/bin
tar -xf ffmpeg-n9.0.2-*-linuxarm64-gpl*.tar.xz -C ~/.local/opt
ln -sf ~/.local/opt/ffmpeg-n9.0.2-*-linuxarm64-gpl*/bin/ffmpeg  ~/.local/bin/ffmpeg
ln -sf ~/.local/opt/ffmpeg-n9.0.2-*-linuxarm64-gpl*/bin/ffprobe ~/.local/bin/ffprobe
ffmpeg -version | head -1
```

### rclone

Install the build for the VM's CPU from rclone.org (`curl https://rclone.org/install.sh
| sudo bash` picks the right one), then check `rclone version`.

### vidshrink

The code is public on GitHub, so the VM clones it directly (the VM needs no access to the server):

```bash
sudo apt install -y python3-venv git xz-utils
git clone https://github.com/gunysa1/vidshrink.git ~/vidshrink
```

To update the VM later: `git -C ~/vidshrink pull`, then restart the unit.

Then:

```bash
cd ~/vidshrink
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/vidshrink --help
```

## 4. Google accounts and OAuth

### Download account (new Google account)

Create a new Google account used only for downloads. From the main account, share the
library folders with it as **Viewer**. Read-only is enough.

### OAuth client (one for the cloud VMs)

Both remotes on a cloud VM, `gdrive:` and `gdrive-cloud:`, use an OAuth client of your
own. Don't use rclone's built-in client: Google is retiring rclone's shared client
during 2026. The cloud VMs share one client, separate from the server's and the PC's, so
they can be revoked without touching those. In production it lives in the GCP VM's
project.

In that Google Cloud project:

1. Enable the Google Drive API (if not already).
2. Create an OAuth client ID of type "Desktop app". Note the client ID and secret.
3. On the OAuth consent screen, set the publishing status to **In production**. In
   "Testing", Google expires refresh tokens after 7 days and the unattended worker
   loses Drive access every week (`invalid_grant`). A published but unverified app is
   fine for your own accounts; Google shows an "unverified app" warning at
   authorization.

**A token only works with the client that issued it.** If you change a remote's
`client_id` or `client_secret`, authorize that remote again. Otherwise it keeps working
until its access token runs out, then fails with `invalid_client` about an hour later.
Edit `rclone.conf` directly to change the client. `rclone config update` starts a new
authorization.

### Authorizing the remotes

Create both remotes on the VM with `rclone config`, with the client ID and secret above:

- `gdrive`: type `drive`, scope full access. Log in as the **main** account. Set the
  root so it points at the same Drive root as the server's `gdrive:` (copy non-secret
  options such as `root_folder_id` from the server's config; not its token).
- `gdrive-cloud`: type `drive`, scope `drive.readonly`, and in the advanced options
  `shared_with_me = true`. Log in as the **download** account.

The VM has no browser, so do the login through a port forward from a device that has
one (a PC, or a phone running Termux):

```bash
ssh -L 53682:127.0.0.1:53682 <user>@<vm public IP>
# then, on the VM:
rclone config reconnect gdrive:        # answer y to auto config
rclone config reconnect gdrive-cloud:
```

Open the printed `http://127.0.0.1:53682/auth?...` link in that device's browser and
log in. Run the `rclone` commands at the VM prompt, not on the device: if the device
runs rclone itself, the port forward holds port 53682 and rclone fails with "address
already in use".

Without a port forward: answer `n` to auto config, run the printed
`rclone authorize "drive" ...` command on the device, and paste its result at the VM's
`config_token>` prompt.

### Check that the paths line up

Pick a known library file and list it through both remotes:

```bash
rclone lsjson --hash 'gdrive:<path/to/known-file>'
rclone lsjson --hash 'gdrive-cloud:<path/to/known-file>'
```

The `Path`, `Size` and `Hashes.md5` must be identical. If they differ, fix the root or
shared-with-me options of `gdrive-cloud` before going on. Do not start the worker until
they match.

## 5. Config

Create the work directory and `~/.config/vidshrink/config.toml`:

```bash
mkdir -p ~/vidshrink-work ~/.config/vidshrink
install -m 600 /dev/null ~/.config/vidshrink/config.toml
```

Then write this into it (the file is already private, mode 600):

```toml
server = "http://127.0.0.1:8765"
token = "<job-service token>"
remote = "gdrive:"
download_remote = "gdrive-cloud:"
download_account = "cloud"
encoder = "x265-cloud"
max_hours = 24.0
disk_check = true
queue_disk_gb = 120
queue_hours = 24
reserve_days = 1
bwlimit = ""
worker_name = "oracle-a1"
workdir = "/home/ubuntu/vidshrink-work"
```

On GCP: `worker_name = "gcp-t2d"`, `queue_disk_gb = 100` (145 GB disk) and the
workdir under that VM's home.

`max_hours` is the longest encode the worker will claim (0 turns it off). `disk_check`
skips jobs that do not fit in the free disk.

The worker downloads ahead while it encodes. `queue_disk_gb` caps the files it keeps on
disk (sources waiting plus results not yet uploaded); leave room on the disk.
`queue_hours` caps the expected encode time of jobs it holds but has not started.
`reserve_days` is how long the server keeps a finished download reserved for this VM if
it goes quiet. The VMs are meant to be always on, so one day is enough. A worker that
stays stopped longer finds some jobs given away on its next start; it deletes their
local files (`resume refused, deleting: ...`).

## 6. Run it

```bash
mkdir -p ~/.config/systemd/user
cp ~/vidshrink/deploy/vidshrink-cloud-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vidshrink-cloud-worker
journalctl --user -u vidshrink-cloud-worker -f
```

The unit restarts after a crash but not after a clean exit. If a Drive login dies, the
worker releases its job, sends an alert (shown by `vidshrink status` on the server as
`ALERT <worker>: ...`) and exits. Re-authorize the remote as in section 4, then start it
again by hand:

```bash
systemctl --user start vidshrink-cloud-worker
```

To stop claiming work, `systemctl --user stop vidshrink-cloud-worker`.

On a spot VM the unit starts on its own when the VM boots, and the worker resumes the
downloads and encodes it kept.

## 7. Revoking access

- Drive access: in each Google account, remove the cloud VMs' OAuth client under
  Security > Third-party access, or delete the client in its Cloud project. The server
  and PC use other clients and keep working.
- Network: remove the VM's SSH alias and tunnel unit on the server (or the VM from the
  tailnet).
- Job service: there is one shared token. Rotate it by hand: replace
  `~/vidshrink/serve.token` on the server, restart `vidshrink-serve`, and update the
  token in the PC's and every other worker's config.
- Download account: remove its Viewer shares, or delete the account.
