# StreemReplay01

Betfair replay dashboard / strategy research project.

## Repository state

Current stable code state:

```text
repo:   https://github.com/SergeyPshenichnykh/StreemReplay01.git
branch: main
commit: 3d18832
mode:   stable interactive replay dashboard with maker Under LAY grid simulation
```

Current latest commit:

```text
3d18832 Add compact per-runner maker grid PnL
```

Recent stable commits:

```text
3d18832 Add compact per-runner maker grid PnL
1d9e079 Fix maker grid account overlay and compact totals layout
dde989c Improve stable replay seek frame repaint
8a774b2 Add maker grid FIFO matching simulation
0cc05a6 Add adaptive maker Under lay grid baseline
```

Base full snapshot tag:

```text
v0.1-full-snapshot
```

## Current stable interactive mode

Current working mode:

```text
stable terminal stream
interactive page switching
vertical scroll
replay forward/back seek
Under totals ladders
Correct Score ladders
full ladder queue columns
Engine V2 overlay
one-time maker Under LAY grid
FIFO matching simulation for maker grid
clean maker-grid account overlay
compact totals layout
compact per-runner PnL display
replay sample: replay/football-pro-sample
```

The current stable workflow uses:

```text
--stable-stream
--interactive
--list-totals-ladder
--totals-sticky
--engine-v2-overlay
--show-queue
--maker-under-lay-grid
--maker-under-lay-grid-match
```

## Setup

Run from project root.

Activate virtualenv:

```bash
cd ~/projects/StreemReplay01

source .venv/bin/activate
```

Prompt should look like:

```text
(.venv) nafanya@NitroAN51555:~/projects/StreemReplay01$
```

If the virtualenv is not active, `python` may not exist. Either activate `.venv` or use `python3`.

## Main interactive launch command

Recommended current command:

```bash
cd ~/projects/StreemReplay01

source .venv/bin/activate

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
  --maker-under-lay-grid \
  --maker-under-lay-grid-match
```

Run faster replay, approximately 5x faster:

```bash
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
  --delay 0.004 \
  --balance 1000 \
  --engine-v2-overlay \
  --show-queue \
  --maker-under-lay-grid \
  --maker-under-lay-grid-match
```

Run with explicit frame limit:

```text
--max-frames 1000
```

Default behavior:

```text
--max-frames 0 means unlimited
```

So if `--max-frames` is omitted, replay continues until the replay file ends.

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

## Current dashboard layout

### Totals page

```text
3 columns per visual row
30 ladder rows
Under 0.5 through Under 8.5
compact auto-width totals layout
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

The orders page is not the primary workflow right now. Current focus is the totals ladder with maker-grid simulation and account/PnL visualization.

## Ladder columns

Current ladder column meaning:

```text
MYL   our maker LAY order size
Q0    queue ahead before our order
Q1    queue ahead + our remaining order size
L     market BACK-side size shown as Lay-action column
P     price
B     market LAY-side size shown as Back-action column
MYB   our maker BACK order size
VOL   traded volume at price
MAT   matched amount / matched liability display
```

For current maker-grid mode:

```text
MYL = one-time maker Under LAY grid
MYB = empty
VOL = visible
MAT = matched / liability display
```

## Maker Under LAY grid

Enable maker grid:

```text
--maker-under-lay-grid
```

Enable FIFO matching simulation:

```text
--maker-under-lay-grid-match
```

Current behavior:

```text
one-time grid placement
stationary grid
grid does not move when price moves
grid does not re-place dynamically after price recovery
matching is simulated FIFO-style
matched state is tracked per runner and per price
```

The grid is shown in `MYL`.

Queue columns:

```text
Q0 = queue ahead
Q1 = queue ahead + our remaining order size
```

## Maker-grid matching model

Current matching model is conservative and LAY-only.

For each matched LAY order:

```text
matched_stake = matched stake
matched_liability = matched_stake * (price - 1)
```

For clean maker-grid account overlay:

```text
locked = open_liability + matched_liability
free = balance - locked
pnl_proxy = -matched_liability
```

This means:

```text
free must not become larger than balance because of matched LAY exposure
pnl_proxy is negative while all matched LAY exposure is treated as current worst-case liability
legacy ENGINE_V2 positive pnl/free is ignored in clean maker-grid mode
```

## Engine V2 overlay

Current overlay includes clean maker-grid account values:

```text
ENGINE_V2: active=0 next10s=0 locked=... free=... pnl_proxy=... NEXT=- maker_grid_active=... maker_matching=ON maker_matched=... maker_matched_liability=... maker_liability=...
```

Meaning:

```text
maker_grid_active       open maker-grid orders
maker_matching          FIFO matching switch
maker_matched           total matched stake
maker_matched_liability total matched LAY liability
maker_liability         remaining open LAY liability
locked                  maker_liability + maker_matched_liability
free                    balance - locked
pnl_proxy               -maker_matched_liability
```

Expected invariant:

```text
locked = maker_liability + maker_matched_liability
free = balance - locked
pnl_proxy = -maker_matched_liability
```

Example:

```text
balance = 1000.00
maker_liability = 250.55
maker_matched_liability = 60.17

