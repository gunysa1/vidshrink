# Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the five defects found in the 2026-10-18 whole-project review, plus one cheap speed-up of the scan's Drive listing.

**Architecture:** All changes are small and local: the shared ffmpeg command builder (`encode.py`), the listing upsert (`db.py`), server restart recovery (`worker.py`), the PC claim ordering (`jobs.py`) and the Drive listing (`rclone.py`). No schema change, no protocol change between server and PC. Each task is independently shippable.

**Tech Stack:** Python 3.11+, SQLite, pytest, ffmpeg 9 / ffprobe, rclone.

**Spec:** none — the review findings below are the authority. Where a finding and existing code disagree, the finding wins only when its failure scenario is reproduced by the task's test.

## Review findings this plan fixes

| # | Finding | Evidence |
|---|---|---|
| F1 | `eia_608` closed-caption tracks (35 queued `.mp4`, 13 GB) make the encode fail at the MKV header: `Subtitle codec eia_608 (94218) is not supported`. Both encoders hit it; each file burns 2 attempts and a download. | Reproduced over HTTP on one affected episode. |
| F2 | 26 queued `.mkv` (one show, 34 GB) carry a subtitle stream with an **empty codec name**; copying an unknown-codec stream into MKV fails the same way. | Census of `probe_json`; failure not yet reproduced (file is 0.9 GB; Task 2 verifies cheaply). |
| F3 | `Worker.recover()` burns an attempt for every interruption (reboot, crash, `systemctl restart`). With `max_attempts = 2`, one reboot plus one genuine failure parks a file permanently. | Journal since 2026-10-06: five files lost an attempt to "interrupted during encoding". |
| F4 | A row marked `gone` never comes back when the file reappears unchanged (restore from Drive trash, undo a move). `upsert_listing` only resets rows whose size/modtime changed. | Reproduced with a temp DB: gone → same listing → still gone. |
| F5 | `JobStore._candidates` re-scores all ~35k queued rows on every PC claim with two DB lookups per row: ≈3.3 s per claim on the single-threaded job service, every 5 min while the PC idles. | Timed on the live DB (read-only): 0.53 s read + 2.76 s score/sort. |
| F6 | `Rclone.list_videos` walks Drive folder by folder: the scan's listing step takes ~20–25 min. `--fast-list` lists the same 108,744 files in 74 s. | Timed 2026-10-18. |

Deferred (not in this plan, noted for later): Drive `tmp` leak in `_swap_failed` when the original is still present (none leaked so far — 0 tmp files on Drive); PC download charge is not refunded on `release`; tray shows plain "Off" after a local fault; one film's 3 s duration mismatch; interlaced sources (651 files, 2.15 TB) are skipped rather than deinterlaced.

## Global Constraints

- Never run tests against the real `gdrive:`; tests use the local-filesystem rclone remote from `tests/conftest.py`.
- Only the server deletes or replaces originals; nothing here changes that.
- Don't run two pytest sessions at once (shared basetemp).
- Keep `src/vidshrink/pc_worker.py`, `encode.py`, `jobs.py`, `paths.py`, `probe.py`, `verify.py`, `rclone.py` importable on Windows (no `fcntl`).
- Commit messages end with the attribution lines in effect for this session.
- Each task: failing test first, then the minimal change, then the full suite (`cd ~/vidshrink && .venv/bin/python -m pytest -q`, ~15 min while the server encodes).

---

### Task 1: Convert EIA-608 captions to SRT (or drop them) — F1

**Files:**
- Modify: `src/vidshrink/encode.py` (`CONVERTED`, `DROPPED_SUBS`, `_output_args`)
- Test: `tests/test_encode.py`

**Interfaces:**
- Consumes: `output_streams(streams)`, `TO_FLAC`, `DROPPED_SUBS`, `CONVERTED` from `encode.py` (added in `b18fff5`).
- Produces: `output_streams` now maps `eia_608 → subrip`; `_output_args` emits `-c:s:<i> srt` for it. `verify.compare` picks this up automatically through `output_streams`.

Decision rule: ffmpeg's `cc_dec` decoder turns EIA-608 into text, so conversion keeps the captions. If Step 1's manual check shows the conversion fails on the real file (empty output, error), fall back to dropping: add `"eia_608"` to `DROPPED_SUBS` instead of `CONVERTED`, and adjust the tests to expect the stream to disappear.

