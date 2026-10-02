import urllib.parse
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from vidshrink import db, scorer
from vidshrink.config import Config
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.budget import Budget
from vidshrink.rclone import DOWNLOAD_QUOTA_MARKER, Rclone

ProbeFn = Callable[[str, int], Probe]


def http_prober(base_url: str) -> ProbeFn:
    def probe(path: str, size: int) -> Probe:
        return parse_ffprobe(run_ffprobe(base_url + urllib.parse.quote(path)), size)
    return probe


def scan(conn, rclone: Rclone, cfg: Config, probe_fn: ProbeFn,
         log: Callable[[str], None] = print, download_budget: Budget | None = None,
         stats: Callable[[], dict] | None = None) -> dict:
    """List, probe and rank. With `stats` (rclone core/stats of the probe server),
    bytes downloaded are charged to `download_budget` after every batch; the scan stops
    when the budget is used up or Drive reports its download quota exceeded, leaving the
    rest for the next scan."""
    entries = rclone.list_videos(cfg.extensions)
    log(f"listed {len(entries)} videos")
    with db.transaction(conn):
        for e in entries:
            db.upsert_listing(conn, e["Path"], e["Size"], e["ModTime"])
        gone = db.mark_gone(conn, (e["Path"] for e in entries))
        # Drive allows several files with one name; we can't tell which one we'd replace.
        dups = [p for p, n in Counter(e["Path"] for e in entries).items() if n > 1]
        if db.mark_duplicates(conn, dups):
            log(f"skipping {len(dups)} duplicate names: {dups[:5]}")

    counts = {"listed": len(entries), "gone": gone, "probed": 0, "probe_failed": 0,
              "excluded": 0, "stopped": None}
    todo = []
    # probe_failed is retried every scan: most failures are transient (network, serve http).
    for row in db.rows_in(conn, "unprobed", "probe_failed"):
        if any(row["path"].startswith(p) for p in cfg.excluded):
            db.mark(conn, row["id"], "skipped", reason="excluded folder")
            counts["excluded"] += 1
        else:
            todo.append(row)
    log(f"probing {len(todo)} files")

    n = 0
    start_bytes = last_bytes = stats()["bytes"] if stats else 0
    with ThreadPoolExecutor(cfg.probe_workers) as pool:
        for start in range(0, len(todo), cfg.scan_batch):
            if download_budget and not download_budget.allows(1):
                counts["stopped"] = ("download quota" if download_budget.blocked()
                                     else "download budget")
                log(f"stopping: {counts['stopped']} reached "
                    f"({download_budget.used() / 1e9:.1f} GB today); rerun scan later")
                break
            batch = todo[start:start + cfg.scan_batch]
            futures = {pool.submit(probe_fn, r["path"], r["size"]): r for r in batch}
            for fut in as_completed(futures):
                row = futures[fut]
                n += 1
                try:
                    db.save_probe(conn, row["path"], fut.result().to_json())
                    counts["probed"] += 1
                except (ProbeError, ValueError, TypeError, KeyError) as e:
                    db.mark(conn, row["id"], "probe_failed", reason=str(e)[:300])
                    counts["probe_failed"] += 1
                if n % 500 == 0:
                    log(f"probed {n}/{len(todo)}")
            if stats:
                s = stats()
                if download_budget:
                    download_budget.add(s["bytes"] - last_bytes)
                last_bytes = s["bytes"]
                if DOWNLOAD_QUOTA_MARKER in (s.get("lastError") or ""):
                    if download_budget:
                        download_budget.block(cfg.download_block_secs)
                    counts["stopped"] = "download quota"
                    log("stopping: Drive says the download quota is exceeded; "
                        "downloads paused, rerun scan later")
                    break
    if stats and n:
        log(f"downloaded {(last_bytes - start_bytes) / 1e6:.0f} MB for {n} probes "
            f"({(last_bytes - start_bytes) / n / 1e6:.2f} MB per probe)")

    scorer.rescore(conn, cfg)
    return counts
