#!/usr/bin/env python3
"""Audit a music folder before tidying it. Read-only: never changes any file.

Finds:
  - low-bitrate lossy files (default: under 128 kbps)
  - albums split into several in a player because tracks lack a shared Album Artist
  - likely duplicate tracks, confirmed by audio fingerprint (needs fpcalc)
  - unreadable or untagged files

Usage (on OMV, as root):
  apt install -y python3-mutagen libchromaprint-tools
  python3 audit_music.py /path/to/Music --out /root/music-audit

Writes into --out:
  report.md          summary to read (and to share for review)
  tracks.csv         one row per file with format, bitrate and tags
  low_bitrate.csv    lossy files under the bitrate threshold
  split_albums.csv   albums whose tracks would show as several albums
  duplicates.csv     groups of the same recording in more than one file
  problems.csv       unreadable files and files with missing tags
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

try:
    import mutagen
except ImportError:
    sys.exit("mutagen is missing: apt install -y python3-mutagen")

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".oga", ".opus",
             ".wma", ".wav", ".aif", ".aiff", ".ape", ".wv", ".mpc"}
LOSSLESS_EXT = {".flac", ".wav", ".aif", ".aiff", ".ape", ".wv"}
VA_NAMES = {"various artists", "various", "va", "v a", "compilation"}


def first(tags, *keys):
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:  # noqa: BLE001 - some tag formats raise on odd keys
            v = None
        if v:
            v = v[0] if isinstance(v, (list, tuple)) else v
            v = str(v).strip()
            if v:
                return v
    return ""


def read_track(path: str) -> dict:
    row = {"path": path, "ext": Path(path).suffix.lower(), "size_mb": 0.0, "error": ""}
    try:
        row["size_mb"] = round(os.path.getsize(path) / 1048576, 2)
        audio = mutagen.File(path)
        if audio is None:
            row["error"] = "not recognised as audio"
            return row
        info = audio.info
        dur = float(getattr(info, "length", 0) or 0)
        br = int(getattr(info, "bitrate", 0) or 0)
        if not br and dur:  # e.g. FLAC: estimate from file size
            br = int(os.path.getsize(path) * 8 / dur)
        codec = str(getattr(info, "codec", "") or "").lower()
        lossless = row["ext"] in LOSSLESS_EXT or codec == "alac"
        row.update(duration=round(dur, 1), bitrate_kbps=round(br / 1000),
                   lossless=int(lossless),
                   sample_rate=int(getattr(info, "sample_rate", 0) or 0))
        tags = mutagen.File(path, easy=True)
        t = tags.tags if tags is not None and tags.tags is not None else {}
        row.update(
            artist=first(t, "artist"),
            albumartist=first(t, "albumartist", "album artist", "album_artist"),
            album=first(t, "album"),
            title=first(t, "title"),
            track=first(t, "tracknumber"),
            disc=first(t, "discnumber"),
            year=first(t, "date", "year", "originaldate"),
            compilation=first(t, "compilation"),
            mb_albumid=first(t, "musicbrainz_albumid"),
        )
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"[:200]
    return row


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\((?:[^)]*remaster[^)]*|\d{4} (?:mix|version))\)", " ", s)
    s = re.sub(r"\[(?:[^\]]*remaster[^\]]*)\]", " ", s)
    s = re.sub(r"\s-\s(?:\d{4}\s)?remaster(?:ed)?(?:\s\d{4})?.*$", " ", s)
    s = re.sub(r"\b(?:feat|ft|featuring)\.?\s.*$", " ", s)
    s = re.sub(r"^the\s", "", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def fingerprint(path: str):
    try:
        out = subprocess.run(["fpcalc", "-raw", "-json", "-length", "120", path],
                             capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return path, None
        return path, json.loads(out.stdout).get("fingerprint")
    except Exception:  # noqa: BLE001
        return path, None


def similarity(a, b) -> float:
    """Best bit-agreement between two raw chromaprints over small offsets."""
    if not a or not b:
        return 0.0
    best = 0.0
    for off in range(-6, 7):
        pairs = [(a[i], b[i + off]) for i in range(len(a)) if 0 <= i + off < len(b)]
        if len(pairs) < 20:
            continue
        diff = sum(bin((x ^ y) & 0xFFFFFFFF).count("1") for x, y in pairs)
        best = max(best, 1 - diff / (32 * len(pairs)))
    return best


def quality_key(r):
    return (r["lossless"], r["bitrate_kbps"], bool(r["albumartist"]), r["size_mb"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("music_dir")
    ap.add_argument("--out", default="music-audit")
    ap.add_argument("--min-kbps", type=int, default=128, help="lossy files below this are flagged (default 128)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--no-fingerprint", action="store_true", help="skip audio fingerprint confirmation of duplicates")
    a = ap.parse_args()

    root = Path(a.music_dir)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    files = [str(p) for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXT]
    other = Counter(p.suffix.lower() or "(none)" for p in root.rglob("*")
                    if p.is_file() and p.suffix.lower() not in AUDIO_EXT)
    print(f"Found {len(files)} audio files. Reading tags with {a.workers} workers...", flush=True)
    with ProcessPoolExecutor(a.workers) as ex:
        rows = []
        for i, r in enumerate(ex.map(read_track, files, chunksize=32), 1):
            rows.append(r)
            if i % 2000 == 0:
                print(f"  {i}/{len(files)}", flush=True)
    for r in rows:
        r["rel"] = os.path.relpath(r["path"], root)
        r["folder"] = os.path.dirname(r["rel"])

    good = [r for r in rows if not r["error"]]
    bad = [r for r in rows if r["error"]]

    # --- low bitrate (lossy only) ---
    low = sorted((r for r in good if not r["lossless"] and 0 < r["bitrate_kbps"] < a.min_kbps),
                 key=lambda r: (r["bitrate_kbps"], r["rel"]))

    # --- albums that would split ---
    split = []
    by_folder_album = defaultdict(list)
    for r in good:
        if r["album"]:
            by_folder_album[(r["folder"], r["album"])].append(r)
    for (folder, album), tr in by_folder_album.items():
        artists = {x["artist"] for x in tr if x["artist"]}
        aas = {x["albumartist"] for x in tr}
        if len(tr) >= 2 and (len(aas) > 1 or ("" in aas and len(artists) > 1)):
            missing = sum(1 for x in tr if not x["albumartist"])
            reason = ("album artist differs between tracks" if len(aas - {""}) > 1
                      else f"{missing} of {len(tr)} tracks have no album artist and the track artists differ")
            likely_va = len(artists) >= max(3, len(tr) // 2)
            split.append(dict(folder=folder, album=album, tracks=len(tr), track_artists=len(artists),
                              album_artists=" | ".join(sorted(x or "(blank)" for x in aas))[:200],
                              reason=reason, suggested_album_artist="Various Artists" if likely_va
                              else Counter(x["artist"] for x in tr).most_common(1)[0][0]))
    split.sort(key=lambda s: -s["tracks"])
    # same album title spread over several folders
    album_folders = defaultdict(set)
    for r in good:
        if r["album"]:
            album_folders[norm(r["album"]) + "|" + norm(r["albumartist"] or r["artist"])].add(r["folder"])
    multi_folder = {k: v for k, v in album_folders.items() if len(v) > 1}

    # --- duplicate candidates: same artist+title, similar length ---
    groups = defaultdict(list)
    for r in good:
        if r["title"]:
            groups[(norm(r["artist"] or r["albumartist"]), norm(r["title"]))].append(r)
    candidates = []
    for key, tr in groups.items():
        if len(tr) < 2 or not key[1]:
            continue
        tr.sort(key=lambda r: r.get("duration", 0))
        cluster = [tr[0]]
        for r in tr[1:]:
            if abs(r.get("duration", 0) - cluster[-1].get("duration", 0)) <= 3:
                cluster.append(r)
            else:
                if len(cluster) > 1:
                    candidates.append(cluster)
                cluster = [r]
        if len(cluster) > 1:
            candidates.append(cluster)

    fps = {}
    use_fp = not a.no_fingerprint
    if use_fp:
        try:
            subprocess.run(["fpcalc", "-version"], capture_output=True, check=True)
        except Exception:  # noqa: BLE001
            print("fpcalc not found (apt install -y libchromaprint-tools); skipping fingerprints.", flush=True)
            use_fp = False
    if use_fp and candidates:
        paths = sorted({r["path"] for c in candidates for r in c})
        print(f"Fingerprinting {len(paths)} possible duplicates...", flush=True)
        with ProcessPoolExecutor(a.workers) as ex:
            for i, (p, fp) in enumerate(ex.map(fingerprint, paths), 1):
                fps[p] = fp
                if i % 500 == 0:
                    print(f"  {i}/{len(paths)}", flush=True)

    dup_rows, n_groups, n_extra, wasted = [], 0, 0, 0.0
    for c in candidates:
        c = sorted(c, key=quality_key, reverse=True)
        keep = c[0]
        if use_fp:
            members = [keep] + [r for r in c[1:] if similarity(fps.get(keep["path"]), fps.get(r["path"])) >= 0.80]
            if len(members) < 2:
                continue
            confirm = "fingerprint match"
        else:
            members = c
            confirm = "tags and length only"
        n_groups += 1
        for j, r in enumerate(members):
            sim = round(similarity(fps.get(keep["path"]), fps.get(r["path"])), 3) if use_fp and j else ""
            dup_rows.append(dict(group=n_groups, action="KEEP" if j == 0 else "remove?",
                                 artist=r["artist"], title=r["title"], album=r["album"],
                                 bitrate_kbps=r["bitrate_kbps"], lossless=r["lossless"],
                                 duration=r["duration"], similarity=sim, confirmed_by=confirm, path=r["rel"]))
            if j:
                n_extra += 1
                wasted += r["size_mb"]

    # --- problems ---
    problems = [dict(path=r["rel"], problem=r["error"]) for r in bad]
    for r in good:
        miss = [k for k in ("artist", "title", "album") if not r[k]]
        if miss:
            problems.append(dict(path=r["rel"], problem="missing tags: " + ", ".join(miss)))

    # --- write files ---
    def write(name, data, fields):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(data)

    track_fields = ["rel", "ext", "bitrate_kbps", "lossless", "duration", "sample_rate", "size_mb", "artist",
                    "albumartist", "album", "title", "track", "disc", "year", "compilation", "mb_albumid", "error"]
    write("tracks.csv", rows, track_fields)
    write("low_bitrate.csv", low, ["bitrate_kbps", "ext", "artist", "album", "title", "rel"])
    write("split_albums.csv", split, ["folder", "album", "tracks", "track_artists", "album_artists", "reason",
                                      "suggested_album_artist"])
    write("duplicates.csv", dup_rows, ["group", "action", "artist", "title", "album", "bitrate_kbps", "lossless",
                                       "duration", "similarity", "confirmed_by", "path"])
    write("problems.csv", problems, ["path", "problem"])

    total_gb = sum(r["size_mb"] for r in rows) / 1024
    fmt = Counter(r["ext"] for r in good)
    br_bands = Counter()
    for r in good:
        if r["lossless"]:
            br_bands["lossless"] += 1
        else:
            b = r["bitrate_kbps"]
            br_bands["<128" if b < 128 else "128-191" if b < 192 else "192-255" if b < 256 else "256+"] += 1
    low_albums = Counter((r["artist"] or "?", r["album"] or r["folder"]) for r in low)
    no_aa = sum(1 for r in good if not r["albumartist"])

    lines = [
        "# Music library audit", "",
        f"Folder: `{root}`  ",
        f"Scanned {len(rows)} audio files ({total_gb:.1f} GB) in {time.time() - t0:.0f}s.", "",
        "## Formats", "",
        *[f"- {ext}: {n}" for ext, n in fmt.most_common()],
        *([f"- non-audio files: " + ", ".join(f"{e} ({n})" for e, n in other.most_common(8))] if other else []), "",
        "## Bitrate (lossy files)", "",
        *[f"- {band} kbps: {br_bands.get(band, 0)}" for band in ("<128", "128-191", "192-255", "256+")],
        f"- lossless: {br_bands.get('lossless', 0)}", "",
        f"## Low bitrate: {len(low)} files under {a.min_kbps} kbps "
        f"({sum(r['size_mb'] for r in low) / 1024:.1f} GB)", "",
        "Albums with the most low-bitrate tracks:", "",
        *[f"- {art} - {alb}: {n}" for (art, alb), n in low_albums.most_common(15)], "",
        f"## Albums that will split in the player: {len(split)}", "",
        f"{no_aa} of {len(good)} tracks have no Album Artist tag.", "",
        *[f"- {s['folder'] or '(top level)'} - \"{s['album']}\": {s['tracks']} tracks, "
          f"{s['track_artists']} artists ({s['reason']})" for s in split[:15]],
        "", f"Album titles spread across more than one folder: {len(multi_folder)}", "",
        f"## Duplicates: {n_groups} groups, {n_extra} extra copies ({wasted / 1024:.1f} GB)", "",
        f"Confirmed by: {'audio fingerprint (80%+ match)' if use_fp else 'tags and length only - not fingerprint-checked'}.",
        "In duplicates.csv the best copy (lossless, then highest bitrate) is marked KEEP.", "",
        *[f"- {d['artist']} - {d['title']}: {d['bitrate_kbps']} kbps {d['action']} ({d['path']})"
          for d in dup_rows[:20]], "",
        f"## Problems: {len(problems)} files", "",
        *[f"- {p['path']}: {p['problem']}" for p in problems[:15]], "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDone in {time.time() - t0:.0f}s. Report: {out / 'report.md'}")
    print(f"  low bitrate: {len(low)} | split albums: {len(split)} | duplicate groups: {n_groups} "
          f"({n_extra} extra copies) | problems: {len(problems)}")


if __name__ == "__main__":
    main()