locked = 310.72
free = 689.28
pnl_proxy = -60.17
```

## Per-runner PnL

Each runner ladder now shows compact per-runner PnL in the header.

Example:

```text
Under 4.5 Goals    PNL -17.42
center=mid:1.15 ltp=1.17
```

Current per-runner PnL proxy:

```text
runner_pnl = -matched_liability_for_this_runner
```

This is conservative and LAY-only.

If a runner has no matched maker-grid liability:

```text
PNL +0.00
```

## Stable replay seek

Forward/back seek exists in stable interactive mode:

```text
f = forward one frame
b = backward one frame
```

The stable wrapper has explicit frame-end handling so seek repaint is improved.

Known UI limitation:

```text
backward seek can still feel imperfect in some terminal states
engine state is the source of truth
resume can reveal the actual seeked state if terminal repaint lags
```

Do not change matching logic just to fix repaint behavior.

## Terminal notes

Use Windows Terminal or Ubuntu terminal with a wide window.

If columns are cut off, zoom out:

```text
Ctrl + -
```

Recommended:

```text
wide terminal
small font
no watch command
no smooth-ui for this workflow
```

Do not use `watch` for replay streaming. It restarts the script and repeats `FRAME: 1`.

Do not use `--smooth-ui` for the current stable workflow. Use:

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

Recommended local backup before risky patches:

```bash
cd ~/projects/StreemReplay01

mkdir -p backups

tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  -czf backups/streemreplay01_before_patch_$(date +%Y%m%d_%H%M%S).tar.gz .
```

## Validation commands

Check syntax:

```bash
cd ~/projects/StreemReplay01

source .venv/bin/activate

python -m py_compile \
  scripts/replay_stream_selected_markets_dashboard_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_stationary_totals_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_sticky_totals_engine_v2.py
```

If `.venv` is not active:

```bash
python3 -m py_compile \
  scripts/replay_stream_selected_markets_dashboard_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_stationary_totals_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_sticky_totals_engine_v2.py
```

`py_compile` can modify `__pycache__`. Clean it before commit:

```bash
git restore -- scripts/__pycache__
```

Check git state:

```bash
git status
git log --oneline -5
```

Expected clean state:

```text
On branch main
Your branch is up to date with 'origin/main'.

nothing to commit, working tree clean
```

## Commit workflow

Use explicit files only.

Do not use:

```bash
git add -A
```

Recommended:

```bash
cd ~/projects/StreemReplay01

python -m py_compile \
  scripts/replay_stream_selected_markets_dashboard_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_stationary_totals_engine_v2.py \
  scripts/replay_stream_selected_markets_dashboard_sticky_totals_engine_v2.py

git restore -- scripts/__pycache__

git status --short

git add scripts/replay_stream_selected_markets_dashboard_engine_v2.py

git commit -m "Describe change"

git push origin main

git status
git log --oneline -5
```

## Current development direction

Current completed baseline:

```text
stable totals dashboard
stationary maker Under LAY grid
FIFO matching simulation
clean account overlay
compact totals layout
compact per-runner maker-grid PnL
```

Next planned areas:

```text
trade simulation reporting
cost calculations
potential win calculations
runner-level exposure views
settlement model
final PnL after score/result
better seek/repaint behavior if needed
```

## Second-leg CS recovery shadow regression

Latest validated flow:

- CS bucket recovery can produce safe recovery previews.
- Pure TAKER recovery may shadow-execute only after place-delay.
- HYBRID recovery with MAKER legs is not instant-filled.
- MAKER recovery legs are frozen as `RECOVERY_MAKER_BACK_PACKAGE_SHADOW`.
- Pending maker recovery is checked through `recovery_shadow_fill`.
- Pending maker recovery is visible in the SL debug line as `recmk=... rf=...`.
- Maker recovery fill is only accepted if the fill probe signals `PARTIAL_SIGNAL` or `FULL_SIGNAL`.

Regression sample:

~~~text
rows: 918
frames: 608 -> 9868
negative_slc_rows: 49
blocked_rows: 21
pending_maker_recovery_rows: 13
maker_fill_signal_rows: 0
maker_fill_exec_rows: 0
maker_fill_unsafe_rows: 0
bad_exec: 0
final_slc_worst: +64.607217
final_slc_best: +76.307189
final_slc_ok: True
~~~

Expected interpretation:

- `pending_maker_recovery_rows > 0` means maker recovery packages are being frozen and tracked.
- `maker_fill_signal_rows = 0` means no simulated maker recovery fill occurred without a market fill signal.
- `bad_exec = 0` means no accepted recovery execution worsened SLC.
- `final_slc_ok = True` means the final combined SLC profile remains green.
