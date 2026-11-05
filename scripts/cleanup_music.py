#!/usr/bin/env python3
"""Clean up the music folder using the audit results.

Nothing is deleted except macOS junk files (._* and .DS_Store). Everything else is
MOVED into the quarantine folder, keeping its folder structure, under one of:

  duplicates/    extra copies (the best copy stays in Music)
  low-bitrate/   lossy files under 128 kbps
  ccr-editions/  surplus Creedence remasters (one edition per album is kept)
  drm/           iTunes .m4p files, which Navidrome can't play
  broken/        files that can't be read as audio

Every move is written to quarantine/manifest-<time>.tsv, and --undo puts them back.

Runs as a dry run unless you add --apply.

  python3 scripts/cleanup_music.py --audit /root/music-audit            # preview
  python3 scripts/cleanup_music.py --audit /root/music-audit --apply    # do it
  python3 scripts/cleanup_music.py --undo /path/to/manifest-....tsv     # reverse
"""
import argparse
import csv
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

BASE = "/srv/dev-disk-by-uuid-5e5c1043-1071-4ab9-a394-dc66545e4ee7/plexmedia"

# Duplicate pairs below this fingerprint similarity are usually a different master or
# edit of the song (single vs album, remaster vs original), so they are left alone.
MIN_SIMILARITY = 0.85

# Folders whose "duplicates" are deliberate and shouldn't be touched by the duplicate rule.
SKIP_DUPLICATES_UNDER = (
    "Educational Tracks/",  # test CD: tracks 74/75 are different test signals
    "Creedence Clearwater Revival - Remastered Discography",  # handled by the edition rule
)

CCR_ROOT = "Creedence Clearwater Revival - Remastered Discography 1968-1980 [FLAC]/"
# Order of preference for CCR editions: the first one present for an album is kept.
CCR_PREFERENCE = ("40th Anniversary", "20-Bit K2", "SACD Hybrid")


COMPILATION_HINTS = re.compile(
    r"compilation|singles?\b|greatest hits|best of|the essential|collection|chronicle|"
    r"now that|now thats|live lounge|mash up|rarities|time flies|stop the clocks|masterplan|"
    r"dreamboats|hits", re.I)


def is_junk(p: Path):
    return p.name.startswith("._") or p.name.lower() == ".ds_store"


def preference(r):
    """Higher is better: quality tier first, then original album over compilation/single."""
    kbps = float(r["bitrate_kbps"] or 0)
    tier = 3 if r["lossless"] == "1" else 2 if kbps >= 256 else 1 if kbps >= 192 else 0
    on_album = 0 if COMPILATION_HINTS.search(os.path.dirname(r["path"])) else 1
    return (tier, on_album, kbps, r["action"] == "KEEP")


def ccr_moves(music: Path):
    """Return {relpath: reason} for CCR album editions that aren't the preferred one."""
    root = music / CCR_ROOT
    out = {}
    if not root.is_dir():
        return out
    for album in sorted(root.iterdir()):
        if not album.is_dir() or album.name == "Extras":
            continue
        editions = [e for e in album.iterdir() if e.is_dir()]
        ranked = []
        for e in editions:
            rank = next((i for i, k in enumerate(CCR_PREFERENCE) if k in e.name), None)
            if rank is not None:
                ranked.append((rank, e))
        if len(ranked) < 2:
            continue
        ranked.sort(key=lambda t: t[0])
        keep = ranked[0][1]
        for _, e in ranked[1:]:
            for f in e.rglob("*"):
                if f.is_file() and not is_junk(f):
                    out[str(f.relative_to(music))] = ("ccr-editions", f"kept {keep.name}")
    return out


