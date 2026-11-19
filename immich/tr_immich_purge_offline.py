#!/usr/bin/env python3
"""
tr_immich_purge_offline.py - Delete OFFLINE assets from an Immich external library.

Offline = the file no longer exists at the indexed path (e.g. assets left behind
after moving/reorganising files on disk). Online assets are never touched.

Safe by design:
  - Only ever selects assets where isOffline == true for the given library.
  - Dry-run by default; prints a per-extension tally. Use --yes to actually delete.
  - Deletes with force=true (permanent, skips trash) since the files are already gone.

Auth: --api-key, else IMMICH_API_KEY env, else ~/.immich_key.
Server default http://localhost:2283 (host shell reaches Immich via loopback).

Usage:
  python3 tr_immich_purge_offline.py --library <id>            # dry-run
  python3 tr_immich_purge_offline.py --library <id> --yes      # delete
"""
import argparse, json, os, sys, urllib.request, collections
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


def api(server, path, key, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method,
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def collect_offline(server, key, lib):
    ids, names, page = [], [], 1
    while True:
        d = api(server, "/api/search/metadata", key, "POST",
                {"libraryId": lib, "size": 1000, "page": page, "isOffline": True})["assets"]
        items = d["items"]
        if not items:
            break
        for i in items:
            ids.append(i["id"])
            names.append(i["originalFileName"])
        nxt = d.get("nextPage")
        if not nxt:
            break
        page = int(nxt)
    return ids, names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:2283")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--library", required=True, help="external library id")
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--chunk", type=int, default=500, help="delete batch size")
    args = ap.parse_args()

    key = load_key(args.api_key)
    ids, names = collect_offline(args.server, key, args.library)

    ext = collections.Counter(n.rsplit(".", 1)[-1].lower() for n in names)
    print(f"Offline assets found: {len(ids)}")
    for k, v in ext.most_common():
        print(f"  {v:6} .{k}")

    if not ids:
        print("Nothing to do.")
        return

    if not args.yes:
        print("\nDRY RUN. Re-run with --yes to permanently delete these offline rows.")
        print("(Online assets are not affected.)")
        return

    deleted = 0
    for i in range(0, len(ids), args.chunk):
        batch = ids[i:i + args.chunk]
        api(args.server, "/api/assets", key, "DELETE",
            {"ids": batch, "force": True})
        deleted += len(batch)
        print(f"  deleted {deleted}/{len(ids)}")
    print(f"\nDone. Purged {deleted} offline assets.")


if __name__ == "__main__":
    main()
