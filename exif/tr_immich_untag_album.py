#!/usr/bin/env python3
"""
tr_immich_album_untag.py - Remove one or more tags from the assets of a named Immich album.

The inverse of tr_immich_source_tagger.py. Use it to undo a mis-tag, e.g. when the source
tagger swept an album it shouldn't have (a phone "Camera" album tagged src_google_*).

What it does:
  GET  /api/albums                      find the album(s) whose name matches --album-name
  GET  /api/albums/{id}                 that album's asset ids
  GET  /api/tags                         resolve each --tag value to its tag id
  DELETE /api/tags/{tagId}/assets        bulk-remove the tag from those assets ({"ids":[...]})

Removing a tag from assets does NOT delete the photos or the tag itself - it just detaches
the tag from those assets. A tag left attached to nothing becomes an empty/leaf tag you can
delete separately (Tags view, or it's harmless to leave).

IMPORTANT cross-membership caveat:
  If an asset in this album is ALSO a member of another album that legitimately carries the
  tag (e.g. a phone photo that deduped against its Google Takeout copy, so one asset sits in
  both "Camera" and a real Google album), this will strip the tag from that shared asset too.
  Fix: after untagging, re-run tr_immich_source_tagger.py over the LEGITIMATE albums (with
  --exclude-album for this one) to re-add the tag to genuine members. Tagging is idempotent,
  so that cleanly restores any shared asset without touching the ones you meant to clear.

API key resolution: --api-key if given, else the IMMICH_API_KEY env var. Pass --api-key
explicitly to act on a different account than the env var's key (the Memories trap).

Options:
  --server URL        (required) Immich base URL, e.g. http://localhost:2283
  --api-key KEY       API key. Falls back to IMMICH_API_KEY env var if omitted.
  --album-name NAME   (required) exact album name to scope to (e.g. "Camera"). If several
                      albums share the name, all matching albums are included.
  --tag VALUE         (required, repeatable) tag value to remove, e.g. src_google_2026_06
                      or "G/Camera". Give multiple --tag flags to remove several at once.
  --yes               Actually remove the tags. Without it, dry-run (lists only).

Usage:
  # preview removing two tags from the Camera album's assets:
  python3 tr_immich_album_untag.py --server http://localhost:2283 \
      --album-name "Camera" --tag src_google_2026_06 --tag "G/Camera"
  # apply:
  python3 tr_immich_album_untag.py --server http://localhost:2283 \
      --album-name "Camera" --tag src_google_2026_06 --tag "G/Camera" --yes

Requirements: Python 3.9+ (standard library only).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BATCH = 500          # assets per bulk untag call


def api(server, api_key, path, method="GET", body=None):
    url = server.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", api_key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"FATAL: cannot reach {url}: {e.reason}")


def album_asset_ids(server, key, album_id):
    st, info = api(server, key, f"/api/albums/{album_id}")
    if st != 200 or not isinstance(info, dict):
        return None
    return [a["id"] for a in info.get("assets", [])]


def bulk_untag(server, key, tag_id, asset_ids):
    """Remove one tag from many assets via DELETE /api/tags/{id}/assets, batched.
    Returns the count Immich reports as changed."""
    total = 0
    for i in range(0, len(asset_ids), BATCH):
        chunk = asset_ids[i:i + BATCH]
        st, resp = api(server, key, f"/api/tags/{tag_id}/assets",
                       method="DELETE", body={"ids": chunk})
        if st == 200 and isinstance(resp, list):
            total += sum(1 for r in resp if r.get("success"))
        elif st == 200 and isinstance(resp, dict):
            total += resp.get("count", 0)
        else:
            print(f"    !! untag call returned {st}: {resp}", file=sys.stderr)
    return total


def main():
    ap = argparse.ArgumentParser(
        description="Remove tag(s) from the assets of a named Immich album.")
    ap.add_argument("--server", required=True, help="Immich base URL, e.g. http://localhost:2283")
    ap.add_argument("--api-key", default=None,
                    help="API key (falls back to the IMMICH_API_KEY env var if omitted)")
    ap.add_argument("--album-name", required=True,
                    help='Exact album name to scope to (e.g. "Camera")')
    ap.add_argument("--tag", action="append", required=True, dest="tags",
                    help="Tag value to remove (repeatable), e.g. src_google_2026_06 or G/Camera")
    ap.add_argument("--yes", action="store_true", help="Actually remove (else dry-run)")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("IMMICH_API_KEY")
    if not api_key:
        sys.exit("FATAL: no API key. Pass --api-key or set the IMMICH_API_KEY env var.")
    key_src = "--api-key flag" if args.api_key else "IMMICH_API_KEY env var"
    print(f"(using key from {key_src} - make sure that's the intended account)")

    # Resolve albums by name.
    st, albums = api(args.server, api_key, "/api/albums")
    if st != 200 or not isinstance(albums, list):
        sys.exit(f"FATAL: GET /api/albums returned {st}: {albums}")
    matches = [a for a in albums if a.get("albumName") == args.album_name]
    if not matches:
        sys.exit(f"FATAL: no album named {args.album_name!r}.")

    # Resolve tag values to ids.
    st, all_tags = api(args.server, api_key, "/api/tags")
    if st != 200 or not isinstance(all_tags, list):
        sys.exit(f"FATAL: GET /api/tags returned {st}: {all_tags}")
    by_value = {t.get("value"): t.get("id") for t in all_tags}
    tag_ids = {}
    for v in args.tags:
        if v not in by_value:
            print(f"  !! tag {v!r} not found on server - skipping", file=sys.stderr)
            continue
        tag_ids[v] = by_value[v]
    if not tag_ids:
        sys.exit("FATAL: none of the given --tag values exist on the server.")

    # Gather unique asset ids across matching albums.
    asset_ids = []
    seen = set()
    for a in matches:
        ids = album_asset_ids(args.server, api_key, a["id"])
        if ids is None:
            print(f"  !! could not read assets for album id {a['id']}", file=sys.stderr)
            continue
        for i in ids:
            if i not in seen:
                seen.add(i)
                asset_ids.append(i)

    print(f"\nAlbum(s) named {args.album_name!r}: {len(matches)}  "
          f"-> {len(asset_ids)} unique assets")
    print("Tags to remove from those assets:")
    for v in tag_ids:
        print(f"  - {v}")

    if not args.yes:
        print(f"\nDry run - nothing changed. Re-run with --yes to remove "
              f"{len(tag_ids)} tag(s) from {len(asset_ids)} assets.")
        return

    print("\nRemoving...")
    for v, tid in tag_ids.items():
        n = bulk_untag(args.server, api_key, tid, asset_ids)
        flag = "" if n == len(asset_ids) else \
            f"  (Immich reported {n}; some may already have lacked the tag, which is fine)"
        print(f"  removed {v} from up to {len(asset_ids)} assets{flag}")

    print("\nDone. Note: a tag now attached to nothing just sits empty - delete it in the "
          "Tags view if you want it gone. If any of these assets also belonged to a real "
          "album that should keep the tag, re-run the source tagger over those albums "
          "(with --exclude-album for this one) to restore it.")


if __name__ == "__main__":
    main()