def plan(music: Path, audit: Path):
    moves = {}  # relpath -> (bucket, note)

    def add(rel, bucket, note):
        if rel not in moves:
            moves[rel] = (bucket, note)

    # 1. Creedence editions
    for rel, v in ccr_moves(music).items():
        add(rel, *v)

    # 2. Broken files (ignoring the macOS ._ files, which are deleted separately)
    with open(audit / "problems.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if os.path.basename(r["path"]).startswith("._"):
                continue
            if not r["problem"].startswith("missing tags"):
                add(r["path"], "broken", r["problem"])

    # 3. Low bitrate
    with open(audit / "low_bitrate.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            add(r["rel"], "low-bitrate", f'{r["bitrate_kbps"]} kbps')

    # 4. Duplicates: move every non-KEEP copy whose fingerprint closely matches
    groups = defaultdict(list)
    with open(audit / "duplicates.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            groups[r["group"]].append(r)
    for rows in groups.values():
        candidates = [r for r in rows if r["path"] not in moves]
        if len(candidates) < 2:
            continue
        keep = max(candidates, key=preference)
        ref = next((r for r in rows if r["action"] == "KEEP"), keep)
        for r in candidates:
            if r is keep:
                continue
            if r["path"].startswith(SKIP_DUPLICATES_UNDER) or keep["path"].startswith(SKIP_DUPLICATES_UNDER):
                continue
            # similarity in the CSV is measured against the audit's KEEP copy
            sim = 1.0 if r is ref else float(r["similarity"] or 0)
            if keep is not ref:
                sim = min(sim, float(keep["similarity"] or 0))
            if sim < MIN_SIMILARITY:
                continue
            add(r["path"], "duplicates", f'copy of {keep["path"]} ({keep["bitrate_kbps"]} kbps, match {sim:.2f})')

    # 5. DRM-protected iTunes files
    for f in music.rglob("*.m4p"):
        if not f.name.startswith("._"):
            add(str(f.relative_to(music)), "drm", "iTunes DRM")

    # Drop anything that no longer exists (already moved, renamed, etc.)
    return {k: v for k, v in moves.items() if (music / k).is_file()}


def junk_files(music: Path):
    return [p for p in music.rglob("*") if p.is_file() and is_junk(p)]


def remove_empty_dirs(root: Path):
    removed = 0
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def undo(manifest: Path):
    n = 0
    with open(manifest, encoding="utf-8") as fh:
        for row in csv.reader(fh, delimiter="\t"):
            if row[0] == "bucket":
                continue
            _, src, dst, _ = row
            if Path(dst).is_file() and not Path(src).exists():
                Path(src).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(dst, src)
                n += 1
    print(f"Moved {n} files back.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audit", help="folder with the audit CSVs (from audit_music.py)")
    ap.add_argument("--music", default=f"{BASE}/Music")
    ap.add_argument("--quarantine", default=f"{BASE}/Music-quarantine")
    ap.add_argument("--apply", action="store_true", help="actually move/delete (default is a dry run)")
    ap.add_argument("--undo", metavar="MANIFEST", help="move files listed in a manifest back")
    a = ap.parse_args()

    if a.undo:
        undo(Path(a.undo))
        return
    if not a.audit:
        ap.error("--audit is required")

    music, quarantine, audit = Path(a.music), Path(a.quarantine), Path(a.audit)
    if not music.is_dir():
        sys.exit(f"Music folder not found: {music}")

    junk = junk_files(music)
    moves = plan(music, audit)

    by_bucket = Counter(b for b, _ in moves.values())
    size = defaultdict(int)
    for rel, (b, _) in moves.items():
        size[b] += (music / rel).stat().st_size

    print(f"macOS junk files to delete: {len(junk)}")
    print("Files to move to quarantine:")
    for b in ("duplicates", "low-bitrate", "ccr-editions", "drm", "broken"):
        print(f"  {b:13} {by_bucket[b]:5}  {size[b] / 1e9:5.1f} GB")
    print(f"  {'total':13} {len(moves):5}  {sum(size.values()) / 1e9:5.1f} GB")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    listing = quarantine / f"plan-{stamp}.tsv" if a.apply else audit / "cleanup-plan.tsv"
    listing.parent.mkdir(parents=True, exist_ok=True)
    with open(listing, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["bucket", "path", "note"])
        for rel, (b, note) in sorted(moves.items(), key=lambda kv: (kv[1][0], kv[0])):
            w.writerow([b, rel, note])
    print(f"\nFull list: {listing}")

    if not a.apply:
        print("\nDry run - nothing changed. Check the list, then run again with --apply.")
        return

    for p in junk:
        p.unlink()

    manifest = quarantine / f"manifest-{stamp}.tsv"
    done = 0
    with open(manifest, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["bucket", "from", "to", "note"])
        for rel, (b, note) in sorted(moves.items()):
            src = music / rel
            if not src.is_file():
                continue
            dst = quarantine / b / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = dst.with_name(f"{dst.stem}.{stamp}{dst.suffix}")
            shutil.move(str(src), str(dst))
            w.writerow([b, str(src), str(dst), note])
            fh.flush()
            done += 1
    empty = remove_empty_dirs(music)
    print(f"\nDeleted {len(junk)} junk files, moved {done} files, removed {empty} empty folders.")
    print(f"Undo with: python3 {sys.argv[0]} --undo {manifest}")


if __name__ == "__main__":
    main()
