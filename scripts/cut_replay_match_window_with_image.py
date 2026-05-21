import json
from pathlib import Path


REPLAY_FILE = Path("replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

OUT_FILE = Path("replay/football-pro-sample_cut_4h_to_end_with_image")


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    match_start_pt = None
    match_end_pt = None

    # Останній img=true ДО початку вікна
    last_image_line = None
    last_image_pt = None

    # -------------------------------------------------
    # 1. Знаходимо старт і кінець матчу
    # -------------------------------------------------
    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                definition = mc.get("marketDefinition", {})
                if not isinstance(definition, dict):
                    continue

                if definition.get("inPlay") is True and match_start_pt is None:
                    match_start_pt = pt

                if definition.get("status") == "CLOSED":
                    match_end_pt = pt

    if match_start_pt is None:
        print("ERROR: match start not found")
        return

    if match_end_pt is None:
        print("ERROR: match end not found")
        return

    start_window_pt = match_start_pt - 4 * 60 * 60 * 1000
    end_window_pt = match_end_pt

    # -------------------------------------------------
    # 2. Знаходимо останній img=true ДО start_window
    # -------------------------------------------------
    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
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

            if pt >= start_window_pt:
                continue

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                if mc.get("img") is True:
                    last_image_line = raw
                    last_image_pt = pt

    if last_image_line is None:
        print("ERROR: no img=true found before start_window")
        return

    # -------------------------------------------------
    # 3. Записуємо новий файл:
    #    [last image] + [all rows in time window]
    # -------------------------------------------------
    total = 0
    kept = 0

    with REPLAY_FILE.open("r", encoding="utf-8") as src, OUT_FILE.open("w", encoding="utf-8") as dst:
        # Спочатку записуємо останній image
        dst.write(last_image_line + "\n")
        kept += 1

        # Потім усе всередині вікна
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
    print(f"image_pt       : {last_image_pt}")
    print()
    print(f"rows kept      : {kept}")
    print(f"rows total     : {total}")
    print(f"saved          : {OUT_FILE}")


if __name__ == "__main__":
    main()
