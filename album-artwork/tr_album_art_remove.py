#!/usr/bin/env python3
"""
tr_album_art_remove.py

Bulk-remove sidecars created by tr_album_art_sidecars.py. Two modes:

  (default)  manifest mode -- delete exactly the files listed in the
             JSONL manifest, but ONLY if the on-disk sha256 still
             matches what was recorded. A replaced/edited cover is
             left alone and reported. Safe by construction.

  --scan ROOT  manifest-independent -- walk ROOT and delete any
               cover.* whose bytes contain the "tr_cover_sidecars"
               marker. Use if the manifest is lost.

Dry run by default. --apply performs deletions.
"""

import argparse
import hashlib
import json
import os

MARKER = b"tr_cover_sidecars"
COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png"}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_mode(args):
    kept = removed = missing = changed = 0
    with open(args.manifest) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            path, want = e["path"], e.get("sha256")
            if not os.path.exists(path):
                missing += 1
                print("GONE   %s" % path)
                continue
            if want and sha256_file(path) != want:
                changed += 1
                print("KEEP   %s (sha changed -- replaced since generation)" % path)
                continue
            removed += 1
            print("%s %s" % ("DEL   " if args.apply else "WOULD ", path))
            if args.apply:
                os.remove(path)
    print("\n[%s] to_remove=%d  changed_kept=%d  already_gone=%d"
          % ("APPLY" if args.apply else "DRY-RUN", removed, changed, missing))


def scan_mode(args):
    root = args.scan
    hits = 0
    for dirpath, dirnames, filenames in os.walk(root):
        base = os.path.basename(dirpath)
        if base.startswith("@") or base.startswith("."):
            dirnames[:] = []
            continue
        for fn in filenames:
            if fn.lower() not in COVER_NAMES:
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except Exception:
                continue
            if MARKER in data:
                hits += 1
                print("%s %s" % ("DEL   " if args.apply else "WOULD ", p))
                if args.apply:
                    os.remove(p)
    print("\n[%s | scan] marked_covers=%d" % ("APPLY" if args.apply else "DRY-RUN", hits))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.expanduser("~/tr_album_art_manifest.jsonl"))
    ap.add_argument("--scan", default="", help="manifest-independent: walk this root and remove marked covers")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()
    if args.scan:
        scan_mode(args)
    else:
        manifest_mode(args)


if __name__ == "__main__":
    main()