- [ ] **Step 1: Confirm the conversion on a real file (read-only, ~20 MB from the main account)**

```bash
cd /tmp && rclone serve http gdrive: --addr 127.0.0.1:43997 --read-only --tpslimit 10 \
  --buffer-size 0 --vfs-read-chunk-size 1M --vfs-read-chunk-size-limit 1M & SP=$!; sleep 3
U="http://127.0.0.1:43997/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' \
  "TV/<show>/Season 01/<show> - S01E09 - <title> [WEBRip-480p][AAC 2.0][h264].mp4")"
ffmpeg -hide_banner -nostdin -y -v error -i "$U" -map 0:v -map 0:a? -map 0:s? -c copy -c:s srt -t 30 cc.mkv; echo "exit $?"
ffprobe -v error -select_streams s -show_entries stream=codec_name -of csv=p=0 cc.mkv   # expect: subrip
kill $SP
```
Expected: exit 0 and `subrip`. If not, use the drop fallback described above.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_encode.py
def test_eia_608_captions_become_srt(cfg, tmp_path):
    probe = replace(SDR, streams=[["audio", "aac", "eng"], ["subtitle", "eia_608", "eng"]])
    cmd = build_command(tmp_path / "s", tmp_path / "o.mkv", probe, cfg)
    assert cmd[cmd.index("-c:s:0") + 1] == "srt"
    assert output_streams(probe.streams) == [["audio", "aac", "eng"], ["subtitle", "subrip", "eng"]]


def test_eia_608_after_a_dropped_sub_is_numbered_among_kept_subs(cfg, tmp_path):
    probe = replace(SDR, streams=[["subtitle", "arib_caption", "jpn"], ["subtitle", "eia_608", "eng"]])
    cmd = build_command(tmp_path / "s", tmp_path / "o.mkv", probe, cfg)
    assert cmd[cmd.index("-c:s:0") + 1] == "srt"   # arib dropped, eia_608 is output sub 0
    assert "-c:s:1" not in cmd
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_encode.py -k eia_608`
Expected: FAIL (`-c:s:0` not in list / output_streams keeps `eia_608`).

- [ ] **Step 4: Implement**

```python
# src/vidshrink/encode.py
TO_FLAC = {"pcm_bluray", "pcm_dvd"}
TO_SRT = {"mov_text", "eia_608"}          # text-ish subtitles Matroska can't store as-is
DROPPED_SUBS = {"arib_caption"}
CONVERTED = {**{c: "subrip" for c in TO_SRT}, **{c: "flac" for c in TO_FLAC}}
```
and in `_output_args`, replace `if codec == "mov_text":` with `if codec in TO_SRT:`.

- [ ] **Step 5: Run the encode/verify/nvenc tests**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_encode.py tests/test_verify.py tests/test_encode_nvenc.py`
Expected: all pass (the existing `mov_text` tests still hold since `mov_text ∈ TO_SRT`).

- [ ] **Step 6: Commit**

```bash
git add src/vidshrink/encode.py tests/test_encode.py
git commit -m "fix(encode): convert EIA-608 closed captions to SRT instead of failing the MKV header"
```

---

### Task 2: Drop subtitle streams whose codec ffprobe can't name — F2

**Files:**
- Modify: `src/vidshrink/encode.py` (`DROPPED_SUBS`)
- Test: `tests/test_encode.py`

**Interfaces:**
- Consumes/produces: same `DROPPED_SUBS` set as Task 1; `output_streams` drops `["subtitle", "", lang]`.

- [ ] **Step 1: Check what the stream is (read-only; the MKV header is at the file start, so this reads a few MB)**

