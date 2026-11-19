# Immich Helpers

Time saver scripts for managing an Immich library through its API - tagging, untagging and tidying jobs that would be very tedious by hand. All standard library Python 3.9+, no dependencies.

|Name|Description|
|----|-----------|
| `tr_immich_source_tagger.py` | Adds a flat provenance tag (`src_google_2026_06`) and a nested album tag (`G/<album>`) to every asset in every album. Fills the gap left by immich-go's `from-google-photos`, which recreates album membership but writes no album tags. |
| `tr_immich_tag_from_tag.py` | Applies a tag to every asset that already carries another tag. E.g. copy immich-go's auto `takeout-<timestamp>` tag over to `src_google_2026_06`, covering the whole import rather than just album members. |
| `tr_immich_untag_album.py` | Removes one or more tags from the assets of a named album. The inverse of the source tagger, for undoing a mis-tag. |
| `tr_immich_empty_album_cull.py` | Deletes albums with zero assets. Can never touch an album that still has photos in it. |
| `tr_immich_library_count.py` | Counts assets in an external library and tallies them by file extension, to spot phantom `.xmp`/`.json` imports. Lists libraries if no `--library` is given. |

## Common to all

- **Dry run by default.** Nothing changes without `--yes` (except `library_count`, which is read-only anyway).
- **API key:** `--api-key`, else the `IMMICH_API_KEY` env var. `library_count` also falls back to `~/.immich_key`.
- **The multi-account trap:** if `IMMICH_API_KEY` holds one account's key and you want to act on another, pass `--api-key` explicitly - otherwise you'll silently operate on the env var's account.
- **Server:** `--server` is required, e.g. `http://localhost:2283` (`library_count` defaults to that).

## Source Tagger

Resulting tags per asset: `src_<source>_<batch>` (flat) + `<prefix>/<album>` (nested). Defaults suit Google - override `--prefix` and `--source` for other sources.

Run it straight after the relevant import, before importing the next source, so only that source's albums exist.

  - `--prefix P` - album tag parent, default `G`
  - `--source NAME` - default `google`
  - `--batch SUFFIX` / `--no-batch` - batch suffix on the flat tag, or none for continuous sources like phone sync
  - `--album-name NAME` - scope to one album
  - `--exclude-album NAME` - skip an album (repeatable), e.g. keep phone "Camera" out of a Google run

```
python3 tr_immich_source_tagger.py --server http://localhost:2283 --yes
python3 tr_immich_source_tagger.py --server http://localhost:2283 --prefix F --source facebook --yes
```

## Tag From Tag

```
python3 tr_immich_tag_from_tag.py --server http://localhost:2283 \
    --from-tag takeout-20260617T022516Z --to-tag src_google_2026_06 --yes
```

`--to-tag` is created if it doesn't exist (hierarchical if it contains `/`). Idempotent, safe to re-run.

## Untag Album

```
python3 tr_immich_untag_album.py --server http://localhost:2283 \
    --album-name "Camera" --tag src_google_2026_06 --tag "G/Camera" --yes
```

**Shared-asset caveat:** an asset that's also in another album legitimately carrying the tag gets stripped too. Re-run the source tagger over the legitimate albums (with `--exclude-album` for this one) to restore them.

## Empty Album Cull

```
python3 tr_immich_empty_album_cull.py --server http://localhost:2283 --yes
```

Only the album container is removed - assets are never deleted.

## Library Count

```
python3 tr_immich_library_count.py --library <library-id>
```
