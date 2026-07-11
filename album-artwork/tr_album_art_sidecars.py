#!/usr/bin/env python3
"""
tr_album_art_sidecars.py

Add a per-folder cover.<ext> sidecar to album folders that lack one.
NEVER writes to audio files (no transcode possible by construction).

Phases:
  Phase 1  extract embedded art from a track  -> exact, offline, safe
  Phase 2  (opt-in, --fetch) MusicBrainz + Cover Art Archive lookup

Every generated image carries an in-file marker ("tr_cover_sidecars",
JPEG COM / PNG tEXt, purely additive, pixels untouched) AND is recorded
in a JSONL manifest with its sha256 -- so removal is exact + verifiable,
and files stay identifiable even if the manifest is lost.

Defaults to a dry run. --apply writes. --sample N estimates on a random
subset. Album == folder (first track carrying art wins for that folder).

Deps: none (pure stdlib -- runs on a bare python3)
"""

import argparse
import base64
import hashlib
import json
import os
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib


AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wv", ".alac", ".aiff", ".aif"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

EXISTING_COVER_NAMES = {
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "albumart.jpg", "albumart.jpeg", "albumart.png",
}

OUTPUT_BASENAME = "cover"
MARKER = b"tr_cover_sidecars"

MB_ENDPOINT = "https://musicbrainz.org/ws/2/release/"
CAA_FRONT = "https://coverartarchive.org/release/{mbid}/front"
MB_MIN_INTERVAL = 1.1
_last_mb_call = 0.0


def log(logf, msg):
    print(msg)
    if logf:
        logf.write(msg + "\n")
        logf.flush()


def mime_ext(m):
    return "png" if "png" in (m or "").lower() else "jpg"


def valid_art(data, ext, min_bytes):
    if not data or len(data) < min_bytes:
        return False
    if ext in ("jpg", "jpeg"):
        return data[:2] == b"\xff\xd8"
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext == "gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    return False


def inject_marker(data, ext):
    if ext == "jpg" and data[:2] == b"\xff\xd8":
        seg = b"\xff\xfe" + struct.pack(">H", len(MARKER) + 2) + MARKER
        return data[:2] + seg + data[2:]
    if ext == "png" and data[:8] == b"\x89PNG\r\n\x1a\n":
        ihdr_end = 8 + 4 + 4 + 13 + 4
        cdata = MARKER + b"\x00" + b"1"
        crc = zlib.crc32(b"tEXt" + cdata) & 0xffffffff
        chunk = struct.pack(">I", len(cdata)) + b"tEXt" + cdata + struct.pack(">I", crc)
        return data[:ihdr_end] + chunk + data[ihdr_end:]
    return data


