#!/usr/bin/env python3
"""
tr_flickr_export_organiser.py - Organise a Flickr export into album folders and write XMP
sidecars, WITHOUT touching the original image files.

The Flickr export is two parts: the original images (EXIF intact - left untouched), and a
JSON metadata dump (albums.json + one photo_<id>.json per photo). This:

  1. MOVES each image into a date-prefixed album folder, matching your raw-export naming:
       "YYYY_MM_DD - Album Name"   (date = earliest date_taken in that album)
     Album membership comes from albums.json; image / json / album lists join on the numeric
     Flickr photo id embedded in the filename (..._<id>_o.jpg).
     For files NOT in any album AND with no photo json, a slug fallback parses a trailing
     "-DDMMYY" date in the filename (e.g. caribou-...-glasgow-211110_<id>_o.jpg) to build a
     "YYYY_MM_DD - Slug Title" folder. Only genuinely un-parseable files go to "_ungrouped".

  2. Writes an XMP SIDECAR next to each image ("<image-filename>.xmp"), so the ORIGINAL FILE
     IS NEVER MODIFIED. Every sidecar carries, in dc:subject (keywords, Immich-visible):
       - src_flickr_2026_06       flat provenance tag (on EVERY Flickr asset)
       - flickr/<Album Name>      nested grouping tag, mirroring the G/<album> and F/<album>
                                  scheme (album-matched + slug-fallback assets only)
     When a photo_<id>.json exists, the sidecar also carries:
       - dc:description  = Flickr description + a one-line summary (faves/views/comments)
       - dc:subject      += the photo's own Flickr tags
       - flickr:* custom namespace = full structured archive: faves, views, comment threads
         (id/date/user/text), photopage url -> preserved losslessly, not displayed by Immich.
     EXIF (date, GPS, camera) is left to the original file; the sidecar doesn't duplicate it.

Originals are only MOVED, never edited. Re-downloadable from the zips if needed.

After running: point an Immich external library at the organised tree, rescan (picks up the
sidecars -> tags + descriptions), then use a folder-to-album tool (e.g. Salvoxia
immich-folder-album-creator) to turn the album folders into Immich albums.

Options:
  --images DIR    (required) dir holding the original Flickr images (searched recursively)
  --json DIR      (required) dir holding albums.json + photo_<id>.json
  --dest DIR      destination root for the organised tree (default: --images, in place)
  --src-tag TAG   flat provenance tag (default: src_flickr_2026_06)
  --tag-parent P  nested grouping-tag parent (default: flickr) -> "flickr/<album>"
  --no-slug-fallback   disable date-slug grouping for no-json files (-> _ungrouped instead)
  --no-date-prefix     name folders just "Album Name" (default: "YYYY_MM_DD - Album Name")
  --dry-run       show what would move / be written, change nothing (DEFAULT)
  --yes           actually move files and write sidecars
  --limit N       only process first N images (for testing)

Usage:
  # preview (default):
  python3 tr_flickr_export_organiser.py \
      --images /volume1/photos/_raw_exports_flickr \
      --json   /volume1/photos/_raw_exports_flickr/_json_flickr_2026_06
  # apply:
  python3 tr_flickr_export_organiser.py --images ... --json ... --yes

Requirements: Python 3.9+ (standard library only). No exiftool - originals untouched.
"""

import argparse
import datetime
import html
import json
import re
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ID_RE = re.compile(r'(\d{6,})')          # Flickr photo id within a filename
# trailing "...-DDMMYY_<id>_o.ext" date stamp used by no-json set files
SLUG_DATE_RE = re.compile(r'^(?P<slug>.+?)-(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{2})_\d{6,}_o\.[^.]+$')
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".heic",
            ".mp4", ".mov", ".m4v", ".webp"}
ALBUM_NAME_MAXLEN = 120


def load_albums(json_dir):
    """Return {photo_id: album_title} and {album_title: [photo_ids]} from albums.json."""
    p = Path(json_dir) / "albums.json"
    if not p.exists():
        sys.exit(f"FATAL: albums.json not found in {json_dir}")
    data = json.load(open(p, encoding="utf-8"))
    albums = data.get("albums", data) if isinstance(data, dict) else data
    pid_to_album, album_to_pids = {}, {}
    for a in albums:
        title = (a.get("title") or "").strip()
        pids = [x for x in a.get("photos", []) if x and x != "0"]
        album_to_pids[title] = pids
        for pid in pids:
            pid_to_album.setdefault(pid, title)   # first album wins for placement
    return pid_to_album, album_to_pids


def load_photo_json(json_dir, pid):
    p = Path(json_dir) / f"photo_{pid}.json"
    if not p.exists():
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def album_earliest_date(json_dir, pids):
    best = None
    for pid in pids:
        d = load_photo_json(json_dir, pid)
        if not d:
            continue
        dt = parse_date(d.get("date_taken"))
        if dt and (best is None or dt < best):
            best = dt
    return best


