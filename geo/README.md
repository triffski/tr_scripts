# Maps 'n Stuff

|Name|Description|
|----|-----------|
| `tr_google_saved_places_exporter.py` | Convert custom place lists saved to your Google Maps account into GeoJSON, KML and GPX for use anywhere that imports them (OsmAnd, Organic Maps, etc.). <br><br>Stage 1 geocodes your 'Saved' list CSVs via the Google Places API; <br>Stage 2 (optional) reconstructs the Starred/visited list Takeout won't export; <br>Stage 3 (optional, slow) corrects ambiguous-name mismatches by re-resolving each pin through its real Google Maps URL.|

## Google Saved Places Exporter
  
A full run needs two inputs from **Google Takeout** (https://takeout.google.com), coming from two *different* Takeout products, plus a Google API key saved to `google_api_key.txt` (https://console.cloud.google.com/ > Credentials > API Keys).  

Whack this into a single line text file alongside the script.  

### Getting your data off Google:  

1. Go to Takeout, **Deselect all**, then select only **"Saved"** and **"Maps (your places)"**.
2. Export, download the zip, and unzip.
3. Inside the `Takeout/Saved/` folder you'll find **one CSV per list** . These are the pipeline's main input, copy them to a working folder.
4. Inside `Maps (your places)/` is **`Saved Places.json`** (optional) — drop it in the same input folder, alongside the CSVs. If `Saved Places.json` isn't present, Stage 2 is skipped.

Run: `python3 google_saved_places_pipeline.py {data location} {output folder}`</br>Example: `python3 google_saved_places_exporter.py . ./output_places`

#### Stage 1 (Required) - Per-list CSVs:
These are your custom lists - anything that's not 'Starred'.  

#### Stage 2 (Optional) - Saved Places.json:
Google won't export your **Starred** list directly (it's private/non-shareable), so Stage 2 reconstructs it from the merged export by eliminating the entries found in your custom lists.  

#### Stage 3 (Optional) - Resolve name-geocoded places precisely via their Maps URLs:
This is SLOW: each opens a real browser page and waits for Maps to resolve each entry (~8-10s per place, depending on machine/connection).  

| Input | If present | If missing |
|-------|-----------|------------|
| Stage 1: Per-list `*.csv` | This geocodes each list → `.geojson/.kml/.gpx` | Stage 1 produces nothing (silent — check your input folder) |
| Stage 2: `Saved Places.json` | This reconstructs the Starred/visited list | Stage 2 skipped cleanly |
| `.cache/` | Near-free if cache is reused. | Full API calls (writes cache for next time) |
| Stage 3: Playwright + Chromium | This will resolve name-geocoded pins via Maps URLs | Stage 3 prints install instructions and exits cleanly |

### Notes

- **Cache** absence never breaks a run, it just means that run pays the full API price (probably free), then caches the results for next time.
- **Stage dependency:** Stage 2 excludes places already matched in Stage 1's CSV output. If the merged file is present but the CSVs are **not**, Stage 2 has nothing to exclude against and will reverse-geocode the *entire* merged file as if all of it were Starred (no confirmed matches). Normal use (both present) avoids this.
- **Name-geocoding caveat:** Stage 1 places each pin by name, so an ambiguous name (e.g. "San José") can resolve to the wrong place — a bar in Dublin rather than the capital city of Costa Rica. Stage 3 is the fix: it re-resolves every pin from its actual Maps URL and corrects any that are >1km out. If you're skipping Stage 3, you can instead add a region to LIST_HINTS to bias Stage 1's guesses, or spot-fix the outliers by hand after import.
