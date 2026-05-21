import json
import csv
from pathlib import Path


REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

OUT_CSV = Path("/home/nafanya/projects/betfair_bot/replay/validate_match_odds_book_issues.csv")


def valid_price(price):
    return isinstance(price, (int, float)) and 1.01 <= price <= 1000


def valid_size(size):
    return isinstance(size, (int, float)) and size > 0


def best_back_from_atb(levels):
    if not isinstance(levels, list):
        return None, None

    valid = [
        lvl for lvl in levels
        if isinstance(lvl, list)
        and len(lvl) >= 2
        and valid_price(lvl[0])
        and valid_size(lvl[1])
    ]
    if not valid:
        return None, None

    price, size = max(valid, key=lambda x: x[0])
    return price, size


def best_lay_from_atl(levels):
    if not isinstance(levels, list):
        return None, None

    valid = [
        lvl for lvl in levels
        if isinstance(lvl, list)
        and len(lvl) >= 2
        and valid_price(lvl[0])
        and valid_size(lvl[1])
    ]
    if not valid:
        return None, None

    price, size = min(valid, key=lambda x: x[0])
    return price, size


def new_runner_state():
    return {
        "name": None,
        "ltp": None,
        "tv": None,
        "back_price": None,
        "back_size": None,
        "lay_price": None,
        "lay_size": None,
    }


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    state = {}
    total_lines = 0
    total_market_ticks = 0

    # загальна статистика по runner-ах
    stats = {}

    # проблемні тики
    issues = []

    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            total_lines += 1

            raw = line.strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")
            market_seen = False

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                market_seen = True
                total_market_ticks += 1

                md = mc.get("marketDefinition")
                if isinstance(md, dict):
                    for runner in md.get("runners", []):
                        rid = runner.get("id")
                        name = runner.get("name")

                        if rid is None:
                            continue

                        if rid not in state:
                            state[rid] = new_runner_state()

                        state[rid]["name"] = str(name)

                for rc in mc.get("rc", []):
                    rid = rc.get("id")
                    if rid is None:
                        continue

                    if rid not in state:
                        state[rid] = new_runner_state()

                    s = state[rid]

                    if "ltp" in rc and isinstance(rc["ltp"], (int, float)):
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc and isinstance(rc["tv"], (int, float)):
                        s["tv"] = rc["tv"]

                    if "atb" in rc:
                        bp, bs = best_back_from_atb(rc["atb"])
                        if bp is not None:
                            s["back_price"] = bp
                            s["back_size"] = bs

                    if "atl" in rc:
                        lp, ls = best_lay_from_atl(rc["atl"])
                        if lp is not None:
                            s["lay_price"] = lp
                            s["lay_size"] = ls

            if not market_seen:
                continue

            # валідація поточного tick по всіх відомих runner-ах
            for rid, s in state.items():
                name = s["name"] if s["name"] is not None else str(rid)

                if rid not in stats:
                    stats[rid] = {
                        "name": name,
                        "ticks_seen": 0,
                        "missing_back": 0,
                        "missing_lay": 0,
                        "lay_lt_back": 0,
                        "equal_book": 0,
                        "valid_spread": 0,
                        "min_spread": None,
                        "max_spread": None,
                    }

                st = stats[rid]
                st["ticks_seen"] += 1

                back = s["back_price"]
                lay = s["lay_price"]

                issue_type = None
                spread = None

                if back is None:
                    st["missing_back"] += 1
                    issue_type = "missing_back"

                if lay is None:
                    st["missing_lay"] += 1
                    if issue_type is None:
                        issue_type = "missing_lay"
                    else:
                        issue_type += "+missing_lay"

                if back is not None and lay is not None:
                    spread = lay - back

                    if lay < back:
                        st["lay_lt_back"] += 1
                        issue_type = "lay_lt_back"

                    elif lay == back:
                        st["equal_book"] += 1
                        issue_type = "equal_book"

                    else:
                        st["valid_spread"] += 1

                        if st["min_spread"] is None or spread < st["min_spread"]:
                            st["min_spread"] = spread

                        if st["max_spread"] is None or spread > st["max_spread"]:
                            st["max_spread"] = spread

                if issue_type is not None:
                    issues.append({
                        "line_number": line_number,
                        "pt": pt,
                        "runner_id": rid,
                        "runner_name": name,
                        "back_price": back,
                        "back_size": s["back_size"],
                        "lay_price": lay,
                        "lay_size": s["lay_size"],
                        "ltp": s["ltp"],
                        "tv": s["tv"],
                        "spread": spread,
                        "issue_type": issue_type,
                    })

    # збереження проблемних тиков у csv
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "line_number",
                "pt",
                "runner_id",
                "runner_name",
                "back_price",
                "back_size",
                "lay_price",
                "lay_size",
                "ltp",
                "tv",
                "spread",
                "issue_type",
            ],
        )
        writer.writeheader()
        writer.writerows(issues)

    print("VALIDATION RESULT")
    print("=" * 80)
    print(f"file               : {REPLAY_FILE}")
    print(f"market_id          : {TARGET_MARKET_ID}")
    print(f"total_lines        : {total_lines}")
    print(f"market_ticks       : {total_market_ticks}")
    print(f"issues_csv         : {OUT_CSV}")
    print()

    for rid in sorted(stats.keys(), key=lambda x: stats[x]["name"]):
        st = stats[rid]

        print(f"RUNNER: {st['name']} ({rid})")
        print(f"  ticks_seen       : {st['ticks_seen']}")
        print(f"  missing_back     : {st['missing_back']}")
        print(f"  missing_lay      : {st['missing_lay']}")
        print(f"  lay_lt_back      : {st['lay_lt_back']}")
        print(f"  equal_book       : {st['equal_book']}")
        print(f"  valid_spread     : {st['valid_spread']}")
        print(f"  min_spread       : {st['min_spread']}")
        print(f"  max_spread       : {st['max_spread']}")
        print()

    print(f"total_issues       : {len(issues)}")


if __name__ == "__main__":
    main()
