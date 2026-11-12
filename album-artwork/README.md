# tr_album_art

Pure-stdlib tools to fill missing album-art sidecars (`cover.jpg`/`cover.png`) across a music library. No dependencies — runs on a bare `python3`. Audio files are never modified. Every written cover carries an in-file marker and a manifest line, so all changes are dry-run-previewable and fully reversible.

Typical order: `scan` → `promote` → `sidecars` → (`fetch`, WIP) → `remove` to undo. All four share `~/tr_album_art_manifest.jsonl`.

## Scripts

**`tr_album_art_scan.py`** — read-only census; buckets every folder as standard / promotable / embedded / bare and writes worklists. Changes nothing.
```
python3 tr_album_art_scan.py /path/to/music --probe-embedded --list-dir ~/lists
```

**`tr_album_art_promote.py`** — for folders that already hold art under a non-standard name (WMP `AlbumArt_*`, `*(front).jpg`, etc.), copies the best image to `cover.jpg`. Front-weighted pick, skips sub-1KB junk. Offline.
```
python3 tr_album_art_promote.py --from-list ~/lists/promotable_folders.txt --apply
```

**`tr_album_art_sidecars.py`** — extracts art embedded in the audio tags (FLAC/MP3/M4A/OGG) and writes it to `cover.jpg`. `--skip-if-any-image` leaves folders that already have any image alone; `--fetch` is an optional online lookup. Offline by default.
```
python3 tr_album_art_sidecars.py /path/to/music --skip-if-any-image --apply
```

**`tr_album_art_remove.py`** — undoes anything the tools wrote. Manifest mode deletes only sha-verified entries (a cover you replaced by hand is left alone); `--scan` finds them by marker if the manifest is lost.
```
python3 tr_album_art_remove.py            # dry run
python3 tr_album_art_remove.py --apply    # delete
python3 tr_album_art_remove.py --scan /path/to/music --apply
```

## Notes

- Defaults to a dry run everywhere; add `--apply` to write.
- `--min-bytes` (default 1024) rejects empty/truncated art.
- Relative paths in worklists resolve against the directory the scan was run from — run subsequent tools from the same place.