def extract_embedded_art(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            if ext in (".flac", ".oga"):
                magic = f.read(4)
                if magic != b"fLaC":
                    f.seek(0); head = f.read(10)
                    if head[:3] == b"ID3":
                        size = head[6]<<21|head[7]<<14|head[8]<<7|head[9]
                        f.seek(10+size)
                    else:
                        f.seek(0)
                    if f.read(4) != b"fLaC":
                        return None
                while True:
                    hdr = f.read(4)
                    if len(hdr) < 4:
                        return None
                    btype = hdr[0] & 0x7F
                    last = hdr[0] & 0x80
                    length = int.from_bytes(hdr[1:4], "big")
                    if btype == 6:
                        block = f.read(length)
                        return _parse_flac_picture(block)
                    if last:
                        return None
                    f.seek(length, 1)

            if ext == ".mp3":
                head = f.read(10)
                if head[:3] != b"ID3":
                    return None
                ver = head[3]
                size = head[6]<<21|head[7]<<14|head[8]<<7|head[9]
                tag = f.read(size)
                return _parse_id3_apic(tag, ver)

            if ext in (".m4a", ".mp4", ".aac", ".alac"):
                return _parse_mp4_covr(f)

            if ext in (".ogg", ".opus"):
                data = f.read(400000)
                key = b"metadata_block_picture="
                i = data.lower().find(b"metadata_block_picture=")
                if i < 0:
                    return None
                start = i + len(key)
                b64 = bytearray()
                for c in data[start:]:
                    ch = bytes([c])
                    if ch in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=":
                        b64 += ch
                    else:
                        break
                try:
                    return _parse_flac_picture(base64.b64decode(bytes(b64)))
                except Exception:
                    return None
    except Exception:
        return None
    return None


def _mime_ext(m):
    m = (m or "").lower()
    if "png" in m: return "png"
    if "gif" in m: return "gif"
    return "jpg"


def _parse_flac_picture(block):
    try:
        p = 0
        p += 4  # type
        mlen = int.from_bytes(block[p:p+4], "big"); p += 4
        mime = block[p:p+mlen].decode("latin-1"); p += mlen
        dlen = int.from_bytes(block[p:p+4], "big"); p += 4
        desc = block[p:p+dlen]; p += dlen
        p += 16  # w,h,depth,colors
        pic_len = int.from_bytes(block[p:p+4], "big"); p += 4
        data = block[p:p+pic_len]
        if not data:
            return None
        ext = _mime_ext(mime)
        if data[:2] == b"\xff\xd8":
            ext = "jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        return data, ext
    except Exception:
        return None


def _id3_frame_size(sz, ver):
    if ver == 4 and all(b < 0x80 for b in sz):
        return sz[0] << 21 | sz[1] << 14 | sz[2] << 7 | sz[3]
    return int.from_bytes(sz, "big")


def _parse_id3_apic(tag, ver):
    try:
        p = 0
        n = len(tag)
        fid = b"APIC" if ver >= 3 else b"PIC"
        while p + (10 if ver >= 3 else 6) <= n:
            if ver >= 3:
                frame_id = tag[p:p+4]
                fsize = _id3_frame_size(tag[p+4:p+8], ver)
                p_body = p + 10
            else:
                frame_id = tag[p:p+3]
                fsize = int.from_bytes(tag[p+3:p+6], "big")
                p_body = p + 6
            if frame_id in (b"\x00\x00\x00\x00", b"\x00\x00\x00", b"\x00\x00"):
                break
            if fsize <= 0:
                break
            if frame_id == fid:
                body = tag[p_body:p_body+fsize]
                return _parse_apic_body(body, ver)
            p = p_body + fsize
        return None
    except Exception:
        return None


def _parse_apic_body(body, ver):
    try:
        enc = body[0]
        q = 1
        if ver >= 3:
            mend = body.find(b"\x00", q)
            mime = body[q:mend].decode("latin-1"); q = mend + 1
        else:
            mime = body[q:q+3].decode("latin-1"); q += 3
        q += 1  # picture type
        if enc in (1, 2):  # UTF-16 / UTF-16BE: description ends on aligned double-null
            while q + 1 < len(body):
                if body[q] == 0 and body[q + 1] == 0:
                    q += 2
                    break
                q += 2
        else:  # latin-1 / UTF-8: single-null terminator
            dend = body.find(b"\x00", q)
            q = (dend + 1) if dend >= 0 else len(body)
        data = body[q:]
        if not data:
            return None
        # fall back to sniffing the real format if the mime is odd/absent
        ext = _mime_ext(mime)
        if data[:2] == b"\xff\xd8":
            ext = "jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        return data, ext
    except Exception:
        return None


def _parse_mp4_covr(f):
    try:
        moov = _find_atom(f, b"moov", 0, _filesize(f))
        if not moov: return None
        udta = _find_atom(f, b"udta", moov[0], moov[1])
        if not udta: return None
        meta = _find_atom(f, b"meta", udta[0], udta[1])
        if not meta: return None
        ilst = _find_atom(f, b"ilst", meta[0] + 4, meta[1])
        if not ilst: return None
        covr = _find_atom(f, b"covr", ilst[0], ilst[1])
        if not covr: return None
        f.seek(covr[0])
        while f.tell() < covr[1]:
            hdr = f.read(8)
            if len(hdr) < 8: break
            dsize = struct.unpack(">I", hdr[:4])[0]
            dtype = hdr[4:8]
            if dtype == b"data":
                f.read(8)  # version/flags + reserved
                payload = f.read(dsize - 16)
                if not payload: return None
                if payload[:8] == b"\x89PNG\r\n\x1a\n":
                    return payload, "png"
                return payload, "jpg"
            f.seek(dsize - 8, 1)
        return None
    except Exception:
        return None


def _filesize(f):
    cur = f.tell(); f.seek(0, 2); n = f.tell(); f.seek(cur); return n


def _find_atom(f, want, start, end):
    f.seek(start)
    while f.tell() < end:
        pos = f.tell()
        hdr = f.read(8)
        if len(hdr) < 8: return None
        size = struct.unpack(">I", hdr[:4])[0]
        atype = hdr[4:8]
        hdrlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]; hdrlen = 16
        elif size == 0:
            size = end - pos
        if atype == want:
            return (pos + hdrlen, pos + size)
        f.seek(pos + size)
    return None

