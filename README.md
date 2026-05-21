# StreemReplay01

Betfair replay dashboard / strategy research project.

## Repository state

Current fixed stable code state:

```text
repo:   https://github.com/SergeyPshenichnykh/StreemReplay01.git
branch: main
commit: c9f9a18
mode:   stable interactive replay dashboard
```

Base full snapshot tag:

```text
v0.1-full-snapshot
```

Current latest commit:

```text
c9f9a18 Fix stable interactive seek and compact full ladder columns
```

## Current stable interactive mode

Current working mode:

- stable terminal stream
- interactive page switching
- vertical scroll
- replay forward/back seek
- Under totals ladders
- Correct Score ladders
- full ladder queue columns
- Engine V2 overlay
- orders / fills / PnL page
- replay sample: `replay/football-pro-sample`

The current stable workflow uses:

```text
--stable-stream
--interactive
--list-totals-ladder
--totals-sticky
--engine-v2-overlay
--show-queue
```

## Main interactive launch command

Run from project root:

```bash
cd ~/projects/StreemReplay01

python scripts/replay_stream_selected_markets_dashboard_sticky_totals_engine_v2.py \
  --stable-stream \
  --stable-page totals \
  --interactive \
  --replay-file replay/football-pro-sample \
  --discover-targets \
  --start-minutes-before 10 \
  --self-check \
  --no-snapshots-csv \
  --list-totals-ladder \
  --totals-sticky \
  --totals-rows 3 \
  --ladder-max-rows 30 \
  --ticks-above 15 \
  --ticks-below 15 \
  --col-width 999 \
  --cs-cols 3 \
  --cadence-ms 1000 \
  --delay 0.02 \
  --balance 1000 \
  --engine-v2-overlay \
  --show-queue \
  --max-frames 1000
```

## Interactive keys

```text
1          totals page
2          correct_score page
3          orders page
a          all pages

j          vertical scroll down
k          vertical scroll up
PageDown   fast vertical scroll down
PageUp     fast vertical scroll up
Home / g   top
End        bottom

space      pause / resume
f          forward by 1 replay frame
b          back by 1 replay frame
q          quit
```

Important:

```text
Engine native forward key is n.
Stable wrapper exposes forward as f.
Stable wrapper exposes back as b.
```

## Current layout

### Totals page

```text
3 columns per visual row
30 ladder rows
Under 0.5 through Under 8.5
full ladder fields visible:
MYL / Q0 / Q1 / L / P / B / MYB / VOL / MAT
```

Markets:

```text
Under 0.5
Under 1.5
Under 2.5
Under 3.5
Under 4.5
Under 5.5
Under 6.5
Under 7.5
Under 8.5
```

### Correct Score page

```text
3 columns per visual row
20 ladder rows
full ladder fields visible:
MYL / Q0 / Q1 / L / P / B / MYB / VOL / MAT
```

### Orders page

```text
Engine V2 / active orders / fills / PnL status
```

## Notes

Use Windows Terminal or Ubuntu terminal with a wide window.

If columns are cut off, zoom out:

```text
Ctrl + -
```

Do not use `watch` for replay streaming. It restarts the script and repeats `FRAME: 1`.

Do not use `--smooth-ui` for this workflow. Use:

```text
--stable-stream
```

## Restore from GitHub clone

After cloning the repository, restore oversized files first:

```bash
cd ~/projects

git clone https://github.com/SergeyPshenichnykh/StreemReplay01.git

cd StreemReplay01

./restore_oversize_files.sh
```

This reconstructs oversized files from split parts and verifies SHA256.

Expected restored files include:

```text
_archives/streem_replay_ABSOLUTE_FULL_FINAL_20260521_030447.tar.gz
replay/delta_10s/action_log.csv
replay/delta_10s/price_level_delta.csv
```

## Oversized files

GitHub blocks normal git files larger than 100MB, and Git LFS is not available for this account because the LFS budget was exceeded.

Therefore files larger than 100MB are stored as split parts in:

```text
_oversize_split/
```

To restore them after clone:

```bash
cd StreemReplay01
./restore_oversize_files.sh
```

The restore script reconstructs the original oversized files and verifies SHA256.

## Safety archive

A full project archive was created from the original working project.

Archive location in the project after restore:

```text
_archives/streem_replay_ABSOLUTE_FULL_FINAL_20260521_030447.tar.gz
```

A copy was also placed in Windows Downloads when the archive was created:

```text
C:\Users\sergp\Downloads\streem_replay_ABSOLUTE_FULL_FINAL_20260521_030447.tar.gz
```

Recommended archive command for future full backups:

```bash
cd ~/projects

STAMP=$(date +%Y%m%d_%H%M%S)

tar -czf "streem_replay_ABSOLUTE_FULL_FINAL_${STAMP}.tar.gz" StreemReplay01

cp "streem_replay_ABSOLUTE_FULL_FINAL_${STAMP}.tar.gz" /mnt/c/Users/sergp/Downloads/
```

## Validation commands

Check syntax:

```bash
cd ~/projects/StreemReplay01

python -m py_compile \
  scripts/replay_stream_selected_markets_dashboard_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_stationary_totals_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_sticky_totals_engine_v2.py
```

Check git state:

```bash
git status
git log --oneline -5
```

Expected clean state:

```text
nothing to commit, working tree clean
```
