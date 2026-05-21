# StreemReplay01

Betfair replay dashboard / strategy research project.

## Current stable interactive mode

Current working mode:

- stable terminal stream
- interactive page switching
- Under totals ladders
- Correct Score ladders
- queue columns
- Engine V2 overlay
- orders / fills / PnL page
- replay sample: `replay/football-pro-sample`

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
  --cs-cols 5 \
  --cadence-ms 1000 \
  --delay 0.02 \
  --balance 1000 \
  --engine-v2-overlay \
  --show-queue \
  --max-frames 1000
```

## Interactive keys

```text
1      totals page: Under 0.5–8.5
2      correct_score page: Correct Score ladders
3      orders page: active orders / fills / PnL
a      all pages
space  pause / resume
q      quit
```

## Stable page layout

### Totals page

```text
5 columns
30 ladder rows
queue columns enabled with --show-queue
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
5 columns
20 ladder rows
queue columns enabled with --show-queue
```

### Orders page

Shows Engine V2 / active orders / fills / PnL related lines.

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

## Safety archive

Before creating this repository, a full project archive was created from the original working project.

Recommended archive command:

```bash
cd ~/projects

STAMP=$(date +%Y%m%d_%H%M%S)

tar -czf "streem_replay_ABSOLUTE_FULL_FINAL_${STAMP}.tar.gz" streem_replay

cp "streem_replay_ABSOLUTE_FULL_FINAL_${STAMP}.tar.gz" /mnt/c/Users/sergp/Downloads/
```

Expected archive location:

```text
~/projects/streem_replay_ABSOLUTE_FULL_FINAL_YYYYMMDD_HHMMSS.tar.gz
C:\Users\sergp\Downloads\streem_replay_ABSOLUTE_FULL_FINAL_YYYYMMDD_HHMMSS.tar.gz
```

## Oversized files restore

GitHub blocks normal git files larger than 100MB, and Git LFS is not available for this account because the LFS budget is exceeded.

Therefore files larger than 100MB are stored as split parts in:

```text
_oversize_split/
```

To restore them after clone:

```bash
cd StreemReplay01
./restore_oversize_files.sh
```

This reconstructs the original oversized files and verifies SHA256.
