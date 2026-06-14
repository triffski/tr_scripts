#!/usr/bin/env python3
"""
v4 — Google Places (New) Text Search geocoder for Google Maps saved-list CSVs.

Why v4: Nominatim (v3) gets the right city via hints but often the wrong specific
venue. Google's Places Text Search matches against Google's own POI database, so a
named query biased to the list's region returns Google's actual place — far more
accurate for real businesses.

API KEY HANDLING (safe to push to GitHub):
  The key is read from an EXTERNAL file, never hard-coded. Lookup order:
    1. ./google_api_key.txt  (single line containing the key)   <-- gitignore this
    2. $GOOGLE_API_KEY environment variable
  Create the file:   echo 'AIza...yourkey' > google_api_key.txt
  Gitignore it:      echo 'google_api_key.txt' >> .gitignore

Requires: Places API (New) enabled on a billing-enabled Google Cloud project.
Cost: field-masked to 'location' + 'displayName' = cheapest tier; ~500 lookups is
well within the monthly free credit.

Usage:
    python3 -m pip install requests --break-system-packages
    echo 'AIza...' > google_api_key.txt
    python3 saved_places_to_geojson_v4.py . ./output_google
"""

import csv, glob, json, os, re, sys, time
import requests

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
SLEEP = 0.1  # Google allows high QPS; small pause is courtesy/ratelimit-safety

# Region bias per list (free-text appended to the query). Edit to taste.
LIST_HINTS = {
    "Liverpool_Pubs": "Liverpool, UK",
    "Beer_gardens":   "Liverpool, UK",
    "Vietnam":        "Vietnam",
    # Want_to_go / Favourite_places / got_hates_flags: global, no hint
}

coord_patterns = [
    re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)"),
    re.compile(r"/maps/search/(-?\d+\.\d+),(-?\d+\.\d+)"),
    re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)"),
]
place_slug_re = re.compile(r"/maps/place/([^/@]+)")

def load_key():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_api_key.txt")
    if os.path.exists("google_api_key.txt"):
        return open("google_api_key.txt").read().strip()
    if os.path.exists(p):
        return open(p).read().strip()
    if os.environ.get("GOOGLE_API_KEY"):
        return os.environ["GOOGLE_API_KEY"].strip()
    sys.exit("No API key. Create google_api_key.txt or set GOOGLE_API_KEY.")

def find_rows(path):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        lines = list(csv.reader(f))
    for i, row in enumerate(lines):
        if row[:3] == ["Title", "Note", "URL"]:
            hdr = row
            return [dict(zip(hdr, r + [""]*(len(hdr)-len(r)))) for r in lines[i+1:]]
    return []

def coords_from_url(url):
    for rx in coord_patterns:
        m = rx.search(url or "")
        if m:
            return [float(m.group(2)), float(m.group(1))]  # [lon, lat]
    return None

def slug_name(url):
    m = place_slug_re.search(url or "")
    return m.group(1).replace("+", " ").strip() if m else None

def places_search(query, key, cache):
    if query in cache:
        return cache[query]
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        # field mask -> cheapest tier; only ask for what we need
        "X-Goog-FieldMask": "places.location,places.displayName,places.formattedAddress",
    }
    body = {"textQuery": query, "maxResultCount": 1}
    try:
        r = requests.post(PLACES_URL, headers=headers, json=body, timeout=20)
        time.sleep(SLEEP)
        if r.status_code == 200:
            js = r.json()
            places = js.get("places", [])
            if places:
                loc = places[0]["location"]
                res = {"lat": loc["latitude"], "lon": loc["longitude"],
                       "name": places[0].get("displayName", {}).get("text"),
                       "addr": places[0].get("formattedAddress")}
            else:
                res = None
        else:
            res = {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        res = {"error": str(e)}
    cache[query] = res
    return res


def write_kml(listname, feats, out):
    """Write a .kml bookmark set for Organic Maps / OsmAnd. One file per list =
    one toggleable bookmark group on the phone."""
    def esc(s):
        s = s or ""
        return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                 .replace('"',"&quot;"))
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2">',
             '<Document>',
             f'  <name>{esc(listname)}</name>']
    for f in feats:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        name = esc(p.get("google_match") or p.get("name"))
        desc_bits = []
        if p.get("google_address"): desc_bits.append(esc(p["google_address"]))
        if p.get("note"):           desc_bits.append(esc(p["note"]))
        if p.get("google_maps_url"):desc_bits.append(esc(p["google_maps_url"]))
        desc = "&#10;".join(desc_bits)
        parts += ['  <Placemark>',
                  f'    <name>{name}</name>',
                  f'    <description>{desc}</description>',
                  f'    <Point><coordinates>{lon},{lat},0</coordinates></Point>',
                  '  </Placemark>']
    parts += ['</Document>', '</kml>']
    with open(os.path.join(out, f"{listname}.kml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))

def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: python3 saved_places_to_geojson_v4.py <csv_folder> <output_folder>")
    csv_folder, out = sys.argv[1], sys.argv[2]
    key = load_key()
    os.makedirs(out, exist_ok=True)
    cache_path = os.path.join(out, "places_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    review = []

    files = glob.glob(os.path.join(csv_folder, "*.csv"))
    files += [f for f in glob.glob(os.path.join(csv_folder, "*")) if "homes_and" in os.path.basename(f)]
    for path in sorted(set(files)):
        listname = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(path))[0]).strip("_")
        hint = LIST_HINTS.get(listname, "")
        feats = []
        for row in find_rows(path):
            title = (row.get("Title") or "").strip()
            url   = (row.get("URL") or "").strip()
            note  = (row.get("Note") or "").strip()
            if not title and not url:
                continue
            if "/shopping/" in url or "/product/" in url:
                continue
            props = {"name": title, "note": note, "google_maps_url": url, "list": listname}

            c = coords_from_url(url)
            if c:
                props["coord_source"] = "url"
                feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":c},"properties":props})
                continue

            q = f"{title}, {hint}" if hint else (title or slug_name(url) or "")
            if not q:
                review.append([listname, title, "EMPTY QUERY", "", url]); continue
            g = places_search(q, key, cache)
            json.dump(cache, open(cache_path, "w"))
            if g and "lat" in g:
                props.update(coord_source="google_places",
                             google_match=g.get("name"),
                             google_address=g.get("addr"),
                             query=q)
                feats.append({"type":"Feature","geometry":{"type":"Point","coordinates":[g["lon"],g["lat"]]},"properties":props})
            else:
                err = g.get("error") if g else "no result"
                review.append([listname, title, f"NO MATCH ({err})", "", url])

        json.dump({"type":"FeatureCollection","features":feats},
                  open(os.path.join(out,f"{listname}.geojson"),"w"), indent=2, ensure_ascii=False)
        write_kml(listname, feats, out)
        print(f"{listname}: {len(feats)} features (.geojson + .kml)")

    if review:
        with open(os.path.join(out,"_geocoding_review.csv"),"w",newline="",encoding="utf-8") as f:
            w=csv.writer(f); w.writerow(["list","title","matched_to","importance","url"]); w.writerows(review)
        print(f"\n{len(review)} flagged for review -> {out}/_geocoding_review.csv")
    json.dump(cache, open(cache_path,"w"))
    print("Done.")

if __name__ == "__main__":
    main()