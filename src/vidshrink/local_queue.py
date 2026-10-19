"""On-disk queue of jobs held by a remote worker (PC or cloud VM).

Pure logic, no threads: the stage threads in pc_worker build on LocalQueue and
LocalJob. Must stay importable on Windows (no vidshrink.worker, no fcntl).

Per job `<id>` the workdir holds `<id>.job.json` (the claim's job dict),
`<id>.src`, `<id>.out.mkv`, `<id>.log` and the `<id>.downloaded` /
`<id>.encoded` markers."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

STATES = ("downloading", "downloaded", "encoding", "encoded", "uploading")


@dataclass
class LocalJob:
    job: dict
    state: str
    workdir: Path = Path(".")

    @property
    def id(self) -> int:
        return int(self.job["id"])

    @property
    def src(self) -> Path:
        return self.workdir / f"{self.id}.src"

    @property
    def out(self) -> Path:
        return self.workdir / f"{self.id}.out.mkv"

    @property
    def log(self) -> Path:
        return self.workdir / f"{self.id}.log"


def _read_number(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class LocalQueue:
    def __init__(self, workdir: Path, queue_disk_bytes: int, queue_hours: float,
                 disk_factor: float, disk_reserve: int):
        self.workdir = Path(workdir)
        self.queue_disk_bytes = queue_disk_bytes
        self.queue_hours = queue_hours
        self.disk_factor = disk_factor
        self.disk_reserve = disk_reserve
        self._jobs: dict[int, LocalJob] = {}
        self._lock = threading.Lock()

    def _job(self, job: dict, state: str) -> LocalJob:
        return LocalJob(job=job, state=state, workdir=self.workdir)

    def add(self, job: dict) -> LocalJob:
        """Accept a claimed job: write its `.job.json` atomically and hold it
        in state "downloading"."""
        lj = self._job(job, "downloading")
        path = self.workdir / f"{lj.id}.job.json"
        tmp = self.workdir / f"{lj.id}.job.json.tmp"
        tmp.write_text(json.dumps(job))
        os.replace(tmp, path)
        with self._lock:
            self._jobs[lj.id] = lj
        return lj

    def set_state(self, job_id: int, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        with self._lock:
            self._jobs[job_id].state = state

    def held_ids(self) -> set[int]:
        with self._lock:
            return set(self._jobs)

    def scan(self) -> tuple[list[LocalJob], list[int]]:
        """Read the workdir after a restart. Returns (resumable jobs, ids of
        incomplete jobs whose files the caller deletes). Resumable jobs are
        held in memory; unreadable `.job.json` files are ignored."""
        resumable: list[LocalJob] = []
        incomplete: list[int] = []
        try:
            entries = sorted(self.workdir.glob("*.job.json"))
        except OSError:
            entries = []
        for p in entries:
            try:
                job = json.loads(p.read_text())
                jid = int(job["id"])
                size = job["size"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not p.name.startswith(f"{jid}."):
                continue
            lj = self._job(job, "downloading")
            if _read_number(self.workdir / f"{jid}.encoded") is not None and lj.out.exists():
                lj.state = "encoded"
            elif (_read_number(self.workdir / f"{jid}.downloaded") is not None
                  and lj.src.exists() and _size(lj.src) == size):
                lj.state = "downloaded"
            else:
                incomplete.append(jid)
                continue
            resumable.append(lj)
        with self._lock:
            for lj in resumable:
                self._jobs[lj.id] = lj
        return resumable, incomplete

    def admits(self, size: int, expected_hours: float, free_bytes: int) -> bool:
        """Whether a job of `size` bytes and `expected_hours` may be claimed."""
        with self._lock:
            held = list(self._jobs.values())
        on_disk = sum(_size(j.src) + _size(j.out) for j in held)
        if on_disk + size > self.queue_disk_bytes:
            return False
        if size * self.disk_factor + self.disk_reserve > free_bytes:
            return False
        # Jobs behind the encoder. With none, a job is always admitted so a long
        # film can be claimed, and one file can be prefetched behind a long encode.
        behind = [j for j in held if j.state in ("downloading", "downloaded")]
        if behind:
            hours = sum(float(j.job.get("expected_hours") or 0.0) for j in held
                        if j.state in ("downloading", "downloaded", "encoding"))
            if hours + expected_hours > self.queue_hours:
                return False
        return True

    def drop(self, job_id: int) -> None:
        """Stop holding the job in memory; its files stay on disk."""
        with self._lock:
            self._jobs.pop(job_id, None)

    def forget(self, job_id: int) -> None:
        """Drop the job and delete every file of that id."""
        from vidshrink.pc_worker import _NUMBERED_PATTERNS  # lazy: pc_worker imports us
        with self._lock:
            self._jobs.pop(job_id, None)
        try:
            entries = list(self.workdir.iterdir())
        except OSError:
            return
        for p in entries:
            for pat in _NUMBERED_PATTERNS:
                m = pat.match(p.name)
                if m:
                    if int(m.group(1)) == job_id and p.is_file():
                        try:
                            p.unlink()
                        except OSError:
                            pass
                    break
