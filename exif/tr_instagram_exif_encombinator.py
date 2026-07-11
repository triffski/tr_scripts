#!/usr/bin/env python3
"""
tr_instagram_exif_encombinator.py - Enrich an Instagram export with embedded metadata,
ready for Immich.

Instagram strips EXIF on upload and keeps the real metadata (post dates, captions, GPS) in
sidecar JSON. This reads the export's media-content JSON and writes that metadata INTO
copies of the photos/videos with exiftool, so immich-go `upload from-folder` and Immich get
correct dates, captions and tags instead of dateless files stamped with today.

Instagram has no albums, but it does separate media by CONTENT TYPE, which this preserves
as a hierarchical tag (the rough equivalent of Facebook's per-album nesting):

Tags written into each file (read by Immich on import):
  - src_instagram_<batch>  flat provenance tag (default src_instagram_2026_06), so the
                           batch is findable/cullable and the source is always knowable.
  - I/<type>               hierarchical content-type tag, nested under an "I" parent, where
                           <type> is one of: posts, archived, reels, igtv, stories.

Content covered (the files that actually reference media), each mapped to its I/<type>:
  posts_*.json        -> I/posts       (your feed posts; sharded posts_1.json, ...)
  archived_posts.json -> I/archived
  reels.json          -> I/reels       (video)
  igtv_videos.json    -> I/igtv        (video)
  stories.json        -> I/stories
Deliberately ignored: posts.json (a label/activity feed with no media), profile_photos,
reposts, other_content.

Also:
  - caption (post or media title) -> description.
  - creation_timestamp -> DateTimeOriginal (photos) / QuickTime CreateDate (video).
  - GPS if present in exif_data (often stripped by IG).
  - Carousels: a post's caption + timestamp are fanned out across all its media.
  - Fixes mislabelled files: media whose extension lies about the content (e.g. a JPEG
    named .webp/.heic) is renamed in the OUTPUT to the real format (magic-number sniff).
  - No albums (Instagram has none; the importer puts everything in one named album).
  - No comments (Instagram's export omits comment threads on your posts).

Input / output:
  --input   READ-ONLY. The folder containing the IG JSON export. Never written/renamed.
  --output  receives enriched COPIES, mirroring the source's media subfolder structure.
            --clean wipes it first.

Options:
  -i, --input DIR     (required) source export dir, READ-ONLY
  -o, --output DIR    destination for enriched copies (required unless --inspect)
  --clean             wipe the output dir before running (fresh rebuild)
  --inspect           print detected schema and exit (no writes)
  --dry-run           print actions, copy/write nothing
  --limit N           only process first N media (0 = all)
  --batch SUFFIX      batch suffix for the flat tag src_instagram_<SUFFIX> (default 2026_06)
  --prefix P          content-type hierarchy parent letter (default "I"); tags as P/<type>
  --source NAME       source name in the flat tag src_<NAME>_<batch> (default "instagram")
  --verbose           print each exiftool command as it runs

Requirements: Python 3.9+, exiftool on PATH (macOS: `brew install exiftool`).

Usage:
    python3 tr_instagram_exif_encombinator.py --input /data/ig_export/json --inspect
    python3 tr_instagram_exif_encombinator.py --input /data/ig_export/json --output /data/out --dry-run --limit 5
    python3 tr_instagram_exif_encombinator.py --input /data/ig_export/json --output /data/out --clean
Then upload (Instagram has no albums, so use one named album):
    immich-go upload from-folder --server=... --api-key=... --into-album "Instagram1" /data/out
"""

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

# ----------------------------------------------------------------------------
# Config / defaults
# ----------------------------------------------------------------------------
DEFAULT_SOURCE = "instagram"
DEFAULT_BATCH = "2026_06"
DEFAULT_PREFIX = "I"
PROGRESS_EVERY = 100

# Content files -> the I/<type> leaf they map to. "posts_*.json" matches the sharded post
# files (posts_1.json, ...) but NOT "posts.json" (a label feed with no media).
CONTENT_MAP = [
    ("**/posts_*.json", "posts"),
    ("**/archived_posts.json", "archived"),
    ("**/reels.json", "reels"),
    ("**/igtv_videos.json", "igtv"),
    ("**/stories.json", "stories"),
]

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
TZ_OFFSET_HOURS = 0


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def fix_mojibake(s):
    if not isinstance(s, str):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  !! could not parse {path}: {e}", file=sys.stderr)
        return None