def titlecase_slug(slug):
    """'caribou-...-glasgow' -> 'Caribou ... Glasgow' (hyphens to spaces, title-cased)."""
    words = [w for w in slug.replace("_", "-").split("-") if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def slug_fallback(filename):
    """For no-album/no-json files: parse trailing -DDMMYY date stamp.
    Returns (album_title, datetime) or (None, None)."""
    m = SLUG_DATE_RE.match(filename)
    if not m:
        return None, None
    yy = int(m.group("y"))
    year = 2000 + yy if yy <= 68 else 1900 + yy
    try:
        dt = datetime.datetime(year, int(m.group("m")), int(m.group("d")))
    except ValueError:
        return None, None
    return titlecase_slug(m.group("slug")), dt


def sanitise_folder(name, fallback):
    if not name:
        return fallback
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    name = name.strip(". ")
    return name[:ALBUM_NAME_MAXLEN] or fallback


def folder_for(album_title, earliest, date_prefix, fallback):
    base = sanitise_folder(album_title, fallback)
    if date_prefix and earliest:
        return f"{earliest.strftime('%Y_%m_%d')} - {base}"
    return base


def build_xmp(meta, keywords):
    """Build an XMP sidecar. `keywords` (list) always written to dc:subject.
    `meta` (photo_<id>.json dict or None) adds description + flickr:* archive when present.
    Original file is never touched."""
    desc = faves = views = photopage = ""
    comments, photo_tags = [], []
    if meta is not None:
        desc = (meta.get("description") or "").strip()
        faves = meta.get("count_faves", "0")
        views = meta.get("count_views", "0")
        comments = meta.get("comments") or []
        photo_tags = [t.get("tag") for t in (meta.get("tags") or []) if t.get("tag")]
        photopage = meta.get("photopage", "")

    # visible description = Flickr desc + one-line social summary
    visible_desc = ""
    if meta is not None:
        bits = []
        if faves and faves != "0":
            bits.append(f"{faves} faves")
        if views and views != "0":
            bits.append(f"{views} views")
        if comments:
            bits.append(f"{len(comments)} comments")
        summary = ", ".join(bits)
        if desc and summary:
            visible_desc = f"{desc} \u2014 Flickr: {summary}"
        elif summary:
            visible_desc = f"Flickr: {summary}"
        else:
            visible_desc = desc

    # dc:subject = provenance + grouping keywords + the photo's own Flickr tags
    all_kw = list(keywords) + list(photo_tags)
    subj = ""
    if all_kw:
        items = "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in all_kw)
        subj = f'<dc:subject><rdf:Bag>{items}</rdf:Bag></dc:subject>'

    desc_block = ""
    if visible_desc:
        desc_block = ('<dc:description><rdf:Alt>'
                      f'<rdf:li xml:lang="x-default">{escape(visible_desc)}</rdf:li>'
                      '</rdf:Alt></dc:description>')

    flickr_block = ""
    if meta is not None:
        cmt_xml = ""
        for c in comments:
            cmt_xml += (
                "<flickr:comment>"
                f"<flickr:cDate>{escape(c.get('date',''))}</flickr:cDate>"
                f"<flickr:cUser>{escape(c.get('user',''))}</flickr:cUser>"
                f"<flickr:cText>{escape(html.unescape(c.get('comment','')))}</flickr:cText>"
                "</flickr:comment>"
            )
        flickr_block = (
            f'<flickr:faves>{escape(str(faves))}</flickr:faves>'
            f'<flickr:views>{escape(str(views))}</flickr:views>'
            f'<flickr:photopage>{escape(photopage)}</flickr:photopage>'
            f'<flickr:comments>{cmt_xml}</flickr:comments>'
        )

    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"\n'
        '  xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '  xmlns:flickr="https://triff.co/ns/flickr/1.0/">\n'
        '<rdf:Description rdf:about="">\n'
        f'{desc_block}{subj}{flickr_block}\n'
        '</rdf:Description>\n'
        '</rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>\n'
    )


