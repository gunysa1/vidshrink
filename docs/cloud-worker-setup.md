# Setting up the cloud worker (Oracle A1, x265)

The cloud worker is the PC worker run headless on a Linux VM. It claims jobs from the
server over Tailscale, encodes them with software x265, uploads the result, and reports
back. It is on-demand: it only claims work while its unit runs.

Rules for the whole guide:

- Never copy a token or `rclone.conf` between machines. Every remote is authorized fresh.
- Nothing secret goes in the repo. Tokens and client secrets stay on the machines.

## 1. The instance

Already created in the Oracle console:

- Shape `VM.Standard.A1.Flex`, 4 OCPU, 24 GB RAM.
- Ubuntu 24.04 LTS, aarch64, user `ubuntu`.
- 200 GB boot volume (the workdir lives on it).
- Security list: only SSH inbound. Port 8765 is never opened.

Let user services run without a login session:

```bash
sudo loginctl enable-linger ubuntu
```

## 2. Tailscale (server and VM)

Install Tailscale on the server and on the VM and log both into the same tailnet
(`sudo tailscale up`; open the printed link in a browser). Then, on the VM, check the job
service is reachable. Print the token on the server with `vidshrink token`.

```bash
TOKEN='<job-service token>'
curl -H "Authorization: Bearer $TOKEN" http://<server-tailnet-name>:8765/health
```

It should answer with the health JSON. `vidshrink-serve` already binds `0.0.0.0:8765`,
which includes the Tailscale interface, so nothing changes on the server. Once this works
you can restrict SSH in the Oracle security list to the tailnet.

## 3. Tools on the VM

### ffmpeg

Use the same version as the server: BtbN `n9.0.2`, the `linuxarm64` GPL build. Take the
`ffmpeg-n9.0.2-...-linuxarm64-gpl` tarball from the BtbN FFmpeg-Builds releases page.

```bash
mkdir -p ~/.local/opt ~/.local/bin
tar -xf ffmpeg-n9.0.2-*-linuxarm64-gpl*.tar.xz -C ~/.local/opt
ln -sf ~/.local/opt/ffmpeg-n9.0.2-*-linuxarm64-gpl*/bin/ffmpeg  ~/.local/bin/ffmpeg
ln -sf ~/.local/opt/ffmpeg-n9.0.2-*-linuxarm64-gpl*/bin/ffprobe ~/.local/bin/ffprobe
ffmpeg -version | head -1
```

### rclone

Install the arm64 build from rclone.org (`curl https://rclone.org/install.sh | sudo bash`
picks the right one), then check `rclone version`.

### vidshrink

```bash
cd ~
git clone <repo-url> vidshrink
cd vidshrink
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/vidshrink --help
```

## 4. Google accounts and OAuth

### Download account (new Google account)

Create a new Google account used only for downloads. From the main account, share the
library folders with it as **Viewer**. Read-only is enough.

### Upload client (separate OAuth client)

The upload remote `gdrive:` is a new authorization of the main account, made for this VM
only. It must use its **own OAuth client**, different from the one the server and PC use,
so the VM can be revoked alone.

In your own Google Cloud project:

1. Enable the Google Drive API (if not already).
2. Create an OAuth client ID of type "Desktop app". Note the client ID and secret.
3. On the OAuth consent screen, set the publishing status to **In production**. In
   "Testing", Google expires refresh tokens after 7 days and the unattended worker would
   lose upload access every week. A published but unverified app is fine for your own
   account; Google shows an "unverified app" warning once, at authorization.

### Authorizing the remotes

The browser step of `rclone authorize` runs on a machine with a browser (the PC), not on
the VM.

Upload remote, with the new client:

```bash
rclone authorize "drive" "<client_id>" "<client_secret>"
```

Log in as the main account. Copy the printed token, then on the VM run `rclone config`:
new remote `gdrive`, type `drive`, the same client ID and secret, scope full access, and
paste the token at "config_token". Set the root so it points at the same Drive root as the
server's `gdrive:` (copy the non-secret options such as `root_folder_id` from the server's
config; not its token).

Download remote: model `gdrive-cloud:` on the PC's existing download remote for its own
second account. Use the same non-secret options (`root_folder_id`, shared-with-me
settings and so on), but a fresh authorization:

```bash
rclone authorize "drive"
```

Log in as the new download account, then on the VM create the remote `gdrive-cloud`
(type `drive`, scope read only) and paste the token.

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
chmod 600 ~/.config/vidshrink/config.toml   # after creating it
```

```toml
server = "http://<server-tailnet-name>:8765"
token = "<job-service token>"
remote = "gdrive:"
download_remote = "gdrive-cloud:"
download_account = "cloud"
encoder = "x265-cloud"
max_hours = 24.0
disk_check = true
bwlimit = ""
worker_name = "oracle-a1"
workdir = "/home/ubuntu/vidshrink-work"
```

`max_hours` is the longest encode the worker will claim (0 turns it off). `disk_check`
skips jobs that do not fit in the free disk.

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
`ALERT oracle-a1: ...`) and exits. Re-authorize the remote as in section 4, then start it
again by hand:

```bash
systemctl --user start vidshrink-cloud-worker
```

To stop claiming work, `systemctl --user stop vidshrink-cloud-worker`.

## 7. Revoking access

- Upload access: in the main Google account, remove the VM's OAuth client under
  Security > Third-party access. The server and PC use another client and keep working.
- Network: remove the VM from the tailnet (Tailscale admin console).
- Job service: there is one shared token. Rotate it by hand: replace
  `~/vidshrink/serve.token` on the server, restart `vidshrink-serve`, and update the
  token in the PC's and any other worker's config.
- Download account: remove its Viewer shares, or delete the account.
