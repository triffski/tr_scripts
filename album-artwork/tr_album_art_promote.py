#!/usr/bin/env python3
"""
tr_album_art_promote.py

Promote an album folder's EXISTING art (under a non-standard name --
WMP AlbumArt_{GUID}_Large.jpg, harry.jpg, ...(front).jpg, etc.) to a
player-standard cover.<ext>. Offline, exact, no network, no re-encode.

Picks the LARGEST image by byte size in each folder (real covers dwarf
WMP *_Small thumbnails), copies it to cover.<ext>, injects the
"tr_cover_sidecars" marker (JPEG COM / PNG tEXt, pixels untouched) and
records it in the shared JSONL manifest -- so it's identifiable and
removable exactly like the generated sidecars. Source image untouched.

Dry run by default. --apply writes. Feed a folder list with --from-list
(e.g. promotable_folders.txt) or walk a root.
"""

import argparse
import hashlib
import json
import os
import re
import struct
import time
import zlib

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wv", ".alac", ".aiff", ".aif"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
STANDARD = {
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "albumart.jpg", "albumart.jpeg", "albumart.png",
}
MARKER = b"tr_cover_sidecars"

PREFER = {"front", "cover", "folder", "packshot", "frontal"}
DEMOTE = {"back", "inlay", "inside", "label", "tray", "spine", "booklet",
          "matrix", "side", "aside", "bside", "obi", "rear"}
TOK_SPLIT = re.compile(r"[\s\-_.()\[\]{}'\u2019#=~+]+")


def name_tier(fn):
    stem = os.path.splitext(fn)[0].lower()
    toks = set(t for t in TOK_SPLIT.split(stem) if t)
    has_pref = bool(toks & PREFER)
    has_dem = bool(toks & DEMOTE)
    if has_pref and not has_dem:
        return 2
    if not has_pref and not has_dem:
        return 1
    return 0


def sniff_format(data):
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return None


def inject_marker(data, ext):
    if ext in ("jpg", "jpeg") and data[:2] == b"\xff\xd8":
        seg = b"\xff\xfe" + struct.pack(">H", len(MARKER) + 2) + MARKER
        return data[:2] + seg + data[2:]
    if ext == "png" and data[:8] == b"\x89PNG\r\n\x1a\n":
        ihdr_end = 8 + 4 + 4 + 13 + 4
        cdata = MARKER + b"\x00" + b"1"
        crc = zlib.crc32(b"tEXt" + cdata) & 0xffffffff
        chunk = struct.pack(">I", len(cdata)) + b"tEXt" + cdata + struct.pack(">I", crc)
        return data[:ihdr_end] + chunk + data[ihdr_end:]
    return data


def atomic_write(dst, data):
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


TIER_LABEL = {2: "front", 1: "neutral", 0: "back?"}


def list_images(dirpath, filenames):
    return [f for f in filenames
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS and f.lower() not in STANDARD]


def pick_image(dirpath, imgs, min_bytes):
    cands = []
    for fn in imgs:
        try:
            sz = os.path.getsize(os.path.join(dirpath, fn))
        except OSError:
            continue
        if sz < min_bytes:
            continue
        cands.append((fn, sz))
    if not cands:
        return None
    best = max(cands, key=lambda c: (name_tier(c[0]), c[1]))
    return best[0]


def iter_target_dirs(args):
    if args.from_list:
        for line in open(args.from_list):
            d = line.strip()
            if d:
                yield d
    else:
        for dirpath, dirnames, filenames in os.walk(args.root):
            base = os.path.basename(dirpath)
            if base.startswith("@") or base.startswith("."):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not (d.startswith("@") or d.startswith("."))]
            yield dirpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="library root (ignored if --from-list given)")
    ap.add_argument("--from-list", default="", help="file of folder paths (e.g. promotable_folders.txt)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-bytes", type=int, default=1024, dest="min_bytes",
                    help="ignore images smaller than this (skips empty/broken art). default 1024")
    ap.add_argument("--log", default=os.path.expanduser("~/tr_album_art.log"))
    ap.add_argument("--manifest", default=os.path.expanduser("~/tr_album_art_manifest.jsonl"))
    args = ap.parse_args()

    logf = open(args.log, "a") if args.apply else None
    mf = open(args.manifest, "a") if args.apply else None
    st = {"folders": 0, "promoted": 0, "skip_no_audio": 0, "skip_standard": 0,
          "skip_no_image": 0, "skip_too_small": 0, "errors": 0}
    run_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    for dirpath in iter_target_dirs(args):
        try:
            filenames = [f for f in os.listdir(dirpath) if os.path.isfile(os.path.join(dirpath, f))]
        except OSError:
            st["errors"] += 1
            continue
        low = [f.lower() for f in filenames]
        if not any(os.path.splitext(n)[1] in AUDIO_EXTS for n in low):
            st["skip_no_audio"] += 1
            continue
        st["folders"] += 1
        if any(n in STANDARD for n in low):
            st["skip_standard"] += 1
            continue
        imgs = list_images(dirpath, filenames)
        if not imgs:
            st["skip_no_image"] += 1
            continue
        chosen = pick_image(dirpath, imgs, args.min_bytes)
        if not chosen:
            st["skip_too_small"] += 1
            print("SKIP    %s  (all images < %dB)" % (dirpath, args.min_bytes))
            continue

        src = os.path.join(dirpath, chosen)
        try:
            with open(src, "rb") as f:
                data = f.read()
            fmt = sniff_format(data)
            if fmt is None:
                fmt = os.path.splitext(chosen)[1].lower().lstrip(".") or "jpg"
            outext = "jpg" if fmt in ("jpg", "jpeg") else fmt
            dst = os.path.join(dirpath, "cover." + outext)
            data = inject_marker(data, outext)
            st["promoted"] += 1
            msg = "PROMOTE %s  (%s [%s] -> cover.%s, %dB)" % (dirpath, chosen, TIER_LABEL[name_tier(chosen)], outext, len(data))
            print(msg)
            if args.apply:
                atomic_write(dst, data)
                logf.write(msg + "\n"); logf.flush()
                mf.write(json.dumps({"ts": run_ts, "phase": "promoted", "path": dst,
                                     "source": chosen, "bytes": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()}) + "\n")
                mf.flush()
        except Exception as e:
            st["errors"] += 1
            print("  ERROR %s: %s" % (dirpath, e))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("\n[%s] candidate folders=%d  promoted=%d  skip(standard)=%d  skip(no image)=%d  skip(too small)=%d  errors=%d"
          % (mode, st["folders"], st["promoted"], st["skip_standard"], st["skip_no_image"], st["skip_too_small"], st["errors"]))
    if logf: logf.close()
    if mf: mf.close()


if __name__ == "__main__":
    main()
