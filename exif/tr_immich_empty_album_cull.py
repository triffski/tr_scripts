#!/usr/bin/env python3
"""
tr_immich_empty_album_cull.py - Delete EMPTY albums from an Immich account.

Targets emptiness directly (albums with zero assets) via the Immich API, so it can never
touch an album that still has photos in it. Lists first, deletes only with --yes.

Safe by design:
  - Dry-run by default: prints the empty albums it WOULD delete and stops.
  - Only albums with assetCount == 0 are ever considered.
  - --yes is required to actually delete; even then it prints each deletion.
  - Per-album albumName shown so you can eyeball the list before committing.

API: GET /api/albums returns each album with an assetCount; DELETE /api/albums/{id}
removes one. Album deletion only removes the album container - assets are NOT deleted
(and empty albums have none anyway).

API key resolution: --api-key if given, else the IMMICH_API_KEY env var. If your env var
holds one account's key but you want to act on a DIFFERENT account (e.g. "Memories"),
pass --api-key explicitly to override it for that run - otherwise you'll silently operate
on the env var's account.

Options:
  --server URL      (required) Immich base URL, e.g. http://localhost:2283
  --api-key KEY     API key. Falls back to IMMICH_API_KEY env var if omitted.
  --yes             Actually delete the empty albums. Without it, dry-run (lists only).

Usage:
    # See what's empty (writes nothing):
    python3 tr_immich_empty_album_cull.py --server http://localhost:2283 --api-key KEY
    # using IMMICH_API_KEY from the environment:
    python3 tr_immich_empty_album_cull.py --server http://localhost:2283
    # actually delete the empty ones:
    python3 tr_immich_empty_album_cull.py --server http://localhost:2283 --yes

Requirements: Python 3.9+ (uses only the standard library).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def api(server, api_key, path, method="GET"):
    url = server.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("x-api-key", api_key)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"FATAL: cannot reach {url}: {e.reason}")


def main():
    ap = argparse.ArgumentParser(description="Delete empty albums from an Immich account.")
    ap.add_argument("--server", required=True, help="Immich base URL, e.g. http://localhost:2283")
    ap.add_argument("--api-key", default=None,
                    help="API key (falls back to the IMMICH_API_KEY env var if omitted)")
    ap.add_argument("--yes", action="store_true", help="Actually delete (otherwise dry-run)")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("IMMICH_API_KEY")
    if not api_key:
        sys.exit("FATAL: no API key. Pass --api-key or set the IMMICH_API_KEY env var.")
    key_src = "--api-key flag" if args.api_key else "IMMICH_API_KEY env var"
    print(f"(using key from {key_src} - make sure that's the intended account)")

    status, albums = api(args.server, api_key, "/api/albums")
    if status != 200 or not isinstance(albums, list):
        sys.exit(f"FATAL: GET /api/albums returned {status}: {albums}")

    # assetCount is included in the album list response.
    empties = [a for a in albums if a.get("assetCount", 0) == 0]
    total = len(albums)

    print(f"Account has {total} albums; {len(empties)} are empty.\n")
    if not empties:
        print("Nothing to do.")
        return

    for a in empties:
        print(f"  EMPTY  {a.get('albumName', '(no name)')!r}   id={a.get('id')}")

    if not args.yes:
        print(f"\nDry run - nothing deleted. Re-run with --yes to delete these "
              f"{len(empties)} empty albums.")
        return

    print(f"\nDeleting {len(empties)} empty albums...")
    deleted = failed = 0
    for a in empties:
        st, resp = api(args.server, api_key, f"/api/albums/{a['id']}", method="DELETE")
        if st in (200, 204):
            deleted += 1
            print(f"  deleted  {a.get('albumName')!r}")
        else:
            failed += 1
            print(f"  !! FAILED {a.get('albumName')!r}  ({st}: {resp})", file=sys.stderr)

    print(f"\nDone. deleted: {deleted}   failed: {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
