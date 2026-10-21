import argparse
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.jobs import ALERT_PREFIX, PC_SEEN_KEY, WORKER_SEEN_PREFIX, JobStore, load_or_create_token, make_server
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone, free_port, serve_http, served_stats
from vidshrink.paths import tmp_path
from vidshrink.scanner import http_prober, scan


def _log(msg: str) -> None:
    print(msg, flush=True)


def _gb_of_limit(b: Budget) -> str:
    used = f"{b.used() / 1e9:.0f}"
    return f"{used} GB (no cap)" if b.limit is None else f"{used} / {b.limit / 1e9:.0f} GB"


def status_report(conn, cfg: Config, budget: Budget) -> str:
    lines = ["Stages:"]
    for row in conn.execute("SELECT stage, COUNT(*) n, SUM(size) s FROM videos GROUP BY stage ORDER BY n DESC"):
        lines.append(f"  {row['stage']:<14} {row['n']:>7}  {(row['s'] or 0) / 1e12:8.2f} TB")

    done = conn.execute("SELECT COUNT(*) n, SUM(size - out_size) s FROM videos WHERE stage='done'").fetchone()
    lines.append(f"Saved so far: {(done['s'] or 0) / 1e12:.2f} TB across {done['n']} files")

    queued = db.rows_in(conn, "queued")
    ratios = {f: scorer.ratio_for(conn, f) for f in scorer.DEFAULT_RATIO}
    speeds = {r: scorer.speed_for(conn, r) for r in scorer.DEFAULT_SPEED}
    saved = hours = 0.0
    for row in queued:
        p = Probe.from_json(row["probe_json"])
        fam = scorer.codec_family(p.codec)
        res = scorer.resolution_class(p.width, p.height)
        saved += row["size"] * ratios[fam]
        hours += p.duration * speeds[res] / 3600
    lines.append(f"Projected for the queue: {saved / 1e12:.2f} TB saved, "
                 f"{hours:,.0f} encode hours ({hours / 24 / 365:.1f} years)")

    lines.append("Top of queue (GB saved per encode hour):")
    for row in queued[:10]:
        lines.append(f"  {row['score']:7.2f}  {row['size'] / 1e9:6.1f} GB  {row['path']}")

    failed = db.rows_in(conn, "failed")
    if failed:
        lines.append(f"Failed ({len(failed)}), latest:")
        for row in sorted(failed, key=lambda r: -r["updated_at"])[:10]:
            lines.append(f"  {row['path']}: {row['reason']}")

    swapping = db.rows_in(conn, "swapping")
    if swapping:
        lines.append(f"Swaps waiting to be retried ({len(swapping)}):")
        for row in sorted(swapping, key=lambda r: -r["updated_at"])[:10]:
            lines.append(f"  {row['path']}: {row['reason'] or 'swap in progress or interrupted'}")

    dry = db.rows_in(conn, "dry_run")
    if dry:
        lines.append(f"Dry-run uploads to review ({len(dry)}):")
        for row in sorted(dry, key=lambda r: r["path"]):
            lines.append(f"  {row['path']} -> {tmp_path(row['path'])}")

    leased = db.rows_in(conn, "leased")
    if leased:
        lines.append(f"With remote workers ({len(leased)}):")
        for row in leased:
            left = max(0, (row["lease_until"] or 0) - budget.now()) / 60
            lines.append(f"  {row['path']} ({row['worker']}, lease {left:.0f} min left)")
    remote = db.rows_in(conn, "remote_ready")
    if remote:
        lines.append(f"Remote results waiting for the server ({len(remote)}):")
        for row in remote:
            lines.append(f"  {row['path']}")
    seen = db.kv_get(conn, PC_SEEN_KEY)
    if seen:
        ago = (budget.now() - float(seen)) / 3600
        lines.append(f"PC last seen: {ago:.1f} h ago")

    lines.append(f"Uploaded today ({budget.day()} Pacific): "
                 f"{budget.used() / 1e9:.0f} / {budget.limit / 1e9:.0f} GB")
    downloads = Budget(conn, cfg.daily_download_limit, budget.now, kind="download")
    paused = " (PAUSED: Drive download quota hit)" if downloads.blocked() else ""
    lines.append(f"Downloaded today: {downloads.used() / 1e9:.0f} / "
                 f"{downloads.limit / 1e9:.0f} GB{paused}")
    pc_downloads = Budget(conn, cfg.daily_pc_download_limit, budget.now, kind="pc_download")
    pc_recently_seen = bool(seen) and (budget.now() - float(seen)) / 86400 <= cfg.pc_absent_days
    if pc_downloads.used() > 0 or pc_recently_seen:
        pc_paused = " (PAUSED: Drive download quota hit)" if pc_downloads.blocked() else ""
        lines.append(f"PC account downloaded today: {_gb_of_limit(pc_downloads)}{pc_paused}")
    cloud_downloads = Budget(conn, cfg.daily_cloud_download_limit, budget.now, kind="cloud_download")
    if cloud_downloads.used() > 0 or cloud_downloads.blocked():
        cloud_paused = " (PAUSED: Drive download quota hit)" if cloud_downloads.blocked() else ""
        lines.append(f"Cloud account downloaded today: {_gb_of_limit(cloud_downloads)}{cloud_paused}")
    for row in conn.execute("SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key",
                            (f"{WORKER_SEEN_PREFIX}%",)):
        name = row["key"][len(WORKER_SEEN_PREFIX):]
        if conn.execute("SELECT 1 FROM videos WHERE worker=? AND encoder='nvenc' LIMIT 1",
                        (name,)).fetchone():
            continue  # the PC: already shown as "PC last seen"
        ago = (budget.now() - float(row["value"])) / 3600
        lines.append(f"{name} last seen: {ago:.1f} h ago")
    for row in conn.execute("SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key",
                            (f"{ALERT_PREFIX}%",)):
        lines.append(f"ALERT {row['key'][len(ALERT_PREFIX):]}: {json.loads(row['value'])['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    base = Config()
    ap = argparse.ArgumentParser(prog="vidshrink")
    ap.add_argument("--remote", default=base.remote)
    ap.add_argument("--db", type=Path, default=base.db_path)
    ap.add_argument("--workdir", type=Path, default=base.workdir)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="list and probe videos, then rank them")
    sc.add_argument("--workers", type=int, default=base.probe_workers)
    rn = sub.add_parser("run", help="encode and replace files, best first")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--limit", type=int)
    sub.add_parser("status", help="show progress and the queue")
    sv = sub.add_parser("serve", help="job service for the PC worker (LAN only)")
    sv.add_argument("--host", default=base.serve_host)
    sv.add_argument("--port", type=int, default=base.serve_port)
    sub.add_parser("token", help="print the job-service token")
    for name, helptext in (("pc-worker", "run the PC worker in this window"),
                           ("tray", "PC worker tray icon"),
                           ("calibrate", "pick the NVENC CQ with VMAF")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", type=Path, default=None)
        if name == "calibrate":
            p.add_argument("--samples", type=int, default=3)
            p.add_argument("--cqs", default="20,22,24,26")
    lc = sub.add_parser("local", help="compress a local folder of videos in place")
    lc.add_argument("folder", type=Path)
    lc.add_argument("--config", type=Path, default=None)
    lc.add_argument("--cq", type=int, default=None, help="NVENC CQ (default: config's)")
    lc.add_argument("--dry-run", action="store_true")
    lc.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    if args.cmd in ("pc-worker", "tray", "calibrate"):
        return _pc_main(args)
    if args.cmd == "local":
        return _local_main(args)

    cfg = replace(base, remote=args.remote, db_path=args.db, workdir=args.workdir)
    if "VIDSHRINK_MIN_SIZE" in os.environ:
        cfg = replace(cfg, min_size=int(os.environ["VIDSHRINK_MIN_SIZE"]))
    if "VIDSHRINK_TOKEN_PATH" in os.environ:
        cfg = replace(cfg, token_path=Path(os.environ["VIDSHRINK_TOKEN_PATH"]))
    if not cfg.remote.startswith("gdrive:"):
        cfg = replace(cfg, rclone_args=())  # drive-only flags are noise elsewhere

    conn = db.connect(cfg.db_path)
    rclone = Rclone(cfg.remote, cfg.rclone_args)
    budget = Budget(conn, cfg.daily_upload_limit)

    if args.cmd == "token":
        print(load_or_create_token(cfg.token_path))
        return 0
    if args.cmd == "serve":
        token = load_or_create_token(cfg.token_path)
        store = JobStore(conn, cfg, rclone, log=_log)
        server = make_server(store, token, args.host, args.port)
        _log(f"job service on {args.host}:{args.port} (token in {cfg.token_path})")
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0

    if args.cmd == "scan":
        cfg = replace(cfg, probe_workers=args.workers)
        rc_port = free_port()
        downloads = Budget(conn, cfg.daily_download_limit, kind="download")
        with serve_http(rclone, free_port(), rc_port=rc_port, serve_args=cfg.serve_args) as url:
            counts = scan(conn, rclone, cfg, http_prober(url), log=_log,
                          download_budget=downloads, stats=lambda: served_stats(rc_port))
        _log(f"scan finished: {counts}")
    elif args.cmd == "run":
        from vidshrink.worker import Worker  # server-only (fcntl); keep the PC importable
        n = Worker(conn, rclone, cfg, budget, log=_log).run(limit=args.limit, dry_run=args.dry_run)
        _log(f"run finished: {n} files")
    else:
        print(status_report(conn, cfg, budget))
    return 0


def _pc_main(args) -> int:
    from vidshrink.jobs import JobClient
    from vidshrink.pc_worker import (PCWorker, apply_ffmpeg_dir, default_config_path,
                                      load_pc_config, pc_download_rclone, pc_rclone)
    pcfg = load_pc_config(args.config or default_config_path())
    apply_ffmpeg_dir(pcfg)
    client = JobClient(pcfg.server, pcfg.token)

    def make_worker():
        return PCWorker(pcfg, client, pc_rclone(pcfg),
                        download_rclone=pc_download_rclone(pcfg), log=_log)

    if args.cmd == "pc-worker":
        _run_until_ctrl_c(make_worker())
        return 0
    if args.cmd == "tray":
        from vidshrink.tray import TrayController, run_tray
        run_tray(TrayController(make_worker))
        return 0
    return _calibrate(args, pcfg, client)


def _local_main(args) -> int:
    from vidshrink.encode import build_command, build_nvenc_command, run_encode_cancellable
    from vidshrink.local import compress_folder
    from vidshrink.pc_worker import PCConfig, apply_ffmpeg_dir, default_config_path, \
        load_pc_config
    path = args.config or default_config_path()
    pcfg = load_pc_config(path) if path.exists() else PCConfig(server="", token="", encoder="x265-cloud")
    apply_ffmpeg_dir(pcfg)
    if not args.folder.is_dir():
        _log(f"not a folder: {args.folder}")
        return 2
    cq = args.cq if args.cq is not None else pcfg.cq

    def encode(src, out, probe, log_path, cancel):
        cmd = (build_nvenc_command(src, out, probe, cq) if pcfg.encoder == "nvenc"
               else build_command(src, out, probe, Config()))
        return run_encode_cancellable(cmd, log_path, cancel)

    cancel = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.update(totals=compress_folder(
        args.folder, encode, pcfg.min_saving, args.dry_run, args.limit, cancel, _log)),
        daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        _log("Ctrl-C: stopping; the file being encoded is left unchanged")
        cancel.set()
        t.join()
    totals = result.get("totals")
    return 0 if totals is not None and totals.failed == 0 else 1


def _run_until_ctrl_c(w) -> None:
    """Run the worker in a thread so Ctrl-C lands in the main thread, which then
    stops it now (releasing its job) and waits for it to finish. Its ffmpeg/rclone
    children run in their own process group, so the Ctrl-C doesn't reach them."""
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        _log("Ctrl-C: stopping now and releasing the job")
        w.stop(now=True)
        t.join()


def _calibrate(args, pcfg, client) -> int:
    from vidshrink.calibrate import cut_sample, format_table, measure, pick_cq
    from vidshrink.encode import build_command, build_nvenc_command, run_encode_cancellable
    from vidshrink.pc_worker import pc_download_rclone
    from vidshrink.probe import Probe
    rc = pc_download_rclone(pcfg)
    download_account = "pc" if pcfg.download_remote else "main"
    pcfg.workdir.mkdir(parents=True, exist_ok=True)
    jobs, samples = [], []
    try:
        for _ in range(args.samples):
            job = client.claim(pcfg.worker_name, "nvenc",
                               download_account=download_account)["job"]
            if job is None:
                break
            # Keep it leased until every sample is cut: the server hands out the
            # top-scored queued job, so releasing it now would make the next claim
            # return (and download) the same file again.
            jobs.append(job)
            probe = Probe(**job["probe"])
            src = pcfg.workdir / f"cal-{job['id']}.src"
            _log(f"downloading {job['path']}")
            rc.download(job["path"], src)
            sample = pcfg.workdir / f"cal-{job['id']}.mkv"
            cut_sample(src, sample, probe)
            src.unlink()
            samples.append((sample, probe))
            for held in list(jobs):
                try:
                    if client.heartbeat(held["id"], pcfg.worker_name,
                                        download_account=download_account).get("cancel"):
                        jobs.remove(held)  # lease already gone; nothing to release
                except Exception:
                    pass
        # Release before measure(): it can outlast the lease (1800 s), and there's
        # no heartbeat during it.
        for job in jobs:
            try:
                client.release(job["id"], pcfg.worker_name)
            except Exception:
                pass
        jobs.clear()
        x265 = lambda s, o, p, lg, c: run_encode_cancellable(build_command(s, o, p, Config()), lg, c)
        nvenc = lambda cq: (lambda s, o, p, lg, c:
                            run_encode_cancellable(build_nvenc_command(s, o, p, cq), lg, c))
        cqs = [int(x) for x in args.cqs.split(",")]
        rows = measure(samples, cqs, nvenc, x265, pcfg.workdir, log=_log)
        print(format_table(rows))
        print(f"\nSuggested cq = {pick_cq(rows)}  (set it in {args.config or 'config.toml'})")
    finally:
        for job in jobs:
            try:
                client.release(job["id"], pcfg.worker_name)
            except Exception:
                pass
        for sample, _ in samples:
            sample.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