```bash
cd /tmp && rclone serve http gdrive: --addr 127.0.0.1:43997 --read-only --tpslimit 10 \
  --buffer-size 0 --vfs-read-chunk-size 1M --vfs-read-chunk-size-limit 1M & SP=$!; sleep 3
U="http://127.0.0.1:43997/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' \
  "TV/<show>/Season 1/<show> - S01E11 - <title> SDTV.mkv")"
timeout 60 ffprobe -v error -select_streams s -show_entries stream=index,codec_name,codec_tag_string,codec_type:stream_tags -of compact "$U"
kill $SP
```
Expected: a subtitle stream with no `codec_name`. (Earlier, a full `-show_streams` probe of this file kept reading past 300 MB — keep the `timeout 60`; the header alone is enough.) If the stream turns out to have a real codec MKV supports, record that in the ledger and skip the implementation steps.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_encode.py
def test_unknown_codec_subtitle_is_dropped(cfg, tmp_path):
    probe = replace(SDR, streams=[["audio", "ac3", "eng"], ["subtitle", "", "eng"],
                                  ["subtitle", "subrip", "eng"]])
    cmd = build_command(tmp_path / "s", tmp_path / "o.mkv", probe, cfg)
    assert "-0:s:0" in cmd
    assert output_streams(probe.streams) == [["audio", "ac3", "eng"], ["subtitle", "subrip", "eng"]]
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_encode.py -k unknown_codec`
Expected: FAIL.

- [ ] **Step 4: Implement**

```python
# src/vidshrink/encode.py
# arib_caption copies into a broken file; "" is a stream ffprobe couldn't identify,
# which the muxer can't store either.
DROPPED_SUBS = {"arib_caption", ""}
```

- [ ] **Step 5: Run the encode/verify tests**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_encode.py tests/test_verify.py`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/vidshrink/encode.py tests/test_encode.py
git commit -m "fix(encode): drop subtitle streams with no identifiable codec"
```

---

### Task 3: An interruption costs an attempt only the second time in a row — F3

**Files:**
- Modify: `src/vidshrink/worker.py` (`recover`)
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `Worker._fail`, `Worker._cleanup`, `db.mark`, the `reason` column.
- Produces: `recover()` behaviour: first interruption → `queued`, attempts unchanged, `reason="interrupted during <stage>"`; an interruption of a row whose `reason` already starts with `"interrupted during"` → `_fail` as today. A normal start of work (`prepare`) clears an old interruption reason so only *consecutive* interruptions count.

Why not simply skip the attempt: a file that crashes the machine every time (OOM, kernel bug in a decoder) must still get parked. Consecutive interruptions of the same file is the signal for that.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recover.py
INTERRUPTED = "interrupted during encoding"


def test_first_interruption_is_free(conn, cfg, setup):
    root, worker, row = setup
    vid = row("e.mkv", "encoding")
    (cfg.workdir / f"{vid}.src").write_bytes(b"partial")
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"], r["reason"]) == ("queued", 0, INTERRUPTED)
    assert not (cfg.workdir / f"{vid}.src").exists()


def test_second_interruption_in_a_row_burns_an_attempt(conn, cfg, setup):
    root, worker, row = setup
    vid = row("e.mkv", "encoding", reason=INTERRUPTED)
    worker.recover()
    r = db.get(conn, vid)
    assert (r["stage"], r["attempts"]) == ("queued", 1)
    db.mark(conn, vid, "verifying")        # reason still says interrupted
    worker.recover()
    assert db.get(conn, vid)["attempts"] == 2
    assert db.get(conn, vid)["stage"] == "failed"


def test_starting_work_clears_an_old_interruption(conn, cfg, setup):
    root, worker, row = setup
    (root / "e.mkv").write_bytes(b"x" * 1000)
    vid = row("e.mkv", "queued", reason=INTERRUPTED)
    # Make the download step mark the row and then stop: rclone.download is replaced
    # by a stub that raises, so prepare() fails right after marking 'downloading'.
    worker.rclone.download = lambda *a, **k: (_ for _ in ()).throw(RcloneError(1, "boom"))
    worker.prepare(db.get(conn, vid))
    assert db.get(conn, vid)["reason"] != INTERRUPTED
```
Delete the now-contradicted `test_interrupted_encode_requeues_then_fails` (its first assertion expected `attempts == 1` after a single interruption); the two new tests cover both halves of its intent.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_recover.py -k "interruption"`
Expected: FAIL (`attempts == 1`, reason not set).

- [ ] **Step 3: Implement**

```python
# src/vidshrink/worker.py
INTERRUPTED_PREFIX = "interrupted during "

    def recover(self, dry_run: bool = False) -> int:
        for row in db.rows_in(self.conn, "downloading", "encoding", "verifying"):
            reason = f"{INTERRUPTED_PREFIX}{row['stage']}"
            if (row["reason"] or "").startswith(INTERRUPTED_PREFIX):
                # Second interruption in a row for this file: it may be what's
                # crashing us, so it costs an attempt like any other failure.
                self._fail(row, reason)
            else:
                # A reboot or restart isn't this file's fault: requeue for free, but
                # remember it so a repeat does count.
                self._cleanup(row["id"])
                db.mark(self.conn, row["id"], "queued", reason=reason)
                self.log(f"queued: {row['path']}: {reason} (first time, no attempt used)")
        ...  # rest unchanged
