import json
from pathlib import Path


REPLAY_FILE = Path("replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

OUT_FILE = Path("replay/football-pro-sample_cut_4h_to_end")


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    # Час початку матчу:
    # беремо перший момент, коли inPlay = True
    match_start_pt = None

    # Час завершення:
    # беремо останній момент, де status = CLOSED
    match_end_pt = None

    # Спочатку проходимо весь файл і знаходимо межі інтервалу
    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")

            for mc in data.get("mc", []):
                market_id = str(mc.get("id"))

                if market_id != TARGET_MARKET_ID:
                    continue

                definition = mc.get("marketDefinition", {})
                if not isinstance(definition, dict):
                    continue

                # Перший перехід у inPlay = старт матчу
                if definition.get("inPlay") is True and match_start_pt is None:
                    match_start_pt = pt

                # Останній status = CLOSED = завершення матчу
                if definition.get("status") == "CLOSED":
                    match_end_pt = pt

    if match_start_pt is None:
        print("ERROR: match start not found (inPlay=True)")
        return

    if match_end_pt is None:
        print("ERROR: match end not found (status=CLOSED)")
        return

    # Початок вікна = 4 години до старту матчу
    start_window_pt = match_start_pt - 4 * 60 * 60 * 1000

    # Кінець вікна = завершення матчу
    end_window_pt = match_end_pt

    kept = 0
    total = 0

    # Другий прохід:
    # записуємо у новий файл тільки рядки, що попадають у потрібний інтервал
    with REPLAY_FILE.open("r", encoding="utf-8") as src, OUT_FILE.open("w", encoding="utf-8") as dst:
        for line in src:
            total += 1

            raw = line.strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")

            if pt is None:
                continue

            if start_window_pt <= pt <= end_window_pt:
                dst.write(raw + "\n")
                kept += 1

    print(f"match_start_pt : {match_start_pt}")
    print(f"match_end_pt   : {match_end_pt}")
    print(f"start_window   : {start_window_pt}")
    print(f"end_window     : {end_window_pt}")
    print()
    print(f"rows kept      : {kept}")
    print(f"rows total     : {total}")
    print(f"saved          : {OUT_FILE}")


if __name__ == "__main__":
    main()
