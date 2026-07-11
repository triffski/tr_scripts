#!/usr/bin/env python3
"""
tr_immich_library_count.py - Count assets in an Immich external library and tally
by file extension, to diagnose phantom (xmp/json) imports vs real images.

Auth: --api-key, else IMMICH_API_KEY env, else ~/.immich_key.
Server defaults to http://localhost:2283 (host shell can't reach 10.0.0.2).

Usage:
  python3 tr_immich_library_count.py --library 14c5a000-e827-4709-acdb-d46049cfb465
  python3 tr_immich_library_count.py            # lists libraries if no --library
"""
import argparse, json, os, sys, urllib.request, collections
from pathlib import Path


def load_key(explicit):
    if explicit:
        return explicit, "flag"
    env = os.environ.get("IMMICH_API_KEY")
    if env:
        return env.strip(), "env"
    f = Path.home() / ".immich_key"
    if f.exists():
        return f.read_text().strip(), "~/.immich_key"
    sys.exit("No API key: pass --api-key, set IMMICH_API_KEY, or create ~/.immich_key")


def api(server, path, key, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method,
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:2283")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--library", default=None, help="library id (omit to list libraries)")
    args = ap.parse_args()

    key, src = load_key(args.api_key)
    print(f"API key from: {src}")

    if not args.library:
        libs = api(args.server, "/api/libraries", key)
        print("Libraries:")
        for L in libs:
            print(f"  {L['id']}  {L['name']:20}  assetCount(meta)={L.get('assetCount')}")
        return

    ext = collections.Counter()
    total = 0
    page = 1
    while True:
        d = api(args.server, "/api/search/metadata", key, "POST",
                {"libraryId": args.library, "size": 1000, "page": page})["assets"]
        items = d["items"]
        if not items:
            break
        for i in items:
            ext[i["originalFileName"].rsplit(".", 1)[-1].lower()] += 1
        total += len(items)
        nxt = d.get("nextPage")
        if not nxt:
            break
        page = int(nxt)

    print(f"\nTOTAL assets in library: {total}")
    for k, v in ext.most_common():
        print(f"  {v:6} .{k}")


if __name__ == "__main__":
    main()