```
In `prepare()`, where the row is switched to `downloading`, also clear the marker: `db.mark_if(self.conn, vid, ("queued",), "downloading", encoder="x265", reason=None)`.

- [ ] **Step 4: Run the recover and worker tests**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_recover.py tests/test_worker.py`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/worker.py tests/test_recover.py
git commit -m "fix(worker): a restart no longer costs an attempt unless it happens twice in a row"
```

---

### Task 4: A gone file that reappears is re-probed — F4

**Files:**
- Modify: `src/vidshrink/db.py` (`upsert_listing`)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `upsert_listing` resets a `gone` row to `unprobed` (probe/score/reason/attempts cleared, size/modtime refreshed) whenever its path is listed again, changed or not.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py
def test_gone_row_comes_back_when_listed_again(conn):
    db.upsert_listing(conn, "a.mkv", 1, "t")
    db.mark_gone(conn, [])
    assert db.get_by_path(conn, "a.mkv")["stage"] == "gone"
    db.upsert_listing(conn, "a.mkv", 1, "t")          # restored from the trash, unchanged
    r = db.get_by_path(conn, "a.mkv")
    assert (r["stage"], r["probe_json"], r["attempts"]) == ("unprobed", None, 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_db.py -k comes_back`
Expected: FAIL (`stage == "gone"`).

- [ ] **Step 3: Implement**

In `upsert_listing`, after the early returns for `done` / in-progress rows:

```python
    if row["stage"] == "gone" or (row["size"], row["modtime"]) != (size, modtime):
        conn.execute(
            "UPDATE videos SET size=?, modtime=?, stage='unprobed', probe_json=NULL,"
            " score=NULL, reason=NULL, attempts=0, updated_at=? WHERE path=?",
            (size, modtime, now, path),
        )
```

- [ ] **Step 4: Run the db and scanner tests**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_db.py tests/test_scanner.py`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/db.py tests/test_db.py
git commit -m "fix(db): a file marked gone is re-probed when it shows up in a listing again"
```

---

### Task 5: Score PC claim candidates with cached ratios and speeds — F5

**Files:**
- Modify: `src/vidshrink/jobs.py` (`_candidates`)
- Modify: `src/vidshrink/scorer.py` (new helper `encoder_tables`)
- Test: `tests/test_jobs_store.py`

**Interfaces:**
- Produces: `scorer.encoder_tables(conn, encoder) -> tuple[dict[str, float], dict[str, float]]` returning `(ratios_by_family, speeds_by_resolution)` for one encoder; `_candidates` uses it and `scorer.score(...)` directly, so the per-row DB lookups disappear. Ordering is unchanged (same numbers, computed once).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_jobs_store.py
def test_candidates_do_not_query_learned_per_row(store, conn, monkeypatch):
    # Set up 50 queued rows the way the other tests in this file do (reuse their helper).
    for i in range(50):
        add(conn, f"Movies/m{i}.mkv", size=10_000_000_000)   # this module's existing helper
    calls = []
    orig = db.learned_mean
    monkeypatch.setattr(db, "learned_mean", lambda *a, **k: calls.append(1) or orig(*a, **k))
    rows = store._candidates("nvenc")
    assert len(rows) == 50
    assert len(calls) <= len(scorer.DEFAULT_RATIO) + len(scorer.DEFAULT_SPEED)   # tables, not per row
```
(`add(conn, path, size=..., probe=BASE, score=1.0)` and the `store` fixture already exist in `tests/test_jobs_store.py`; don't add a second helper.)

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_jobs_store.py -k per_row`
Expected: FAIL (≈100 calls).

- [ ] **Step 3: Implement**

