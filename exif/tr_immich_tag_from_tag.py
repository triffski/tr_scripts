#!/usr/bin/env python3
"""
tr_immich_tag_from_tag.py - Apply a tag to every asset that already carries another tag.

Use it to turn a tag that covers a whole set into a second, cleaner tag over the same set.
The motivating case: immich-go stamps every Google-Takeout import with an auto tag like
"takeout-20260617T022516Z" (all ~58k imported assets, album members or not). The album-based
source tagger only reaches album members (~9k), so a flat provenance tag applied per-album
under-covers. This copies the FULL set: find every asset with the source tag, apply the
target tag to all of them - giving true "this came from Google" provenance over everything,
not just the album-organised photos.

How it enumerates assets (Immich has no "get assets by tag" endpoint, but search does):
  GET  /api/tags                              resolve --from-tag and --to-tag values to ids
  POST /api/search/metadata {tagIds, page}    page through every asset carrying --from-tag
  PUT  /api/tags/assets {tagIds, assetIds}    bulk-apply --to-tag, batched

Idempotent: assets that already have --to-tag are unaffected. Safe to re-run.

API key resolution: --api-key if given, else the IMMICH_API_KEY env var. Pass --api-key
explicitly to act on a different account than the env var's key (the Memories trap).

Options:
  --server URL      (required) Immich base URL, e.g. http://localhost:2283
  --api-key KEY     API key. Falls back to IMMICH_API_KEY env var if omitted.
  --from-tag VALUE  (required) existing tag whose assets define the set, e.g.
                    takeout-20260617T022516Z
  --to-tag VALUE    (required) tag to apply to all of those assets, e.g. src_google_2026_06.
                    Created if it doesn't exist (hierarchical if it contains '/').
  --yes             Actually apply. Without it, dry-run (counts only).

Usage:
  python3 tr_immich_tag_from_tag.py --server http://localhost:2283 \
      --from-tag takeout-20260617T022516Z --to-tag src_google_2026_06
  python3 tr_immich_tag_from_tag.py --server http://localhost:2283 \
      --from-tag takeout-20260617T022516Z --to-tag src_google_2026_06 --yes

Requirements: Python 3.9+ (standard library only).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PAGE = 1000          # assets per search page
BATCH = 500          # assets per bulk tag call


def api(server, api_key, path, method="GET", body=None):
    url = server.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-api-key", api_key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"FATAL: cannot reach {url}: {e.reason}")


def resolve_tag(server, key, value, create=False):
    st, tags = api(server, key, "/api/tags")
    if st != 200 or not isinstance(tags, list):
        sys.exit(f"FATAL: GET /api/tags returned {st}: {tags}")
    for t in tags:
        if t.get("value") == value:
            return t.get("id")
    if not create:
        return None
    st, resp = api(server, key, "/api/tags", method="PUT", body={"tags": [value]})
    if st == 200 and isinstance(resp, list) and resp:
        return resp[0].get("id")
    sys.exit(f"FATAL: could not create tag {value!r}: {st} {resp}")


def assets_with_tag(server, key, tag_id):
    """Page through /api/search/metadata filtering by tagIds; collect all asset ids."""
    ids, page = [], 1
    while True:
        st, resp = api(server, key, "/api/search/metadata", method="POST",
                       body={"tagIds": [tag_id], "page": page, "size": PAGE})
        if st != 200 or not isinstance(resp, dict):
            sys.exit(f"FATAL: search/metadata returned {st}: {resp}")
        assets = resp.get("assets", {})
        items = assets.get("items", [])
        ids.extend(a["id"] for a in items)
        nxt = assets.get("nextPage")
        if not nxt:
            break
        page = int(nxt)
    return ids


def bulk_tag(server, key, tag_id, asset_ids):
    total = 0
    for i in range(0, len(asset_ids), BATCH):
        chunk = asset_ids[i:i + BATCH]
        st, resp = api(server, key, "/api/tags/assets", method="PUT",
                       body={"tagIds": [tag_id], "assetIds": chunk})
        if st == 200 and isinstance(resp, list):
            total += sum(1 for r in resp if r.get("success"))
        elif st == 200 and isinstance(resp, dict):
            total += resp.get("count", 0)
        else:
            print(f"    !! tag call returned {st}: {resp}", file=sys.stderr)
    return total


def main():
    ap = argparse.ArgumentParser(
        description="Apply --to-tag to every asset that carries --from-tag.")
    ap.add_argument("--server", required=True, help="Immich base URL, e.g. http://localhost:2283")
    ap.add_argument("--api-key", default=None,
                    help="API key (falls back to the IMMICH_API_KEY env var if omitted)")
    ap.add_argument("--from-tag", required=True,
                    help="Existing tag whose assets define the set (e.g. takeout-...)")
    ap.add_argument("--to-tag", required=True,
                    help="Tag to apply to all of those assets (created if missing)")
    ap.add_argument("--yes", action="store_true", help="Actually apply (else dry-run)")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("IMMICH_API_KEY")
    if not api_key:
        sys.exit("FATAL: no API key. Pass --api-key or set the IMMICH_API_KEY env var.")
    key_src = "--api-key flag" if args.api_key else "IMMICH_API_KEY env var"
    print(f"(using key from {key_src} - make sure that's the intended account)")

    from_id = resolve_tag(args.server, api_key, args.from_tag, create=False)
    if not from_id:
        sys.exit(f"FATAL: --from-tag {args.from_tag!r} not found on the server.")

    print(f"Enumerating assets tagged {args.from_tag!r} ...")
    asset_ids = assets_with_tag(args.server, api_key, from_id)
    print(f"  found {len(asset_ids)} assets.")
    print(f"Would apply {args.to_tag!r} to all {len(asset_ids)} of them.")

    if not args.yes:
        print("\nDry run - nothing written. Re-run with --yes to apply.")
        return

    to_id = resolve_tag(args.server, api_key, args.to_tag, create=True)
    n = bulk_tag(args.server, api_key, to_id, asset_ids)
    flag = "" if n == len(asset_ids) else \
        f"  (Immich reported {n}; the rest already had it, which is fine)"
    print(f"\nApplied {args.to_tag} to up to {len(asset_ids)} assets{flag}")
    print("Done. Re-running is safe (idempotent).")


if __name__ == "__main__":
    main()