def read_album_tags(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            if ext == ".flac":
                if f.read(4) != b"fLaC":
                    return None, None
                artist = album = None
                while True:
                    hdr = f.read(4)
                    if len(hdr) < 4:
                        break
                    btype = hdr[0] & 0x7F
                    last = hdr[0] & 0x80
                    length = int.from_bytes(hdr[1:4], "big")
                    if btype == 4:
                        block = f.read(length)
                        p = 0
                        vlen = int.from_bytes(block[p:p+4], "little"); p += 4 + vlen
                        cnt = int.from_bytes(block[p:p+4], "little"); p += 4
                        for _ in range(cnt):
                            clen = int.from_bytes(block[p:p+4], "little"); p += 4
                            comment = block[p:p+clen].decode("utf-8", "replace"); p += clen
                            if "=" in comment:
                                k, v = comment.split("=", 1)
                                k = k.lower()
                                if k == "albumartist" and not artist:
                                    artist = v
                                elif k == "artist" and not artist:
                                    artist = v
                                elif k == "album":
                                    album = v
                        break
                    if last:
                        break
                    f.seek(length, 1)
                return artist, album
            if ext == ".mp3":
                head = f.read(10)
                if head[:3] != b"ID3":
                    return None, None
                ver = head[3]
                size = head[6]<<21|head[7]<<14|head[8]<<7|head[9]
                tag = f.read(size)
                def text_frame(fid4, fid3):
                    fid = fid4 if ver >= 3 else fid3
                    p = 0; n = len(tag)
                    while p + (10 if ver >= 3 else 6) <= n:
                        if ver >= 3:
                            frame_id = tag[p:p+4]
                            fsize = _id3_frame_size(tag[p+4:p+8], ver)
                            body = tag[p+10:p+10+fsize]; step = 10
                        else:
                            frame_id = tag[p:p+3]
                            fsize = int.from_bytes(tag[p+3:p+6], "big")
                            body = tag[p+6:p+6+fsize]; step = 6
                        if frame_id.startswith(b"\x00") or fsize <= 0:
                            break
                        if frame_id == fid and body:
                            enc = body[0]
                            raw = body[1:]
                            if enc in (1, 2):
                                txt = raw.decode("utf-16", "replace")
                            else:
                                txt = raw.decode("latin-1", "replace")
                            return txt.replace("\x00", "").strip()
                        p = p + step + fsize
                    return None
                artist = text_frame(b"TPE2", b"TP2") or text_frame(b"TPE1", b"TP1")
                album = text_frame(b"TALB", b"TAL")
                return artist, album
    except Exception:
        return None, None
    return None, None
def atomic_write(dst, data):
    tmp = dst + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def write_manifest(mf, entry):
    mf.write(json.dumps(entry) + "\n")
    mf.flush()


def mb_throttle():
    global _last_mb_call
    dt = time.time() - _last_mb_call
    if dt < MB_MIN_INTERVAL:
        time.sleep(MB_MIN_INTERVAL - dt)
    _last_mb_call = time.time()


def mb_find_release(artist, album, ua):
    q = 'artist:"%s" AND release:"%s"' % (artist, album)
    url = MB_ENDPOINT + "?query=" + urllib.parse.quote(q) + "&fmt=json&limit=1"
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    mb_throttle()
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.load(r)
    rels = data.get("releases") or []
    if not rels:
        return None
    top = rels[0]
    return top.get("id"), top.get("title"), top.get("score")


def caa_fetch_front(mbid, ua):
    req = urllib.request.Request(CAA_FRONT.format(mbid=mbid), headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=40) as r:
        ct = r.headers.get("Content-Type", "")
        data = r.read()
    return data, ("png" if "png" in ct.lower() else "jpg")


def load_cache(path):
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path, cache):
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, path)


