# Portability / перенос на інший ПК

Цей репозиторій — експериментальний dev‑UI для розробки стратегії під Betfair (terminal ladder + Correct Score table + replay stream).

## Мінімальні вимоги

- Python 3.11+ (рекомендовано 3.12)
- Linux/WSL (перевірено на Ubuntu під WSL)
- Термінал з підтримкою ANSI escape codes

## Швидкий старт (source‑only)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
```

Основні скрипти dashboard/stream **не потребують сторонніх бібліотек** (stdlib only).

## Додаткові залежності (research/plots)

Деякі `market_research_*` та `scripts/plot_*.py` потребують:

```bash
pip install -r requirements-research.txt
```

## Дані replay (ВАЖЛИВО)

Папка `replay/` у поточному проєкті може бути дуже великою (гігабайти), тому **не комітиться в Git**.

Варіанти:

- Переносити `replay/` окремо (USB/архів/rsync)
- Або створювати архів із `--with-replay` (див. нижче)

## Архів проєкту

Є утиліта для створення архіву:

```bash
python scripts/make_project_archive.py --out dist/betfair_bot-src.tar.gz
python scripts/make_project_archive.py --with-replay --out dist/betfair_bot-full.tar.gz
```

За замовчуванням архів — “source‑only”: без `.venv/`, без `replay/`, без `TEST*.bmp/png`.

