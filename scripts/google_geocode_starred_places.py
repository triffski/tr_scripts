#!/usr/bin/env python3
"""
google_geocode_starred_places.py

Reconstructs a Google Maps Starred / visited list that Google Takeout WON'T export.
(The Starred list is private and non-shareable, so it never appears as its own file
in a Takeout "Saved" export — only mixed into the merged "Maps (your places)" dump.)

METHOD:
  1. Read the merged 'Saved Places.json' ("Maps (your places)" export) — every saved
     place, but with no list membership and (for many) no name, only a coordinate.
  2. Exclude every place that already appears in your per-list GeoJSONs (the output of
     the companion script, saved_places_to_geojson_v4.py) — those belong to known
     lists, so what remains is, by elimination, your Starred/visited set.
  3. Reverse-geocode each remaining coordinate via Google Places API (New) Nearby
     Search (tight radius) to recover a name + address. Matches are marked
     "Approximate location match" in the description, since they are nearest-POI
     guesses, not confirmed saves. Coordinates with no POI in range stay "Visited".
  4. Output the recovered list as GeoJSON / KML / GPX / CSV.

LIMITATIONS (inherent to the method — note for any other user):
  * APPROXIMATE: auto-named places are the nearest POI to the saved coordinate, not a
    confirmed identity. A pin in a dense building may name the wrong unit.
  * UNDERCOUNTS: places that are ALSO in another of your lists are excluded, so a
    place that was both Starred and (say) Want-to-go won't appear here.
  * Needs BOTH inputs in the SAME folder: the merged 'Saved Places.json' (space or
    underscore) AND the per-list *.geojson files from the companion script.

ARGUMENTS (mirrors the companion script: <input_folder> <output_folder>):
  input_folder  : a SINGLE folder that must contain BOTH:
                    - 'Saved Places.json'  (the merged "Maps (your places)" export)
                    - the per-list *.geojson files from the companion geocoder.
                  Easiest setup: run the companion script with its output going to
                  this folder, then drop 'Saved Places.json' into the same folder.
  output_folder : where to write Starred_visited.{geojson,gpx,kml,csv}
                  (can be the same folder as input).

Requires: Google Places API (New) enabled, billing-enabled project.
Key read from google_api_key.txt (in cwd) or $GOOGLE_API_KEY. Gitignore the key file.
A cache (.cache/nearby_cache.json, inside the output folder) avoids re-charging on reruns.

USAGE:
    python3 -m pip install requests --break-system-packages
    echo 'AIza...' > google_api_key.txt
    # ./output_google must hold the per-list .geojson AND 'Saved Places.json'
    python3 google_geocode_starred_places.py ./output_google ./output_google
"""
import os, sys, json, csv, math, time, glob
import requests

RADIUS = 25.0          # metres — tight: name only near-exact matches
SLEEP  = 0.15
NEARBY = "https://places.googleapis.com/v1/places:searchNearby"
KNOWN_LISTS = ("Beer_gardens", "Favourite_places", "got_hates_flags",
               "Liverpool_Pubs", "Vietnam", "Want_to_go")

def load_key():
    if os.path.exists("google_api_key.txt"):
        return open("google_api_key.txt").read().strip()
    k = os.environ.get("GOOGLE_API_KEY")
    if k: return k.strip()
    sys.exit("No API key (google_api_key.txt in cwd, or $GOOGLE_API_KEY).")

def hav(a, b, c, d):
    R = 6371000; p1, p2 = math.radians(a), math.radians(c)
    dp = math.radians(c - a); dl = math.radians(d - b)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(x))

