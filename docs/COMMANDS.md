# Команди / scripts

## Еталонний dev‑UI з бекапу — compact engine_v2 stream

```bash
python scripts/replay_stream_selected_markets_dashboard_engine_v2.py \
  --discover-targets \
  --start-minutes-before 10 \
  --ladder \
  --self-check \
  --engine-v2-overlay \
  --show-queue \
  --smooth-ui \
  --interactive \
  --delay 0.0025 \
  --col-width 24 \
  --cs-cols 1 \
  --ladder-max-rows 4
```

Клавіші (`--interactive`):

- `space` — pause/resume
- `n` (або `т`) — next frame (+250ms)
- `b` (або `и`) — back frame (-250ms)
- `q` (або `й`) — quit

## Stationary totals wrapper

```bash
python scripts/replay_stream_selected_markets_dashboard_stationary_totals.py --help
```

## Smooth UI wrapper (legacy)

```bash
python scripts/replay_stream_selected_markets_dashboard_smooth.py --help
```
