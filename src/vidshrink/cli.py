import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from vidshrink import db, scorer
from vidshrink.budget import Budget
from vidshrink.config import Config
from vidshrink.probe import Probe
from vidshrink.rclone import Rclone, free_port, serve_http
from vidshrink.scanner import http_prober, scan
from vidshrink.worker import Worker


def _log(msg: str) -> None:
    print(msg, flush=True)


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

    lines.append(f"Uploaded today ({budget.day()} Pacific): "
                 f"{budget.used() / 1e9:.0f} / {budget.limit / 1e9:.0f} GB")
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
    args = ap.parse_args(argv)

    cfg = replace(base, remote=args.remote, db_path=args.db, workdir=args.workdir)
    if "VIDSHRINK_MIN_SIZE" in os.environ:
        cfg = replace(cfg, min_size=int(os.environ["VIDSHRINK_MIN_SIZE"]))
    if not cfg.remote.startswith("gdrive:"):
        cfg = replace(cfg, rclone_args=())  # drive-only flags are noise elsewhere

    conn = db.connect(cfg.db_path)
    rclone = Rclone(cfg.remote, cfg.rclone_args)
    budget = Budget(conn, cfg.daily_upload_limit)

    if args.cmd == "scan":
        cfg = replace(cfg, probe_workers=args.workers)
        with serve_http(rclone, free_port()) as url:
            counts = scan(conn, rclone, cfg, http_prober(url), log=_log)
        _log(f"scan finished: {counts}")
    elif args.cmd == "run":
        n = Worker(conn, rclone, cfg, budget, log=_log).run(limit=args.limit, dry_run=args.dry_run)
        _log(f"run finished: {n} files")
    else:
        print(status_report(conn, cfg, budget))
    return 0


if __name__ == "__main__":
    sys.exit(main())
