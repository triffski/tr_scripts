# Bits 'n bobs

Somewhere to store random scripts, batch files and whatnot.

# Scripts & Batch Files
A couple of Python scripts for managing photos and metadata exported from Google and Meta platforms, for trasnfer to to Immich. Also a few time saver scripts for managing an Immich library.

|Name|Description|
|----|-----------|
| `tr_instagram_exif_encombinator.py`  | Instaram to Immich photo helper. Instagram strips EXIF on upload and keeps the real metadata (post dates, captions, GPS) in sidecar JSON. This reads `content/posts_*.json` and writes that metadata back into your downloaded photos with exiftool. <br><br>Grab your data in JSON format and make sure the script can access exiftool. Use `--dry-run` for one of those. |
| `tr_facebook_exif_encombinator.py` | Similar to the Instagram script, this embeds dates, captions, GPS and `source`/`batch` tags into copies via exiftool, retains folders and fixes mislabelled-extension files (JPEGs named .webp etc). |

## Instagram EXIF Encombinator

Helper for getting Instagram data into Immich more completely. Takes a **JSON** export from Instagram and tries to write a lot of the stripped metadata back to enrich the photos. Shame something similar can't be done for IG's image quality. 

Requires an Instagram export, `exiftool` and optionally `immich-go`.

What it does:
  - caption (post title) -> description → it falls back to media-level title too, so more accurately: caption (post or media title) -> description
  - post creation_timestamp -> DateTimeOriginal → also falls back to media-level timestamp, and for video it writes QuickTime CreateDate, not DateTimeOriginal. So: creation_timestamp -> DateTimeOriginal (photos) / QuickTime CreateDate (video)
  - GPS -> if present in exif_data (often stripped by IG)
  - tags -> instagram + a dated batch tag (default ig_2026_06)
  - Carousels: a post's caption + timestamp are fanned out across all its media.
  - Fixes Instagram's mislabelled files: media whose extension lies about the content
    (e.g. a JPEG named .webp or .heic) is renamed in the OUTPUT to match the real bytes,
    sniffed from magic numbers. Genuine WebP/HEIC/PNG files are left as-is. The source
    is never touched.
  - No comments - the export does not include comment threads on your posts.

**Input/output folders:**. 

  `--input`  is READ-ONLY. Nothing is ever written or renamed there.    
  `--output`  receives enriched COPIES, mirroring the source's media subfolder structure.  
  `--clean` wipes the test runs first and guarantee a fresh rebuild.  
  
**Usage:**   

Inspect the exported file structure first (no writes):  
```
python3 tr_instagram_exif_encombinator.py --input /data/ig_export/json --inspect
```
    
**Dry run a few items:**
```
python3 tr_instagram_exif_encombinator.py --input /data/ig_export/json --output /data/out --dry-run --limit 5
```
    
**Real run, wiping output first:**
```
python3 tr_instagram_exif_encombinator.py --input /data/ig_export/json --output /data/out --clean
```

## Facebook EXIF Encombinator

Similar to the Instagram script, this embeds dates, captions, GPS and `source`/`batch` tags into copies via exiftool, retains folders, fixes mislabelled-extension files, non-destructive `--input` → `--output` with `--clean`, progress readout.

Also:  
  - Preserves albums. It reads each album JSON and writes one output folder per album, ready for immich-go's `--folder-as-album`.
  - Date-prefixes album folders as `YYYY_MM_DD - Name` from the album's earliest photo, so albums sort chronologically by name in Immich.
  - Album From-date fallback — a photo with no date of its own inherits the album's earliest date instead of landing on today.
  - Prefers the original capture time (`taken_timestamp`) over upload time when Facebook kept it.
  - Optional album exclusion (`--exclude-album`), none by default.
  - Deliberately ignores last_modified_timestamp for dates — Facebook bulk-restamps it, so it's unreliable.

Requires a Facebook export, `exiftool` and optionally `immich-go`.

##  Using with immich-go:

Download `immich-go` and throw it in a folder along with the script(s). Update the following with your paths and API key, then point it at a freshly prepped folder:

```
cd /volume1/apps/_scripts

./immich-go upload from-folder \
  --server=http://localhost:2283 \
  --api-key=YOUR_KEY \
  --folder-as-album FOLDER \
  /volume1/backup/DataExports/DATASET/_working
```

- **YOUR_KEY** — fresh API key from the destination Immich account (permissions: `asset.copy`, `asset.delete`, `job.create`, `job.read` - or just all).
- **DATASET** — a folder prepped by the above scripts - `Facebook_2026_06_17`, `Instagram_2026_06_17` etc.
- The album line — this is the one that differs:  
    - Facebook → `--folder-as-album FOLDER` (one Immich album per date-prefixed folder, as above).  
    - Instagram → `--into-album "Instagram"`