def collect_albums(root):
    albums = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = os.path.basename(dirpath)
        if base.startswith("@") or base.startswith("."):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not (d.startswith("@") or d.startswith("."))]
        audio = sorted(f for f in filenames if os.path.splitext(f)[1].lower() in AUDIO_EXTS)
        if not audio:
            continue
        names_lower = {f.lower() for f in filenames}
        albums.append((dirpath, names_lower, audio))
    return albums


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="music library root (or a subfolder to scope it)")
    ap.add_argument("--apply", action="store_true", help="write sidecars (default: dry run)")
    ap.add_argument("--fetch", action="store_true", help="Phase 2 online lookup for folders with no embedded art")
    ap.add_argument("--min-bytes", type=int, default=1024, dest="min_bytes", help="reject embedded/fetched art smaller than this (drops truncated 1B junk). default 1024")
    ap.add_argument("--ua", default="", help='MusicBrainz User-Agent, e.g. "tr-covers/1.0 (you@triff.co)" (required with --fetch)')
    ap.add_argument("--sample", type=int, default=0, help="process only N randomly-chosen album folders (estimate mode)")
    ap.add_argument("--skip-if-any-image", action="store_true", dest="skip_any_image",
                    help="skip a folder if it already contains ANY image file, not just a recognised cover name")
    ap.add_argument("--log", default=os.path.expanduser("~/tr_album_art.log"))
    ap.add_argument("--manifest", default=os.path.expanduser("~/tr_album_art_manifest.jsonl"))
    ap.add_argument("--cache", default=os.path.expanduser("~/tr_album_art_mbcache.json"))
    args = ap.parse_args()

    if args.fetch and not args.ua:
        print("ERROR: --fetch requires --ua with a real contact string.")
        sys.exit(2)

    albums = collect_albums(args.root)
    total_found = len(albums)
    if args.sample and args.sample < total_found:
        albums = random.sample(albums, args.sample)

    logf = open(args.log, "a") if args.apply else None
    mf = open(args.manifest, "a") if args.apply else None
    cache = load_cache(args.cache) if args.fetch else {}

    st = {"folders": 0, "have": 0, "embedded": 0, "fetched": 0, "no_art": 0, "errors": 0}
    run_ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    for dirpath, names_lower, audio in albums:
        st["folders"] += 1

        has_recognised = any(n in EXISTING_COVER_NAMES for n in names_lower)
        has_any_image = any(os.path.splitext(n)[1] in IMAGE_EXTS for n in names_lower)
        if has_recognised or (args.skip_any_image and has_any_image):
            st["have"] += 1
            continue

        art = None
        for fn in audio:
            cand = extract_embedded_art(os.path.join(dirpath, fn))
            if cand and valid_art(cand[0], cand[1], args.min_bytes):
                art = cand
                break

        if art:
            data, ext = art
            data = inject_marker(data, ext)
            dst = os.path.join(dirpath, OUTPUT_BASENAME + "." + ext)
            st["embedded"] += 1
            log(logf, "EMBED  %s -> %s (%dB)" % (dirpath, os.path.basename(dst), len(data)))
            if args.apply:
                try:
                    atomic_write(dst, data)
                    write_manifest(mf, {"ts": run_ts, "phase": "embedded", "path": dst,
                                        "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                except Exception as e:
                    st["errors"] += 1
                    log(logf, "  ERROR write: %s" % e)
            continue

        if not args.fetch:
            st["no_art"] += 1
            log(logf, "MISS   %s (no embedded art)" % dirpath)
            continue

        artist, album = read_album_tags(os.path.join(dirpath, audio[0]))
        if not artist or not album:
            st["no_art"] += 1
            log(logf, "MISS   %s (no artist/album tags)" % dirpath)
            continue

        key = (artist + "\x1f" + album).lower()
        try:
            if key in cache:
                hit = cache[key]
            else:
                res = mb_find_release(artist, album, args.ua)
                hit = {"mbid": res[0], "title": res[1], "score": res[2]} if res else None
                cache[key] = hit
                save_cache(args.cache, cache)

            if not hit:
                st["no_art"] += 1
                log(logf, "MISS   %s (no MB release for '%s / %s')" % (dirpath, artist, album))
                continue

            data, ext = caa_fetch_front(hit["mbid"], args.ua)
            data = inject_marker(data, ext)
            dst = os.path.join(dirpath, OUTPUT_BASENAME + "." + ext)
            st["fetched"] += 1
            log(logf, "FETCH  %s -> %s  [matched '%s' score=%s %dB]"
                % (dirpath, os.path.basename(dst), hit.get("title"), hit.get("score"), len(data)))
            if args.apply:
                atomic_write(dst, data)
                write_manifest(mf, {"ts": run_ts, "phase": "fetched", "path": dst,
                                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                    "mbid": hit.get("mbid"), "matched_title": hit.get("title"),
                                    "score": hit.get("score")})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                st["no_art"] += 1
                log(logf, "MISS   %s (no CAA front image)" % dirpath)
            else:
                st["errors"] += 1
                log(logf, "  ERROR http %s: %s" % (e.code, dirpath))
        except Exception as e:
            st["errors"] += 1
            log(logf, "  ERROR %s: %s" % (dirpath, e))

    f = st["folders"]
    have_any = st["have"] + st["embedded"] + st["fetched"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    scope = "sample=%d of %d" % (f, total_found) if args.sample and args.sample < total_found else "all %d" % f
    lines = [
        "",
        "[%s | %s]" % (mode, scope),
        "  album folders scanned      : %d" % f,
        "  already have sidecar       : %d  (%.1f%%)" % (st["have"], pct(st["have"], f)),
        "  embedded art -> would make : %d  (%.1f%%)" % (st["embedded"], pct(st["embedded"], f)),
    ]
    if args.fetch:
        lines.append("  online fetch -> would make : %d  (%.1f%%)" % (st["fetched"], pct(st["fetched"], f)))
    lines += [
        "  NO art anywhere            : %d  (%.1f%%)" % (st["no_art"], pct(st["no_art"], f)),
        "  --------",
        "  HAVE art (any source)      : %d / %d  (%.1f%%)" % (have_any, f, pct(have_any, f)),
        "  errors                     : %d" % st["errors"],
    ]
    log(logf, "\n".join(lines))
    if logf:
        logf.close()
    if mf:
        mf.close()


if __name__ == "__main__":
    main()
