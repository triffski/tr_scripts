#!/usr/bin/env python3
"""
tr_immich_album_cull_all.py - Delete ALL albums owned by the API key's account.

Use to wipe a bad album-creator run before redoing it. Deletes albums only -
the photos/assets are untouched (albums are just groupings).

Dry-run by default; --yes to delete. Optional --name-contains to limit to albums
whose name contains a substring (e.g. only ones from a given batch).

Auth: --api-key, else IMMICH_API_KEY env, else ~/.immich_key.
To act on a different account, pass that account's key explicitly:
  --api-key "$(cat ~/.immich_key_other)"

Usage:
  python3 tr_immich_album_cull_all.py  # dry-run, all albums
  python3 tr_immich_album_cull_all.py --yes  # delete all
"""
import argparse, json, os, sys, urllib.request
from pathlib import Path


def load_key(explicit):
    if explicit:
        return explicit
    env = os.environ.get("IMMICH_API_KEY")
    if env:
        return env.strip()
    f = Path.home() / ".immich_key"
    if f.exists():
        return f.read_text().strip()
    sys.exit("No API key: pass --api-key, set IMMICH_API_KEY, or create ~/.immich_key")


def api(server, path, key, method="GET"):
    req = urllib.request.Request(server.rstrip("/") + path, method=method,
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:2283")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--name-contains", default=None,
                    help="Only delete albums whose name contains this substring")
    ap.add_argument("--yes", action="store_true", help="Actually delete (default: dry-run)")
    args = ap.parse_args()

    key = load_key(args.api_key)
    albums = api(args.server, "/api/albums", key)  # albums owned by this key's account

    if args.name_contains:
        albums = [a for a in albums if args.name_contains in a["albumName"]]

    print(f"Albums matched: {len(albums)}")
    for a in albums:
        print(f"  {a.get('assetCount','?'):>5}  {a['albumName']}")

    if not albums:
        print("Nothing to do.")
        return

    if not args.yes:
        print(f"\nDRY RUN. Re-run with --yes to DELETE these {len(albums)} albums "
              f"(photos are NOT deleted, only the album groupings).")
        return

    deleted = 0
    for a in albums:
        api(args.server, f"/api/albums/{a['id']}", key, method="DELETE")
        deleted += 1
        if deleted % 25 == 0:
            print(f"  deleted {deleted}/{len(albums)}")
    print(f"\nDone. Deleted {deleted} albums. Photos untouched.")


if __name__ == "__main__":
    main()
