import shutil
from pathlib import Path


# Файл зі списком потрібних ринків.
# Формат рядка:
# market_id | market_type | market_name | event_name
TARGET_MARKETS_FILE = Path("replay/target_markets.txt")

# Папка, де зараз лежать отримані файли.
SOURCE_DIR = Path("replay")

# Базова папка, куди треба розкладати файли.
MARKETS_BASE_DIR = Path("replay/markets")


# Визначає підпапку за розширенням файла.
def get_subdir_by_suffix(file_path):
    suffix = file_path.suffix.lower()

    # CSV-файли кладемо в csv/
    if suffix == ".csv":
        return "csv"

    # Графіки та зображення кладемо в plots/
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return "plots"

    # Все інше кладемо в raw/
    return "raw"


# Читає replay/target_markets.txt
# і будує словник:
#   market_id -> market_type
def load_market_map():
    market_map = {}

    if not TARGET_MARKETS_FILE.exists():
        print(f"File not found: {TARGET_MARKETS_FILE}")
        return market_map

    with TARGET_MARKETS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            # Очікуваний формат:
            # 1.131162806 | MATCH_ODDS | Match Odds | Middlesbrough v Man City
            parts = [p.strip() for p in line.split("|")]

            if len(parts) < 2:
                continue

            market_id = parts[0]
            market_type = parts[1]

            if market_id and market_type:
                market_map[market_id] = market_type

    return market_map


def main():
    market_map = load_market_map()

    if not market_map:
        print("market map is empty")
        return

    if not SOURCE_DIR.exists():
        print(f"Source dir not found: {SOURCE_DIR}")
        return

    moved_count = 0
    skipped_count = 0

    # Переглядаємо всі об'єкти в replay
    for file_path in SOURCE_DIR.iterdir():
        # Якщо це не файл, а папка — пропускаємо
        if not file_path.is_file():
            print(f"skip dir : {file_path.name}")
            skipped_count += 1
            continue

        # Службові файли не чіпаємо
        if file_path.name in {
            "football-pro-sample",
            "target_markets.txt",
            "markets.txt",
            "events.txt",
            "selected_markets.txt",
            "replay_schema.csv",
        }:
            print(f"skip system file : {file_path.name}")
            skipped_count += 1
            continue

        matched_market_id = None
        matched_market_type = None

        # Шукаємо market_id в назві файла
        for market_id, market_type in market_map.items():
            # У target_markets.txt market_id має формат:
            # 1.131162806
            #
            # А в назві файла часто використовується тільки:
            # 131162806
            short_market_id = market_id.split(".")[-1]

            if market_id in file_path.name or short_market_id in file_path.name:
                matched_market_id = market_id
                matched_market_type = market_type
                break

        # Якщо ринок по назві файла не визначений — пропускаємо
        if matched_market_id is None:
            print(f"skip no market_id in filename : {file_path.name}")
            skipped_count += 1
            continue

        # Визначаємо підпапку за типом файла
        subdir = get_subdir_by_suffix(file_path)

        # Формуємо директорію призначення
        target_dir = MARKETS_BASE_DIR / matched_market_type / subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        # Шлях до нового файла
        target_file = target_dir / file_path.name

        # Копіюємо файл
        shutil.copy2(file_path, target_file)

        print(f"copy : {file_path.name} -> {target_file}")
        moved_count += 1

    print()
    print(f"copied:  {moved_count}")
    print(f"skipped: {skipped_count}")


if __name__ == "__main__":
    main()
