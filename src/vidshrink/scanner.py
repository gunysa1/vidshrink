import urllib.parse
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from vidshrink import db, scorer
from vidshrink.config import Config
from vidshrink.probe import Probe, ProbeError, parse_ffprobe, run_ffprobe
from vidshrink.rclone import Rclone

ProbeFn = Callable[[str, int], Probe]


def http_prober(base_url: str) -> ProbeFn:
    def probe(path: str, size: int) -> Probe:
        return parse_ffprobe(run_ffprobe(base_url + urllib.parse.quote(path)), size)
    return probe


def scan(conn, rclone: Rclone, cfg: Config, probe_fn: ProbeFn,
         log: Callable[[str], None] = print) -> dict[str, int]:
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

    counts = {"listed": len(entries), "gone": gone, "probed": 0, "probe_failed": 0, "excluded": 0}
    todo = []
    for row in db.rows_in(conn, "unprobed"):
        if any(row["path"].startswith(p) for p in cfg.excluded):
            db.mark(conn, row["id"], "skipped", reason="excluded folder")
            counts["excluded"] += 1
        else:
            todo.append(row)
    log(f"probing {len(todo)} files")

    with ThreadPoolExecutor(cfg.probe_workers) as pool:
        futures = {pool.submit(probe_fn, r["path"], r["size"]): r for r in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            row = futures[fut]
            try:
                db.save_probe(conn, row["path"], fut.result().to_json())
                counts["probed"] += 1
            except (ProbeError, ValueError, TypeError, KeyError) as e:
                db.mark(conn, row["id"], "probe_failed", reason=str(e)[:300])
                counts["probe_failed"] += 1
            if n % 500 == 0:
                log(f"probed {n}/{len(todo)}")

    scorer.rescore(conn, cfg)
    return counts