```python
# src/vidshrink/scorer.py
def encoder_tables(conn, encoder: str) -> tuple[dict[str, float], dict[str, float]]:
    """Learned ratio per codec family and speed per resolution class for one encoder,
    looked up once so callers can score many rows without a query per row."""
    return ({f: ratio_for(conn, f, encoder) for f in DEFAULT_RATIO},
            {r: speed_for(conn, r, encoder) for r in DEFAULT_SPEED})
```
```python
# src/vidshrink/jobs.py
    def _candidates(self, encoder: str) -> list:
        ratios, speeds = scorer.encoder_tables(self.conn, encoder)
        scored = []
        for row in db.rows_in(self.conn, "queued"):
            probe = Probe.from_json(row["probe_json"])
            if probe.is_hdr:
                continue
            family = scorer.codec_family(probe.codec)
            res = scorer.resolution_class(probe.width, probe.height)
            scored.append((scorer.score(row["size"], probe, ratios[family], speeds[res]), row))
        scored.sort(key=lambda t: (-t[0], t[1]["id"]))
        return [row for _, row in scored]
```
`encoder_score` stays for other callers/tests.

- [ ] **Step 4: Run the job-store and scorer tests**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_jobs_store.py tests/test_jobs_http.py tests/test_scorer_encoders.py`
Expected: pass; claim order in existing tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/jobs.py src/vidshrink/scorer.py tests/test_jobs_store.py
git commit -m "perf(jobs): score PC claim candidates from cached tables instead of two queries per row"
```

---

### Task 6: List Drive with `--fast-list` — F6

**Files:**
- Modify: `src/vidshrink/rclone.py` (`list_videos`)
- Test: `tests/test_rclone.py`

**Interfaces:**
- Produces: `list_videos` passes `--fast-list`. Same return value. (The local-filesystem backend used in tests accepts and ignores the flag, so the existing listing tests also prove the flag is harmless there.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rclone.py
def test_list_videos_uses_fast_list(rc, monkeypatch):
    seen = {}
    def fake_run(*args, **kwargs):
        seen["args"] = args
        return "[]"
    monkeypatch.setattr(rc, "_run", fake_run)
    rc.list_videos(["mkv"])
    assert "--fast-list" in seen["args"] and "-R" in seen["args"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_rclone.py -k fast_list`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
    def list_videos(self, extensions: Sequence[str]) -> list[dict]:
        pattern = "*.{" + ",".join(extensions) + "}"
        # --fast-list: one recursive query instead of a request per folder; on Drive
        # this turned a ~20 min listing into ~75 s (108k files, 2026-10-18).
        out = self._run("lsjson", "-R", "--fast-list", "--files-only", "--no-mimetype",
                        "--ignore-case", "--include", pattern, self.remote)
```

- [ ] **Step 4: Run the rclone and scanner tests**

Run: `cd ~/vidshrink && .venv/bin/python -m pytest -q tests/test_rclone.py tests/test_scanner.py`
Expected: pass (including `test_list_videos` and `test_list_videos_ignores_vidshrink_tmp` against the real local rclone, proving the flag is accepted).

- [ ] **Step 5: Commit**

```bash
git add src/vidshrink/rclone.py tests/test_rclone.py
git commit -m "perf(scan): list Drive with --fast-list (~20 min -> ~75 s)"
```

---

### Task 7: Full suite, rollout notes

- [ ] **Step 1: Run the whole suite once, alone**

Run: `cd ~/vidshrink && pgrep -fa pytest || .venv/bin/python -m pytest -q`
Expected: all pass (≈300 tests, ~15 min while the server is encoding).

- [ ] **Step 2: Rollout (operator steps, not code)**

1. `systemctl --user restart vidshrink-serve` (Task 5 only touches the job service; safe any time).
2. Restart `vidshrink.service` between jobs: wait for the current `done:`/`failed:` line in `journalctl --user -u vidshrink -f`, then `systemctl --user restart vidshrink`. With Task 3 in place, a restart mid-encode no longer costs an attempt, but it still wastes the encode so far.
3. On the PC: `git pull`, then restart the tray (Tasks 1–2 change the shared command builder; the PC needs them too).
4. Nothing to requeue: the 35 `eia_608` and 26 unknown-subtitle files are still `queued`; they simply stop failing.
