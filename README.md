# Music tidy-up with beets (OMV)

Tidies the music collection on OMV: correct tags from MusicBrainz, one album per
album (no more compilations split by artist), cover art, a clean
`Artist/Album/Track` layout, and low-bitrate files and duplicates moved out of the way.

**Safety first:** beets never touches the original `Music` folder (it is mounted
read-only). It builds a new copy in `Music-tidy`. Nothing is deleted: rejects go to
`Music-quarantine` for you to review. The Fedora server (192.168.1.200) keeps its copy
until you're finished.

| Folder on OMV (under `.../plexmedia/`) | In the container | What |
|---|---|---|
| `Music` | `/music` (read-only) | Original collection |
| `Music-tidy` | `/music-tidy` | Tidied copy, for Navidrome |
| `Music-quarantine` | `/quarantine` | Low-bitrate files and extra duplicates |

## 1. Audit the collection (read-only)

```
apt install -y python3-mutagen libchromaprint-tools git
git clone https://github.com/phildrouet/beets.git /opt/beets
cd /opt/beets
python3 scripts/audit_music.py /srv/dev-disk-by-uuid-5e5c1043-1071-4ab9-a394-dc66545e4ee7/plexmedia/Music --out /root/music-audit
```

It writes `report.md` plus CSV files to `/root/music-audit`: low-bitrate files (under
128 kbps), albums that split in the player, duplicates confirmed by audio fingerprint,
and unreadable or untagged files. Share `report.md` (and the CSVs if needed) with
Claude to plan the clean-up.

## 1b. Clean up using the audit

`scripts/cleanup_music.py` acts on the audit results. It deletes only macOS junk
(`._*` and `.DS_Store`) and **moves** everything else into `Music-quarantine`, sorted
into `duplicates/`, `low-bitrate/`, `ccr-editions/`, `drm/` (iTunes `.m4p`) and
`broken/`. Duplicates keep the best-quality copy and, at similar quality, the copy on
the original album rather than a compilation or single. Close-but-different masters
(fingerprint match under 0.85) are left alone. For the Creedence box set, one edition
per album is kept (40th Anniversary, else 20-bit K2).

```
cd /opt/beets && git pull
python3 scripts/cleanup_music.py --audit /root/music-audit            # dry run: shows the plan
less /root/music-audit/cleanup-plan.tsv                               # optional: check the list
python3 scripts/cleanup_music.py --audit /root/music-audit --apply    # do it
```

To undo, run the command it prints at the end (`--undo <manifest>`). After the clean-up,
steps 5 and 6 below are just a mop-up for anything beets finds that the audit missed.

## 2. Start beets

```
P=/srv/dev-disk-by-uuid-5e5c1043-1071-4ab9-a394-dc66545e4ee7/plexmedia
mkdir -p $P/Music-tidy $P/Music-quarantine
chown 1000:100 $P/Music-tidy $P/Music-quarantine
cd /opt/beets && docker compose up -d
```

Run beets commands as the container's own user so files are owned correctly. Add this
shortcut (to `/root/.bashrc` to keep it):

```
alias beet='docker exec -it -u abc -e BEETSDIR=/config beets beet'
```

Check it works: `beet version` and `beet config -p`.

## 3. First pass: automatic import

Matches every album against MusicBrainz. Confident matches get corrected tags; anything
uncertain is copied with its existing tags (nothing is skipped or lost). Run in `tmux`:
it can take several hours, because MusicBrainz allows about one lookup per second.

```
tmux new -s beets
beet import -q /music
```

Detach with Ctrl+B then D, and reattach with `tmux attach -t beets`. If it stops, run the
same command again: already-imported folders are skipped.

## 4. Fix the albums that didn't match

```
beet ls -a -f '$albumartist - $album' mb_albumid::^$ | sort > /root/unmatched.txt
wc -l /root/unmatched.txt
```

Re-match them interactively. beets shows its best guesses for each album; pick one, search
by name, or keep as-is:

```
beet import -L mb_albumid::^$
```

For a compilation still split into several albums, fix it by hand:

```
beet modify -a albumartist="Various Artists" comp=1 album:"Album Name"
```

## 5. Quarantine low-bitrate files (under 128 kbps)

```
beet ls -f '$bitrate  $albumartist - $album - $title' bitrate:..127999
beet move -d /quarantine bitrate:..127999
beet remove -f path:/quarantine
```

## 6. Quarantine duplicates

**Whole duplicate albums first** (e.g. the same album as MP3 and as FLAC, which show up
as `Album [1]` and `Album [2]`). List them and decide which copy to keep. The audit's
`duplicates.csv` and Claude can help:

```
beet duplicates -a -f '$albumartist - $album'
```

For each one, list the copies with their ID numbers, and check the format and bitrate of
each copy's tracks:

```
beet ls -a -f '$id  $albumartist - $album  $path' album:"Album Name"
beet ls -f '$format $bitrate  $title' album_id:12
```

Quarantine the copy you don't want by its ID:

```
beet move -a -d /quarantine id:12
beet remove -a -f path:/quarantine
```

**Then individual tracks.** beets finds duplicates by MusicBrainz **recording ID**, so live versions, remixes and
re-recordings are not treated as duplicates. Albums still without a MusicBrainz match
have no recording ID and are ignored here: use the audit's `duplicates.csv` for those.
The best copy (highest bitrate, so lossless wins) is kept.

```
beet duplicates -f '$bitrate  $path'
beet duplicates -m /quarantine
beet remove -f path:/quarantine
beet move
```

`beet move` (with no arguments) re-files everything using the folder layout, which tidies
names like `Album [2]` left over from duplicate albums.

## 7. Switch Navidrome to the tidied copy

In `/opt/navidrome/docker-compose.yml`, change the music volume from `.../Music` to
`.../Music-tidy`, then:

```
cd /opt/navidrome && docker compose up -d
```

In Navidrome, run a full scan. Check albums, compilations and artwork look right.

## 8. Finish

After a week or so of happy listening:
- look through `Music-quarantine` for anything you want back, then delete it
- delete the original `Music` folder
- keep a backup of `Music-tidy` somewhere other than OMV's media drive before retiring the
  Fedora server's copy

## Adding new music later

Put new albums anywhere under `Music` (e.g. a `Music/incoming` folder) and run
`beet import -q /music`. Only new folders are processed.

## Notes

- Folder layout: `Albumartist/Album/Track Title`, compilations under `Compilations/`,
  single tracks under `Non-Album/Artist/`. Multi-disc albums get `Disc-Track` names.
- Files with no tags at all end up in a folder named `_`. Fix them with `beet edit` or by
  re-importing with a manual search.
- Config: `config/config.yaml`. The library database and logs in `config/` are not stored
  in git.
