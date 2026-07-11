#!/usr/bin/env python3
"""
tr_immich_source_tagger.py - Add source + album tags to imported Immich albums.

immich-go's from-google-photos recreates album MEMBERSHIP (--sync-albums) but writes no
album-name tags. This adds them via the API, after import, so Google albums carry the same
tagging scheme as the Facebook/Instagram enrich scripts:

  - flat provenance tag : src_google_<batch>        (e.g. src_google_2026_06)
  - nested album tag    : <prefix>/<album name>      (e.g. G/IKEA Adventure)

Both are applied to every asset of every (non-empty) album. The flat tag says WHERE a photo
came from; the nested tag groups it by album under a short single-letter parent.

Run this in the source's own window - i.e. straight after the Google import, BEFORE
re-importing Facebook/Instagram - so only Google's albums exist and nothing else picks up
the G/ prefix or src_google tag.

API used:
  GET /api/albums                     list albums (+ assetCount)
  GET /api/albums/{id}                album asset ids
  PUT /api/tags  {"tags":[v]}         upsert hierarchical tag, returns leaf id
  PUT /api/tags/assets                bulk apply {"tagIds":[...],"assetIds":[...]}

Notes:
  - Immich has no "list assets by tag" endpoint, so the script can't verify each asset
    after tagging; it prints tagged/expected counts per album so any shortfall is visible.
    Re-running is safe (tagging is idempotent).
  - '/' in an album name is the hierarchy separator, so it's replaced with '-'.
  - Defaults suit Google (prefix G, source-name google); override for other sources.

Safety: dry-run by default (lists what it would tag); --yes to apply. Empty albums skipped.

API key resolution: --api-key if given, else the IMMICH_API_KEY env var. If you keep a
key in IMMICH_API_KEY for one account but want to act on a DIFFERENT account (e.g. a
separate "Memories" account), pass --api-key explicitly to override the env var for that
run - otherwise you'll silently tag the env var's account.

Options:
  --server URL      (required) Immich base URL, e.g. http://localhost:2283
  --api-key KEY     API key. Falls back to IMMICH_API_KEY env var if omitted.
  --prefix P        Album hierarchy parent letter. Default "G". Each album becomes P/<name>.
  --source NAME     Source name in the flat tag src_<NAME>_<batch>. Default "google".
  --batch SUFFIX    Batch suffix for the flat source tag. Default "2026_06".
  --no-batch        Drop the batch suffix: flat tag is just src_<source>. Use for a
                    continuous source (e.g. phone sync) where there's no batch.
  --album-name NAME Only tag the album(s) whose name matches NAME exactly (e.g. "Camera").
                    Default: all albums. Use to scope a scheduled/cron run to one album.
  --exclude-album NAME  Skip the album(s) named NAME (repeatable, exact match). Use to keep
                    a non-source album out of a run, e.g. --exclude-album "Camera" on a
                    Google run so phone photos don't get tagged src_google.
  --yes             Actually create/apply tags. Without it, dry-run (lists only).

Resulting tags per asset:  src_<source>_<batch>  (flat)  +  <prefix>/<album>  (nested)
Defaults produce:          src_google_2026_06            +  G/<album>

Usage:
  python3 tr_immich_source_tagger.py --server http://localhost:2283 --api-key KEY
  python3 tr_immich_source_tagger.py --server http://localhost:2283 --api-key KEY --yes
  # using IMMICH_API_KEY from the environment:
  python3 tr_immich_source_tagger.py --server http://localhost:2283 --yes
  # other source: G->F, google->facebook
  python3 tr_immich_source_tagger.py --server ... --api-key KEY --prefix F --source facebook --yes

Requirements: Python 3.9+ (standard library only).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

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
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"FATAL: cannot reach {url}: {e.reason}")


def tag_leaf(name):
    """'/' is the hierarchy separator; keep an album '/' from creating spurious nesting."""
    return name.replace("/", "-").strip()


def album_asset_ids(server, key, album_id):
    st, info = api(server, key, f"/api/albums/{album_id}")
    if st != 200 or not isinstance(info, dict):
        return None
    return [a["id"] for a in info.get("assets", [])]


def upsert_tag(server, key, value):
    """Upsert a (possibly hierarchical) tag value; return its leaf id (or None)."""
    st, resp = api(server, key, "/api/tags", method="PUT", body={"tags": [value]})
    if st == 200 and isinstance(resp, list) and resp:
        return resp[0].get("id")
    return None


def bulk_tag(server, key, tag_id, asset_ids):
    """Apply one tag to many assets via PUT /api/tags/assets, batched. Returns the total
    Immich reports as tagged."""
    total = 0
    for i in range(0, len(asset_ids), BATCH):
        chunk = asset_ids[i:i + BATCH]
        st, resp = api(server, key, "/api/tags/assets", method="PUT",
                       body={"tagIds": [tag_id], "assetIds": chunk})
        if st == 200 and isinstance(resp, dict):
            total += resp.get("count", 0)
        elif st == 200 and isinstance(resp, list):
            total += sum(1 for r in resp if r.get("success"))
        else:
            print(f"    !! tag call returned {st}: {resp}", file=sys.stderr)
    return total


def main():
    ap = argparse.ArgumentParser(
        description="Add src_<source>_<batch> + <prefix>/<album> tags to Immich albums.")
    ap.add_argument("--server", required=True, help="Immich base URL, e.g. http://localhost:2283")
    ap.add_argument("--api-key", default=None,
                    help="API key (falls back to the IMMICH_API_KEY env var if omitted)")
    ap.add_argument("--prefix", default="G", help='Album hierarchy parent (default "G")')
    ap.add_argument("--source", default="google",
                    help='Source name for the flat tag src_<source>_<batch> (default "google")')
    ap.add_argument("--batch", default="2026_06",
                    help='Batch suffix for the flat source tag (default "2026_06")')
    ap.add_argument("--no-batch", action="store_true",
                    help="Drop the batch suffix - flat tag is just src_<source> (for a "
                         "continuous source like phone sync, where there's no batch)")
    ap.add_argument("--album-name", default=None,
                    help="Only tag the album(s) whose name matches this exactly "
                         "(e.g. \"Camera\"). Default: all albums.")
    ap.add_argument("--exclude-album", action="append", default=[], dest="exclude_albums",
                    help="Album name to skip (repeatable, exact match). e.g. to keep a phone "
                         "\"Camera\" album out of a Google run: --exclude-album \"Camera\".")
    ap.add_argument("--yes", action="store_true", help="Actually create/apply tags (else dry-run)")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("IMMICH_API_KEY")
    if not api_key:
        sys.exit("FATAL: no API key. Pass --api-key or set the IMMICH_API_KEY env var.")
    key_src = "--api-key flag" if args.api_key else "IMMICH_API_KEY env var"
    print(f"(using key from {key_src} - make sure that's the intended account)")

    src_tag = f"src_{args.source}" if args.no_batch else f"src_{args.source}_{args.batch}"

    st, albums = api(args.server, api_key, "/api/albums")
    if st != 200 or not isinstance(albums, list):
        sys.exit(f"FATAL: GET /api/albums returned {st}: {albums}")

    work = [(a["id"], a.get("albumName", "(no name)"), a.get("assetCount", 0))
            for a in albums if a.get("assetCount", 0) > 0]

    if args.album_name is not None:
        work = [w for w in work if w[1] == args.album_name]
        if not work:
            print(f"No album named {args.album_name!r} with assets. "
                  f"Nothing to do.")
            return
        print(f"Filtered to album(s) named {args.album_name!r}.")

    if args.exclude_albums:
        excluded = {e for e in args.exclude_albums}
        before = len(work)
        work = [w for w in work if w[1] not in excluded]
        skipped = before - len(work)
        if skipped:
            print(f"Excluding {skipped} album(s): {', '.join(sorted(excluded))}")

    print(f"Account has {len(albums)} albums; {len(work)} with assets.")
    print(f"Each asset gets:  {src_tag}  +  {args.prefix}/<album>\n")
    if not work:
        print("Nothing to do.")
        return

    for _, name, count in work:
        print(f"  {args.prefix}/{tag_leaf(name)}   ({count} assets)")

    if not args.yes:
        print(f"\nDry run - nothing written. Re-run with --yes to tag these "
              f"{len(work)} albums (+ flat {src_tag}).")
        return

    # Pre-create the flat source tag once.
    src_id = upsert_tag(args.server, api_key, src_tag)
    if not src_id:
        sys.exit(f"FATAL: could not create source tag {src_tag!r}")

    print(f"\nApplying tags...")
    ok = failed = 0
    for album_id, name, count in work:
        ids = album_asset_ids(args.server, api_key, album_id)
        if ids is None:
            failed += 1
            print(f"  !! could not read assets for {name!r}", file=sys.stderr)
            continue
        # nested album tag
        alb_id = upsert_tag(args.server, api_key, f"{args.prefix}/{tag_leaf(name)}")
        if not alb_id:
            failed += 1
            print(f"  !! could not create album tag for {name!r}", file=sys.stderr)
            continue
        n_alb = bulk_tag(args.server, api_key, alb_id, ids)
        n_src = bulk_tag(args.server, api_key, src_id, ids)
        flag = "" if (n_alb == len(ids) and n_src == len(ids)) else \
            f"  <-- expected {len(ids)} (album {n_alb}, src {n_src}), check this one"
        print(f"  {len(ids):5d} assets -> {args.prefix}/{tag_leaf(name)} + {src_tag}{flag}")
        ok += 1

    print(f"\nDone. albums tagged: {ok}   failed: {failed}")
    print(f"Verify in Tags view: a '{args.prefix}' parent with album children, plus a flat "
          f"'{src_tag}'. Mismatches are safe to fix by re-running (idempotent).")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