def dig(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return default
    return d


def ts_to_exif(ts):
    dt = datetime.datetime.utcfromtimestamp(int(ts)) + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def entries_from(data):
    """Content files are either a bare list of items, or a dict wrapping one list
    (reels/igtv/archived/stories). Return the list either way."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def gps_from(media):
    exif = dig(media, "media_metadata", "photo_metadata", "exif_data", default=None) \
        or dig(media, "media_metadata", "video_metadata", "exif_data", default=None) or []
    if exif:
        lat, lon = exif[0].get("latitude"), exif[0].get("longitude")
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            return lat, lon
    return None, None


def detect_export_root(input_dir):
    for d in input_dir.rglob("media"):
        if d.is_dir():
            return d.parent
    return input_dir


def real_image_ext(path):
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
        return ".heic"
    return None


def corrected_output(dst_file, src_file):
    real = real_image_ext(src_file)
    if real and dst_file.suffix.lower() != real:
        return dst_file.with_suffix(real), True
    return dst_file, False


def safe_clean(output, input_dir):
    out, inp = output.resolve(), input_dir.resolve()
    if out == inp:
        sys.exit("FATAL: --clean refused: output equals input.")
    try:
        inp.relative_to(out)
        sys.exit("FATAL: --clean refused: input lives inside output; would delete the source.")
    except ValueError:
        pass
    if len(out.parts) <= 2:
        sys.exit(f"FATAL: --clean refused: {out} is too close to the filesystem root.")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Cleaned output: {out}")


# ----------------------------------------------------------------------------
# exiftool command
# ----------------------------------------------------------------------------
def build_exiftool_cmd(path, ts, lat, lon, description, src_tag, type_leaf, prefix):
    cmd = ["exiftool", "-m", "-overwrite_original", "-charset", "UTF8", "-codedcharacterset=utf8"]
    is_video = path.suffix.lower() in VIDEO_EXTS

    if ts:
        d = ts_to_exif(ts)
        if is_video:
            cmd += ["-api", "QuickTimeUTC=1",
                    f"-QuickTime:CreateDate={d}", f"-QuickTime:ModifyDate={d}",
                    f"-FileModifyDate={d}"]
        else:
            cmd += [f"-DateTimeOriginal={d}", f"-CreateDate={d}",
                    f"-ModifyDate={d}", f"-FileModifyDate={d}"]

    if description:
        if is_video:
            cmd += [f"-QuickTime:Description={description}", f"-XMP-dc:Description={description}"]
        else:
            cmd += [f"-EXIF:ImageDescription={description}",
                    f"-XMP-dc:Description={description}",
                    f"-IPTC:Caption-Abstract={description}"]

    # Flat provenance tag.
    cmd += [f"-IPTC:Keywords+={src_tag}", f"-XMP-dc:Subject+={src_tag}"]
    # Hierarchical content-type tag: I/<type>.
    if type_leaf:
        cmd += [f"-XMP-digiKam:TagsList+={prefix}/{type_leaf}"]

    if lat is not None and lon is not None:
        cmd += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]

    cmd.append(str(path))
    return cmd


# ----------------------------------------------------------------------------
# Discovery: collect content files with their type leaf
# ----------------------------------------------------------------------------
def find_content(input_dir):
    """Return list of (path, type_leaf), deduped, for every matched content file."""
    seen, found = set(), []
    for glob, leaf in CONTENT_MAP:
        for p in input_dir.glob(glob):
            if p.is_file() and p not in seen:
                seen.add(p)
                found.append((p, leaf))
    return sorted(found, key=lambda t: str(t[0]))


def collect_entries(content_files):
    """Pass 1: load every content file once. Returns (entries, failures) where each entry
    is (entry_dict, type_leaf) so the content type travels with the data."""
    entries, failures = [], 0
    for cf, leaf in content_files:
        data = load_json(cf)
        if data is None:
            failures += 1
            continue
        for e in entries_from(data):
            entries.append((e, leaf))
    return entries, failures


# ----------------------------------------------------------------------------
# Inspect
# ----------------------------------------------------------------------------
def inspect(input_dir, export_root, content_files, src_tag, prefix):
    print(f"\nInput:                {input_dir}")
    print(f"Detected export root: {export_root}")
    print(f"Tags per photo:       {src_tag}  +  {prefix}/<type>")
    print(f"content files found:  {len(content_files)}")
    for p, leaf in content_files:
        print(f"  - {p.relative_to(input_dir)}   -> {prefix}/{leaf}")
    if not content_files:
        print("\n  No content files matched CONTENT_MAP - check the patterns at the top.")
        return

    cf, leaf = content_files[0]
    entries = entries_from(load_json(cf))
    print(f"\nEntries in {cf.name}: {len(entries)}  (type {prefix}/{leaf})")
    if entries:
        e0 = entries[0]
        media = e0.get("media") or []
        if media:
            uri = media[0].get("uri")
            resolved = export_root / uri if uri else None
            cap = fix_mojibake(e0.get("title") or media[0].get("title") or "")
            ts = e0.get("creation_timestamp") or media[0].get("creation_timestamp")
            lat, lon = gps_from(media[0])
            print("\nWhat would be extracted from entry #1, media #0:")
            print(f"  uri:        {uri}")
            print(f"  resolves:   {resolved}  exists={resolved.exists() if resolved else False}")
            print(f"  timestamp:  {ts} -> {ts_to_exif(ts) if ts else '(none)'}")
            print(f"  caption:    {cap!r}")
            print(f"  gps:        {lat}, {lon}")
            print(f"  tags:       {src_tag}, {prefix}/{leaf}")
    print("\nIf that looks right, drop --inspect and run --dry-run --limit 5.\n")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Embed Instagram JSON metadata + source/type tags into photo copies.")
    ap.add_argument("-i", "--input", required=True,
                    help="Source export dir (READ-ONLY), the folder containing the IG JSON export")
    ap.add_argument("-o", "--output",
                    help="Destination dir for enriched copies (required unless --inspect)")
    ap.add_argument("--clean", action="store_true",
                    help="Wipe the output dir before running, for a fresh rebuild")
    ap.add_argument("--inspect", action="store_true", help="Print detected schema and exit")
    ap.add_argument("--dry-run", action="store_true", help="Print actions, copy/write nothing")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N media (0 = all)")
    ap.add_argument("--batch", default=DEFAULT_BATCH,
                    help=f"Batch suffix for the flat source tag (default {DEFAULT_BATCH})")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help=f'Content-type hierarchy parent letter (default "{DEFAULT_PREFIX}")')
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help=f'Source name in src_<source>_<batch> (default "{DEFAULT_SOURCE}")')
    ap.add_argument("--verbose", action="store_true", help="Print each command as it runs")
    args = ap.parse_args()

    src_tag = f"src_{args.source}_{args.batch}"

    input_dir = Path(args.input).expanduser().resolve()
    if not input_dir.is_dir():
        sys.exit(f"FATAL: input not found: {input_dir}")

    if not args.inspect and not args.output:
        sys.exit("FATAL: --output is required (except with --inspect).")
    output = Path(args.output).expanduser().resolve() if args.output else None

    if not args.dry_run and not args.inspect and not shutil.which("exiftool"):
        sys.exit("FATAL: exiftool not found on PATH. Install with: brew install exiftool")

    export_root = detect_export_root(input_dir)
    content_files = find_content(input_dir)

    if args.inspect:
        inspect(input_dir, export_root, content_files, src_tag, args.prefix)
        return

    if not content_files:
        sys.exit("FATAL: no content files found. Run --inspect and check CONTENT_MAP.")

    entries, failed = collect_entries(content_files)
    total_media = sum(len(e.get("media") or []) for e, _ in entries)
    display_total = min(args.limit, total_media) if args.limit else total_media
    print(f"Found {total_media} media across {len(entries)} entries "
          f"in {len(content_files)} content files"
          + (f" (processing first {display_total})" if args.limit else ""))
    print(f"Tags per photo: {src_tag} + {args.prefix}/<type>\n")

    if args.clean and not args.dry_run:
        safe_clean(output, input_dir)

    ok = skipped = renamed = processed = 0
    for entry, leaf in entries:
        caption = fix_mojibake(entry.get("title") or "")
        entry_ts = entry.get("creation_timestamp")

        for m in (entry.get("media") or []):
            if args.limit and processed >= args.limit:
                break
            processed += 1

            uri = m.get("uri")
            if not uri:
                skipped += 1
            else:
                src_file = export_root / uri
                if not src_file.exists():
                    print(f"  !! missing source: {uri}", file=sys.stderr)
                    skipped += 1
                else:
                    ts = entry_ts or m.get("creation_timestamp")
                    cap = caption or fix_mojibake(m.get("title") or "")
                    lat, lon = gps_from(m)

                    dst_file = output / uri
                    final_dst, did_rename = corrected_output(dst_file, src_file)
                    cmd = build_exiftool_cmd(final_dst, ts, lat, lon, cap, src_tag, leaf, args.prefix)

                    if args.dry_run or args.verbose:
                        tag = f"  (rename -> {final_dst.suffix})" if did_rename else ""
                        print(f"  [{args.prefix}/{leaf}] {uri} -> {final_dst.name}{tag}")
                        print("    " + " ".join(repr(a) if (" " in a or "\n" in a) else a for a in cmd))

                    if args.dry_run:
                        ok += 1
                        if did_rename:
                            renamed += 1
                    else:
                        final_dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, final_dst)
                        if did_rename:
                            renamed += 1
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        if res.returncode == 0:
                            ok += 1
                        else:
                            failed += 1
                            print(f"  !! exiftool failed on {uri}: {res.stderr.strip()}",
                                  file=sys.stderr)

            if processed % PROGRESS_EVERY == 0:
                print(f"Processed: {processed} of {display_total} files", flush=True)

        if args.limit and processed >= args.limit:
            print("\n(reached --limit)")
            break

    _summary(output, ok, skipped, failed, renamed, src_tag)
    if failed:
        sys.exit(1)


def _summary(output, ok, skipped, failed, renamed, src_tag):
    print(f"\n{'='*52}")
    print(f"Done.  written/ok: {ok}   skipped: {skipped}   failed: {failed}   "
          f"renamed to real ext: {renamed}")
    print(f"       flat tag: {src_tag}   (+ per-type I/<type> tags)")
    print(f"{'='*52}")
    print("Next:  immich-go upload from-folder --server=... --api-key=... "
          f'--into-album "Instagram1"  {output}')


if __name__ == "__main__":
    main()
