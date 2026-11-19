#!/usr/bin/env python3
"""
tr_immich_delete_by_name.py - List or delete Immich PEOPLE by name.

Built for a faces rebuild: after a full Face Detection reset, stale
named-person records can survive the wipe (faces cleared, person kept) and re-seed
"junk-magnet" clusters that hoover up wrong faces. This finds those survivors by name
and deletes them via the official API, so a subsequent Facial Recognition reset
re-clusters with NO inherited centroids.

Deleting a person via DELETE /api/people/{id} removes the person record and unassigns
its faces - the FACES AND PHOTOS ARE UNTOUCHED, they just return to the unassigned pool
to re-cluster cleanly. It is NOT a photo deletion.

MODES
  --list                 list every named person (id + name + face count). Read-only.
  --name NAME            target person/people whose name matches NAME (repeatable).
                         Exact match by default; --contains for substring.
  --all-named            target EVERY person that has a (non-empty) name - i.e. wipe all
                         inherited names before a clean re-cluster. Use with care.
  --contains             match --name as a case-insensitive substring, not exact.
  --yes                  actually delete (default: dry-run, lists what it WOULD delete).

SAFETY
  Dry-run by default. Prints the matched people (name, id, #faces) and does nothing until
  --yes. Deletion only removes person records; detected faces survive and re-cluster.
  Unnamed people (auto-clusters with no name) are NEVER touched unless you explicitly
  match them - this only ever targets named survivors, or --all-named.

API + KEY: default http://localhost:2283 ; key from --api-key, else ~/.immich_key,
else $IMMICH_API_KEY. Pass the key explicitly when acting on a different account - don't rely
on an env var pointing at another account (the multi-account trap).

USAGE
  # see what named people exist
  python3 tr_immich_delete_by_name.py --list
  # dry-run delete one
  python3 tr_immich_delete_by_name.py --name "Jane Smith"
  # delete it
  python3 tr_immich_delete_by_name.py --name "Jane Smith" --yes
  # several at once
  python3 tr_immich_delete_by_name.py --name "Jane Smith" --name "John Smith" --yes
  # substring match (catches "Jane Smith", "Jane Smith 2", etc.)
  python3 tr_immich_delete_by_name.py --name jane --contains --yes
  # nuke ALL inherited names before a clean re-cluster
  python3 tr_immich_delete_by_name.py --all-named --yes

Requirements: Python 3.9+ (standard library only).
"""
import argparse, json, os, sys, urllib.error, urllib.request

DEFAULT_API = "http://localhost:2283"
KEY_PATH = os.path.expanduser("~/.immich_key")


def load_key(cli_key):
    if cli_key:
        return cli_key
    if os.path.exists(KEY_PATH):
        return open(KEY_PATH).read().strip()
    if os.environ.get("IMMICH_API_KEY"):
        return os.environ["IMMICH_API_KEY"].strip()
    sys.exit(f"No API key: pass --api-key, or put one at {KEY_PATH}, or set $IMMICH_API_KEY.")


def api(server, key, path, method="GET", timeout=60):
    url = server.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("x-api-key", key)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        sys.exit(f"FATAL: cannot reach {url}: {e.reason}")


def all_people(server, key):
    """GET /api/people - paginate. Returns list of {id,name,...}. By default the endpoint
    returns named/visible people; withHidden picks up hidden ones too."""
    out, page = [], 1
    while True:
        st, resp = api(server, key, f"/api/people?page={page}&size=500&withHidden=true")
        if st != 200 or not isinstance(resp, dict):
            sys.exit(f"FATAL: GET /api/people returned {st}: {resp}")
        people = resp.get("people", [])
        out.extend(people)
        # immich paginates with hasNextPage / total; stop when a page returns < size
        if len(people) < 500 or not people:
            break
        page += 1
    return out


def main():
    ap = argparse.ArgumentParser(description="List or delete Immich people by name.")
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--list", action="store_true", help="list all named people and exit")
    ap.add_argument("--name", action="append", default=[], dest="names",
                    help="person name to target (repeatable)")
    ap.add_argument("--contains", action="store_true",
                    help="match --name as a case-insensitive substring (default: exact)")
    ap.add_argument("--all-named", action="store_true",
                    help="target every person with a non-empty name")
    ap.add_argument("--yes", action="store_true", help="actually delete (default dry-run)")
    args = ap.parse_args()

    key = load_key(args.api_key)
    src = "--api-key flag" if args.api_key else \
          ("~/.immich_key" if os.path.exists(KEY_PATH) else "IMMICH_API_KEY env")
    print(f"(key from {src} - confirm this is the intended account)\n")

    people = all_people(args.api, key)
    named = [p for p in people if (p.get("name") or "").strip()]

    if args.list:
        print(f"{len(people)} people total, {len(named)} named:\n")
        for p in sorted(named, key=lambda x: x.get("name", "").lower()):
            print(f"  {p.get('name'):30s}  {p['id']}")
        if not named:
            print("  (no named people)")
        return

    # build the target set
    if args.all_named:
        targets = named
    elif args.names:
        targets = []
        for p in named:
            nm = p.get("name", "")
            for want in args.names:
                hit = (want.lower() in nm.lower()) if args.contains else (nm == want)
                if hit:
                    targets.append(p)
                    break
    else:
        sys.exit("Nothing to do: pass --list, --name NAME, or --all-named.")

    if not targets:
        print("No matching named people found. Nothing to do.")
        return

    print(f"{'DELETE' if args.yes else 'DRY-RUN'} - {len(targets)} person(s) matched:")
    for p in sorted(targets, key=lambda x: x.get("name", "").lower()):
        print(f"  {p.get('name'):30s}  {p['id']}")
    print("  (faces/photos are NOT deleted - they return to the unassigned pool)")

    if not args.yes:
        print("\nDry-run only. Re-run with --yes to delete these people.")
        return

    ok = fail = 0
    for p in targets:
        st, _ = api(args.api, key, f"/api/people/{p['id']}", method="DELETE")
        if st in (200, 204):
            ok += 1
            print(f"  deleted: {p.get('name')}")
        else:
            fail += 1
            print(f"  !! failed [{st}]: {p.get('name')} ({p['id']})", file=sys.stderr)
    print(f"\nDone. deleted: {ok}   failed: {fail}")
    if ok:
        print("Now run Facial Recognition -> Reset so faces re-cluster with no inherited people.")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
