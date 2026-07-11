#!/usr/bin/env python3
"""
tr_album_art_scan.py

Fast whole-library bucket count for album-art planning.
Pure stdlib -- NO dependencies, runs on a bare python3 (incl. stor).

DEFAULT: filename-only. Zero file-content reads -> fast even over SMB.
  --probe-embedded : additionally read the tag header of ONE track in
                     each no-image folder to split "embedded art" from
                     "truly bare". Reads only tag bytes, not audio.

Buckets each audio folder:
  standard   : has cover/folder/front/albumart .jpg/.jpeg/.png
  promotable : has image(s), but none under a standard name
               (WMP AlbumArt_{GUID}_Large.jpg, harry.jpg, ...(front).jpg)
  no_image   : no image file at all
"""

import argparse
import os
import struct
import sys

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wv", ".alac", ".aiff", ".aif"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
STANDARD = {
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "albumart.jpg", "albumart.jpeg", "albumart.png",
}


def has_embedded_art(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            if ext in (".flac", ".oga"):
                magic = f.read(4)
                if magic != b"fLaC":
                    f.seek(0)
                    head = f.read(10)
                    if head[:3] == b"ID3":
                        size = head[6] << 21 | head[7] << 14 | head[8] << 7 | head[9]
                        f.seek(10 + size)
                    else:
                        f.seek(0)
                    if f.read(4) != b"fLaC":
                        return False
                while True:
                    hdr = f.read(4)
                    if len(hdr) < 4:
                        return False
                    btype = hdr[0] & 0x7F
                    last = hdr[0] & 0x80
                    length = int.from_bytes(hdr[1:4], "big")
                    if btype == 6:
                        return True
                    if last:
                        return False
                    f.seek(length, 1)

            if ext == ".mp3":
                head = f.read(10)
                if head[:3] != b"ID3":
                    return False
                size = head[6] << 21 | head[7] << 14 | head[8] << 7 | head[9]
                tag = f.read(size)
                return (b"APIC" in tag) or (b"PIC" in tag and head[3] == 2)

            if ext in (".m4a", ".mp4", ".aac", ".alac"):
                while True:
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        return False
                    size = struct.unpack(">I", hdr[:4])[0]
                    atype = hdr[4:8]
                    if size == 1:
                        size = struct.unpack(">Q", f.read(8))[0]
                        payload = size - 16
                    elif size == 0:
                        return atype == b"moov" and b"covr" in f.read()
                    else:
                        payload = size - 8
                    if atype == b"moov":
                        return b"covr" in f.read(payload)
                    if payload < 0:
                        return False
                    f.seek(payload, 1)

            if ext in (".ogg", ".opus"):
                return b"metadata_block_picture" in f.read(200000).lower()
    except Exception:
        return False
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--probe-embedded", action="store_true", dest="probe")
    ap.add_argument("--list-dir", default="", help="write promotable/bare folder lists into this directory")
    args = ap.parse_args()

    st = {"standard": 0, "promotable": 0, "no_image": 0, "embedded": 0, "bare": 0, "nonaudio": 0}
    promotable, bare = [], []

    for dirpath, dirnames, filenames in os.walk(args.root):
        base = os.path.basename(dirpath)
        if base.startswith("@") or base.startswith("."):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not (d.startswith("@") or d.startswith("."))]

        low = [f.lower() for f in filenames]
        audio = [f for f in low if os.path.splitext(f)[1] in AUDIO_EXTS]
        if not audio:
            st["nonaudio"] += 1
            continue

        if any(n in STANDARD for n in low):
            st["standard"] += 1
            continue
        if any(os.path.splitext(n)[1] in IMAGE_EXTS for n in low):
            st["promotable"] += 1
            promotable.append(dirpath)
            continue

        st["no_image"] += 1
        if args.probe:
            first_audio = sorted(f for f in filenames if os.path.splitext(f)[1].lower() in AUDIO_EXTS)[0]
            if has_embedded_art(os.path.join(dirpath, first_audio)):
                st["embedded"] += 1
            else:
                st["bare"] += 1
                bare.append(dirpath)
        else:
            bare.append(dirpath)

    audio_folders = st["standard"] + st["promotable"] + st["no_image"]
    print("audio folders                         : %d" % audio_folders)
    print("  standard cover (skip)               : %d" % st["standard"])
    print("  promotable (art under odd name)     : %d" % st["promotable"])
    print("  no image at all                     : %d" % st["no_image"])
    if args.probe:
        print("      embedded art -> phase 1         : %d" % st["embedded"])
        print("      TRULY BARE -> fetch/manual      : %d" % st["bare"])
    print("non-audio folders                     : %d" % st["nonaudio"])

    if args.list_dir:
        os.makedirs(args.list_dir, exist_ok=True)
        with open(os.path.join(args.list_dir, "promotable_folders.txt"), "w") as f:
            f.write("\n".join(promotable) + ("\n" if promotable else ""))
        with open(os.path.join(args.list_dir, "bare_folders.txt"), "w") as f:
            f.write("\n".join(bare) + ("\n" if bare else ""))
        print("\nlists written to %s" % args.list_dir)


if __name__ == "__main__":
    main()