def unique_path(p):
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def main():
    ap = argparse.ArgumentParser(
        description="Organise Flickr export into album folders + XMP sidecars (originals untouched).")
    ap.add_argument("--images", required=True, help="Dir with original Flickr images (recursive)")
    ap.add_argument("--json", required=True, help="Dir with albums.json + photo_<id>.json")
    ap.add_argument("--dest", default=None, help="Destination root (default: --images, in place)")
    ap.add_argument("--src-tag", default="src_flickr_2026_06",
                    help="Flat provenance tag (default: src_flickr_2026_06)")
    ap.add_argument("--tag-parent", default="flickr",
                    help='Nested grouping-tag parent (default: flickr -> "flickr/<album>")')
    ap.add_argument("--no-slug-fallback", action="store_true",
                    help="Disable date-slug grouping for no-json files")
    ap.add_argument("--rename", action="store_true",
                    help='Rename album-matched files to "<id>_o.ext" (folder gives the what/when; '
                         "slug-fallback and _ungrouped names are left intact)")
    ap.add_argument("--no-date-prefix", action="store_true",
                    help='Folders named just "Album Name" (default date-prefixed)')
    ap.add_argument("--dry-run", action="store_true", help="Preview only (default if no --yes)")
    ap.add_argument("--yes", action="store_true", help="Actually move files and write sidecars")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N images")
    args = ap.parse_args()

    apply = args.yes and not args.dry_run
    date_prefix = not args.no_date_prefix
    use_slug = not args.no_slug_fallback
    parent = args.tag_parent.strip().rstrip("/")
    images = Path(args.images).expanduser().resolve()
    json_dir = Path(args.json).expanduser().resolve()
    dest = Path(args.dest).expanduser().resolve() if args.dest else images
    if not images.is_dir():
        sys.exit(f"FATAL: --images not a dir: {images}")
    if not json_dir.is_dir():
        sys.exit(f"FATAL: --json not a dir: {json_dir}")

    pid_to_album, album_to_pids = load_albums(json_dir)
    print(f"albums.json: {len(album_to_pids)} albums, "
          f"{len(pid_to_album)} album-member photo ids")

    album_earliest = {}
    if date_prefix:
        print("Computing earliest date per album (for folder prefixes)...")
        for title, pids in album_to_pids.items():
            album_earliest[title] = album_earliest_date(json_dir, pids)

    imgs = [p for p in images.rglob("*")
            if p.is_file() and p.suffix.lower() in IMG_EXTS
            and json_dir not in p.parents and p.parent != json_dir]
    imgs.sort()
    if args.limit:
        imgs = imgs[:args.limit]
    print(f"Images found: {len(imgs)}\n")

    moved = ungrouped = no_json = sidecars = slug_grouped = 0
    seen_albums = set()
    for img in imgs:
        m = ID_RE.search(img.stem)
        pid = m.group(1) if m else None
        meta = load_photo_json(json_dir, pid) if pid else None
        album = pid_to_album.get(pid) if pid else None
        album_title = None       # the human album name, for the nested tag
        from_album = False       # True only for real albums.json matches (rename-eligible)

        if album:
            album_title = album
            from_album = True
            folder = folder_for(album, album_earliest.get(album), date_prefix, "_ungrouped")
        elif use_slug:
            slug_title, slug_dt = slug_fallback(img.name)
            if slug_title:
                album_title = slug_title
                folder = folder_for(slug_title, slug_dt, date_prefix, "_ungrouped")
                slug_grouped += 1
            else:
                folder = "_ungrouped"
                ungrouped += 1
        else:
            folder = "_ungrouped"
            ungrouped += 1

        if album_title:
            seen_albums.add(folder)
        if meta is None:
            no_json += 1

        # keywords: flat provenance always; nested grouping when we have an album name
        keywords = [args.src_tag]
        if album_title:
            keywords.append(f"{parent}/{album_title}")

        # rename only album-matched files (slug-fallback names are their only metadata)
        out_name = img.name
        if args.rename and from_album and pid:
            out_name = f"{pid}_o{img.suffix.lower()}"

        target_dir = dest / folder
        target = target_dir / out_name

        if apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = unique_path(target)
            shutil.move(str(img), str(target))
            moved += 1
            xmp = build_xmp(meta, keywords)          # sidecar for EVERY file
            (target.parent / (target.name + ".xmp")).write_text(xmp, encoding="utf-8")
            sidecars += 1
        else:
            note = "" if meta is not None else "  (no photo json)"
            kw = ", ".join(keywords)
            shown = f"{img.name} -> {out_name}" if out_name != img.name else img.name
            print(f"  {shown} -> {folder}/   [{kw}]{note}")

    print("\n" + "=" * 60)
    if apply:
        print(f"Moved: {moved}   sidecars: {sidecars}   "
              f"slug-grouped: {slug_grouped}   _ungrouped: {ungrouped}   no json: {no_json}")
        print(f"Album folders created: {len(seen_albums)}")
        print("Next: rescan the Immich external library to pick up sidecars (tags + "
              "descriptions), then run the folder-to-album tool over the album folders.")
    else:
        grouped = len(imgs) - ungrouped
        print(f"DRY RUN. Would move {len(imgs)} images: {grouped} into album folders "
              f"({slug_grouped} via slug fallback), {ungrouped} to _ungrouped "
              f"({no_json} have no photo json). Every file gets a sidecar with '{args.src_tag}'"
              f" + '{parent}/<album>' where grouped. Re-run with --yes to apply.")
    print("=" * 60)


if __name__ == "__main__":
    main()