def esc(s):
    s = s or ''
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: python3 google_geocode_starred_places.py <input_folder> <output_folder>")
    inp, out = sys.argv[1], sys.argv[2]
    key = load_key()
    os.makedirs(out, exist_ok=True)
    cache_dir = os.path.join(out, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "nearby_cache.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    # confirmed names from the companion script's per-list GeoJSONs
    known = []
    for fn in KNOWN_LISTS:
        p = os.path.join(inp, fn + ".geojson")
        if os.path.exists(p):
            for f in json.load(open(p))["features"]:
                lon, lat = f["geometry"]["coordinates"]
                if [lon, lat] != [0, 0]:
                    known.append((lat, lon, f["properties"].get("google_match") or f["properties"].get("name")))
    if not known:
        print("WARNING: no per-list .geojson files found in input folder — every place "
              "will be treated as unknown (none excluded, none confirmed).")

    def confirmed_name(lat, lon):
        for klat, klon, knm in known:
            if abs(klat-lat) < 0.05 and abs(klon-lon) < 0.05 and hav(lat, lon, klat, klon) <= 50:
                return knm
        return None

    def nearby(lat, lon):
        ck = f"{lat:.6f},{lon:.6f}"
        if ck in cache: return cache[ck]
        try:
            r = requests.post(NEARBY,
                headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                         "X-Goog-FieldMask": "places.displayName,places.location,places.formattedAddress"},
                json={"maxResultCount": 1,
                      "locationRestriction": {"circle": {
                          "center": {"latitude": lat, "longitude": lon}, "radius": RADIUS}}},
                timeout=20)
            time.sleep(SLEEP)
            if r.status_code == 200 and r.json().get("places"):
                p = r.json()["places"][0]
                res = {"name": p.get("displayName", {}).get("text"),
                       "address": p.get("formattedAddress")}
            else:
                res = None
        except Exception as e:
            res = {"error": str(e)}
        cache[ck] = res; json.dump(cache, open(cache_path, "w"))
        return res

    cand = glob.glob(os.path.join(inp, "Saved[ _]Places.json"))
    if not cand:
        sys.exit("No 'Saved Places.json' / 'Saved_Places.json' found in input folder.")
    d = json.load(open(cand[0]))
    placed = [f for f in d["features"] if f["geometry"]["coordinates"] not in ([0,0],[0.0,0.0])]

    recs = []; n_conf = n_auto = n_none = 0
    for f in placed:
        lon, lat = f["geometry"]["coordinates"]
        date = f["properties"].get("date", "")[:10]
        url = f["properties"].get("google_maps_url", "")
        cn = confirmed_name(lat, lon)
        if cn:
            recs.append({"lat":lat,"lon":lon,"title":cn,"date":date,"url":url,
                         "address":"","approx":False}); n_conf += 1
        else:
            g = nearby(lat, lon)
            if g and g.get("name"):
                recs.append({"lat":lat,"lon":lon,"title":g["name"],"date":date,"url":url,
                             "address":g.get("address") or "","approx":True}); n_auto += 1
            else:
                recs.append({"lat":lat,"lon":lon,"title":"Visited","date":date,"url":url,
                             "address":"","approx":False}); n_none += 1

    def desc(r):
        bits = []
        if r["date"]:    bits.append(f"Visited {r['date']}")
        if r["address"]: bits.append(r["address"])
        if r["approx"]:  bits.append("Approximate location match")
        if r["url"]:     bits.append(r["url"])
        return " | ".join(bits)

    # GeoJSON
    gj = [{"type":"Feature","geometry":{"type":"Point","coordinates":[r["lon"],r["lat"]]},
           "properties":{"name":r["title"],"date":r["date"],"address":r["address"],
                         "approximate":r["approx"],"google_maps_url":r["url"],
                         "list":"Starred_visited"}} for r in recs]
    json.dump({"type":"FeatureCollection","features":gj},
              open(os.path.join(out,"Starred_visited.geojson"),"w"), indent=2, ensure_ascii=False)

    # GPX (with osmand:address so OsmAnd populates the Address field; no icon styling
    # — set per-group "Default appearance" in OsmAnd instead)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="google_geocode_starred_places" '
             'xmlns="http://www.topografix.com/GPX/1/1" xmlns:osmand="https://osmand.net">']
    for r in recs:
        parts += [f'  <wpt lat="{r["lat"]}" lon="{r["lon"]}">',
                  f'    <name>{esc(r["title"])}</name>',
                  f'    <desc>{esc(desc(r))}</desc>',
                  '    <type>Starred_visited</type>']
        if r["address"]:
            parts += ['    <extensions>',
                      f'      <osmand:address>{esc(r["address"])}</osmand:address>',
                      '    </extensions>']
        parts.append('  </wpt>')
    parts.append('</gpx>')
    open(os.path.join(out,"Starred_visited.gpx"),"w").write("\n".join(parts))

    # KML
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2">','<Document>',
             '  <name>Starred_visited</name>']
    for r in recs:
        parts += ['  <Placemark>',
                  f'    <name>{esc(r["title"])}</name>',
                  f'    <description>{esc(desc(r))}</description>',
                  f'    <Point><coordinates>{r["lon"]},{r["lat"]},0</coordinates></Point>',
                  '  </Placemark>']
    parts += ['</Document>','</kml>']
    open(os.path.join(out,"Starred_visited.kml"),"w").write("\n".join(parts))

    # CSV (Title,Note,URL,Tags,Comment — matches the Takeout per-list format)
    with open(os.path.join(out,"Starred_visited.csv"),"w",newline="",encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["Title","Note","URL","Tags","Comment"]); w.writerow(["","","","",""])
        for r in recs:
            note_bits = []
            if r["date"]:    note_bits.append(f"Visited {r['date']}")
            if r["address"]: note_bits.append(r["address"])
            if r["approx"]:  note_bits.append("Approximate location match")
            w.writerow([r["title"], " | ".join(note_bits), r["url"], "", ""])

    print(f"total placed: {len(recs)}")
    print(f"  confirmed names (from your lists): {n_conf}")
    print(f"  auto-named (nearest within {int(RADIUS)}m, marked approximate): {n_auto}")
    print(f"  no POI within {int(RADIUS)}m -> 'Visited': {n_none}")
    print("wrote Starred_visited.{geojson,gpx,kml,csv} to", out)

if __name__ == "__main__":
    main()
