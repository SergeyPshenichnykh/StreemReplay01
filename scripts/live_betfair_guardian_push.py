#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bf_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    if not path.exists():
        raise FileNotFoundError(path)

    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        props[k.strip()] = v.strip()

    return props


def prop(props: dict[str, str], key: str, default: str = "") -> str:
    v = props.get(key)
    return default if v is None or v == "" else v


def prop_int(props: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(prop(props, key, str(default))))
    except Exception:
        return default


def prop_float(props: dict[str, str], key: str, default: float) -> float:
    try:
        return float(prop(props, key, str(default)))
    except Exception:
        return default


def prop_bool(props: dict[str, str], key: str, default: bool) -> bool:
    v = prop(props, key, str(default)).strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default


def http_post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[dict[str, Any], float]:
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers=req_headers,
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} url={url} body={detail[:1000]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"connection failed url={url}: {e}") from e

    dt_ms = (time.perf_counter() - t0) * 1000.0

    if status // 100 != 2:
        raise RuntimeError(f"HTTP {status} url={url} body={data[:1000]!r}")

    try:
        js = json.loads(data.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"non-JSON response url={url} body={data[:1000]!r}") from e

    return js, dt_ms


class BetfairDelayed:
    def __init__(self, endpoint: str, app_key: str, session_token: str, timeout: float) -> None:
        if not endpoint:
            raise ValueError("betfair.endpoint is required")
        if not app_key:
            raise ValueError("betfair.appKey is required")
        if not session_token:
            raise ValueError("betfair.sessionToken is required")

        self.endpoint = endpoint
        self.app_key = app_key
        self.session_token = session_token
        self.timeout = timeout
        self.rpc_id = 1

    def call(self, method: str, params: dict[str, Any]) -> Any:
        body = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self.rpc_id,
        }
        self.rpc_id += 1

        headers = {
            "X-Application": self.app_key,
            "X-Authentication": self.session_token,
        }

        js, _dt = http_post_json(self.endpoint, body, headers=headers, timeout=self.timeout)

        if "error" in js:
            raise RuntimeError(f"Betfair error method={method}: {js['error']}")

        return js.get("result")

    def list_events_window(
        self,
        min_minutes: int,
        max_minutes: int,
        event_type_id: str,
        in_play_only: bool,
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        start_from = now + timedelta(minutes=min_minutes)
        start_to = now + timedelta(minutes=max_minutes)

        flt: dict[str, Any] = {
            "eventTypeIds": [event_type_id],
            "marketStartTime": {"from": bf_time(start_from), "to": bf_time(start_to)},
        }
        if in_play_only:
            flt["inPlayOnly"] = True

        result = self.call("SportsAPING/v1.0/listEvents", {"filter": flt}) or []

        out: list[dict[str, Any]] = []
        for row in result:
            event = (row or {}).get("event") or {}
            event_id = str(event.get("id") or "")
            name = str(event.get("name") or "")
            open_date = str(event.get("openDate") or "")
            if not event_id or not name or not open_date:
                continue

            try:
                dt = datetime.fromisoformat(open_date.replace("Z", "+00:00"))
            except Exception:
                continue

            minutes_to_start = int((dt - now).total_seconds() // 60)
            if min_minutes <= minutes_to_start <= max_minutes:
                out.append({
                    "event_id": event_id,
                    "event_name": name,
                    "start_time": open_date,
                    "minutes_to_start": minutes_to_start,
                })

        return out

    def list_all_markets_for_event(self, event_id: str, max_results: int) -> list[dict[str, Any]]:
        params = {
            "filter": {"eventIds": [event_id]},
            "marketProjection": ["EVENT", "MARKET_START_TIME", "RUNNER_DESCRIPTION"],
            "sort": "FIRST_TO_START",
            "maxResults": str(max_results),
        }

        result = self.call("SportsAPING/v1.0/listMarketCatalogue", params) or []
        return [parse_catalogue_market(m) for m in result]

    def load_total_matched_for_markets(self, market_ids: list[str]) -> float:
        total = 0.0
        batch_size = 10

        for start in range(0, len(market_ids), batch_size):
            batch = market_ids[start:start + batch_size]
            result = self.call("SportsAPING/v1.0/listMarketBook", {"marketIds": batch}) or []
            for book in result:
                try:
                    total += float((book or {}).get("totalMatched") or 0.0)
                except Exception:
                    pass
            time.sleep(0.15)

        return total


def infer_market_type(market_name: str | None) -> str:
    name = (market_name or "").strip()

    if name.lower() == "match odds":
        return "MATCH_ODDS"
    if name.lower() == "correct score":
        return "CORRECT_SCORE"

    # Over/Under 2.5 Goals -> OVER_UNDER_25
    lower = name.lower()
    if lower.startswith("over/under ") and lower.endswith(" goals"):
        mid = name[len("Over/Under "): -len(" Goals")].strip()
        if mid.endswith(".5"):
            digits = mid.replace(".", "")
            if digits.isdigit():
                return f"OVER_UNDER_{digits}"

    return "OTHER"


def parse_catalogue_market(m: dict[str, Any]) -> dict[str, Any]:
    event = (m or {}).get("event") or {}
    market_id = str((m or {}).get("marketId") or "")
    market_name = str((m or {}).get("marketName") or "")
    market_type = infer_market_type(market_name)

    under_selection_id = 0
    under_runner_name = ""

    runners = (m or {}).get("runners") or []
    if isinstance(runners, list):
        for r in runners:
            runner_name = str((r or {}).get("runnerName") or "")
            selection_id_raw = (r or {}).get("selectionId")
            try:
                selection_id = int(selection_id_raw)
            except Exception:
                selection_id = 0

            if runner_name.lower().startswith("under ") and selection_id > 0:
                under_selection_id = selection_id
                under_runner_name = runner_name
                break

    return {
        "market_id": market_id,
        "event_id": str(event.get("id") or ""),
        "event_name": str(event.get("name") or ""),
        "market_name": market_name,
        "market_type": market_type,
        "start_time": str((m or {}).get("marketStartTime") or ""),
        "under_selection_id": under_selection_id,
        "under_runner_name": under_runner_name,
    }


def dedup_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for m in markets:
        mid = str(m.get("market_id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(m)

    return out


class BetAngelGuardian:
    def __init__(self, base_url: str, host_header: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.host_header = host_header
        self.timeout = timeout

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Host": self.host_header} if self.host_header else None
        js, _dt = http_post_json(self.base_url + path, body, headers=headers, timeout=self.timeout)

        if str(js.get("status", "OK")).upper() == "FAILED":
            raise RuntimeError(f"Bet Angel FAILED path={path}: {json.dumps(js)[:1000]}")

        return js

    def get_loaded_market_ids(self) -> list[str]:
        body = {
            "marketsFilter": {"filter": "ALL"},
            "dataRequired": ["BEST_PRICE_ONLY", "VOLUME", "INPLAY_INFO", "SELECTION_INFO"],
        }

        ids: list[str] = []

        try:
            prices = self.post("/markets/v1.0/getMarketPrices", body)
            markets = (((prices.get("result") or {}).get("markets")) or [])
            if isinstance(markets, list):
                for m in markets:
                    if isinstance(m, dict):
                        mid = str(m.get("id") or m.get("marketId") or "")
                        if mid and mid not in ids:
                            ids.append(mid)
        except Exception:
            pass

        if ids:
            return ids

        gm = self.post("/markets/v1.0/getMarkets", {})
        markets = (((gm.get("result") or {}).get("markets")) or [])
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict):
                    mid = str(m.get("id") or m.get("marketId") or "")
                    if mid and mid not in ids:
                        ids.append(mid)

        return ids

    def add_markets(self, market_ids: list[str]) -> dict[str, Any]:
        return self.post("/guardian/v1.0/addMarkets", {"marketIds": market_ids})

    def set_under_nominations(self, markets: list[dict[str, Any]], column: int) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for m in markets:
            under_id = int(m.get("under_selection_id") or 0)
            if under_id <= 0:
                continue
            rows.append({
                "marketId": m["market_id"],
                "nominatedSelectionColumn": column,
                "selectionId": str(under_id),
            })
        return self.post("/guardian/v1.0/setNominatedSelections", {"nominatedSelectionInfo": rows})

    def apply_rules(self, rules_file_name: str, market_ids: list[str], column: int) -> dict[str, Any]:
        return self.post("/guardian/v1.0/applyRules", {
            "rulesFileName": rules_file_name,
            "marketsFilter": {"filter": "SPECIFIED", "ids": market_ids},
            "guardianRulesColumn": column,
        })


def select_events_and_markets(props: dict[str, str], bf: BetfairDelayed) -> list[dict[str, Any]]:
    min_minutes = prop_int(props, "strategy.minMinutesBeforeStart", 0)
    max_minutes = prop_int(props, "strategy.maxMinutesBeforeStart", 720)
    max_event_markets = prop_int(props, "betfair.maxEventMarkets", 200)
    max_events = prop_int(props, "strategy.maxSelectedEvents", 2)
    min_event_volume = prop_float(props, "strategy.minEventVolume", 5000.0)
    event_type_id = prop(props, "betfair.eventTypeId", "1")
    in_play_only = prop_bool(props, "strategy.inPlayOnly", True)

    print(f"SCAN_EVENTS_WINDOW minutes={min_minutes}-{max_minutes} inPlayOnly={in_play_only}")
    print(f"MAX_SELECTED_EVENTS={max_events} MIN_EVENT_VOLUME={min_event_volume:.2f}")

    events = bf.list_events_window(min_minutes, max_minutes, event_type_id, in_play_only)

    selected: list[dict[str, Any]] = []

    for ev in events:
        markets = bf.list_all_markets_for_event(ev["event_id"], max_event_markets)
        if not markets:
            continue

        market_ids = [m["market_id"] for m in markets if m.get("market_id")]
        volume = bf.load_total_matched_for_markets(list(dict.fromkeys(market_ids)))

        if volume < min_event_volume:
            print(
                "EVENT_REJECT_LOW_VOLUME",
                ev["event_name"],
                f"eventId={ev['event_id']}",
                f"minutesToStart={ev['minutes_to_start']}",
                f"markets={len(markets)}",
                f"volume={volume:.2f}",
                f"minVolume={min_event_volume:.2f}",
            )
            continue

        print(
            "EVENT_CANDIDATE",
            ev["event_name"],
            f"eventId={ev['event_id']}",
            f"minutesToStart={ev['minutes_to_start']}",
            f"markets={len(markets)}",
            f"volume={volume:.2f}",
        )

        selected.append({
            **ev,
            "volume": volume,
            "markets": markets,
        })

        selected.sort(key=lambda x: float(x["volume"]), reverse=True)
        selected = selected[:max_events]

        time.sleep(0.15)

    return selected


def print_selected_event(ev: dict[str, Any], markets: list[dict[str, Any]]) -> None:
    print(
        f"SELECTED_EVENT={ev['event_name']} eventId={ev['event_id']} "
        f"minutesToStart={ev['minutes_to_start']} volume={float(ev['volume']):.2f} allMarkets={len(ev['markets'])}"
    )
    for m in markets:
        print(
            "  MARKET",
            m["market_id"],
            m["market_type"],
            m["market_name"],
            f"under={m.get('under_selection_id')}",
            m.get("under_runner_name"),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/mnt/c/BeeBot/config/application.properties")
    ap.add_argument("--betangel-base-url", default=None)
    ap.add_argument("--host-header", default="localhost")
    ap.add_argument("--out", default="replay/live_guardian_push_summary.json")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--allow-in-play-selector", action="store_true")
    ap.add_argument("--allow-negative-start-window", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    props = read_properties(Path(args.config))

    cfg_in_play_only = prop_bool(props, "strategy.inPlayOnly", False)
    cfg_min_minutes = prop_int(props, "strategy.minMinutesBeforeStart", 0)
    cfg_max_minutes = prop_int(props, "strategy.maxMinutesBeforeStart", 720)

    if cfg_in_play_only and not args.allow_in_play_selector:
        raise SystemExit(
            "ERROR: strategy.inPlayOnly=true in config. "
            "Refusing to select live in-play events unless --allow-in-play-selector is passed."
        )

    if cfg_min_minutes < 0 and not args.allow_negative_start_window:
        raise SystemExit(
            f"ERROR: strategy.minMinutesBeforeStart={cfg_min_minutes} in config. "
            "Refusing negative start window unless --allow-negative-start-window is passed."
        )

    print(
        f"SELECTOR_CONFIG_GUARD inPlayOnly={cfg_in_play_only} "
        f"minutes={cfg_min_minutes}-{cfg_max_minutes}"
    )

    betangel_base = args.betangel_base_url or prop(props, "betangel.baseUrl", "http://127.0.0.1:9000/api")

    bf = BetfairDelayed(
        endpoint=prop(props, "betfair.endpoint"),
        app_key=prop(props, "betfair.appKey"),
        session_token=prop(props, "betfair.sessionToken"),
        timeout=args.timeout,
    )
    ba = BetAngelGuardian(
        base_url=betangel_base,
        host_header=args.host_header,
        timeout=args.timeout,
    )

    selected_events = select_events_and_markets(props, bf)
    if not selected_events:
        print("NO_SELECTED_EVENTS")
        return 1

    loaded = set(ba.get_loaded_market_ids())
    print(f"LOADED_GUARDIAN_MARKETS={len(loaded)}")

    all_selected_markets: list[dict[str, Any]] = []
    nomination_markets: list[dict[str, Any]] = []
    ids_to_push: list[str] = []

    for ev in selected_events:
        raw = ev["markets"]
        selected_markets = [
            m for m in dedup_markets(raw)
            if str(m.get("market_type") or "").startswith("OVER_UNDER_")
            and int(m.get("under_selection_id") or 0) > 0
        ]

        print(
            f"DEDUP_SELECTED_MARKETS eventId={ev['event_id']} "
            f"raw={len(raw)} dedup={len(selected_markets)} removed={len(raw) - len(selected_markets)}"
        )

        if not selected_markets:
            print(f"NO_MARKETS_FOR_SELECTED_EVENT={ev['event_name']}")
            continue

        print_selected_event(ev, selected_markets)

        ids = list(dict.fromkeys(m["market_id"] for m in selected_markets))
        loaded_selected = sum(1 for mid in ids if mid in loaded)

        print(
            f"GUARDIAN_CHECK eventId={ev['event_id']} selectedMarkets={len(ids)} "
            f"loadedSelectedMarkets={loaded_selected} loadedGuardianMarkets={len(loaded)}"
        )

        all_selected_markets.extend(selected_markets)
        nomination_markets.extend(selected_markets)

        missing_ids = [mid for mid in ids if mid not in loaded]

        if missing_ids:
            print(
                f"RUN_SELECTED_EVENT_MISSING_MARKETS={ev['event_name']} "
                f"eventId={ev['event_id']} missing={len(missing_ids)} loaded={loaded_selected}/{len(ids)}"
            )
            ids_to_push.extend(missing_ids)
        else:
            print(
                f"RUN_SELECTED_EVENT_MARKETS_ALREADY_IN_GUARDIAN={ev['event_name']} "
                f"eventId={ev['event_id']} minutesToStart={ev['minutes_to_start']} volume={float(ev['volume']):.2f}"
            )

    all_selected_markets = dedup_markets(all_selected_markets)
    nomination_markets = dedup_markets(nomination_markets)
    all_market_ids = list(dict.fromkeys(m["market_id"] for m in all_selected_markets))
    distinct_ids_to_push = list(dict.fromkeys(ids_to_push))

    if not all_market_ids:
        print("NO_SELECTED_MARKETS_AFTER_TOP_EVENTS")
        return 1

    responses: dict[str, Any] = {}

    if distinct_ids_to_push:
        print(f"PUSH TO GUARDIAN selectedEvents={len(selected_events)} marketIds={len(distinct_ids_to_push)}")
        if args.dry_run:
            responses["addMarkets"] = {"dry_run": True, "marketIds": distinct_ids_to_push}
        else:
            responses["addMarkets"] = ba.add_markets(distinct_ids_to_push)

        wait_sec = prop_int(props, "strategy.waitAfterAddMarketsSec", prop_int(props, "betangel.afterAddWaitSeconds", 15))
        if wait_sec > 0 and not args.dry_run:
            print(f"WAIT_AFTER_ADD_MARKETS seconds={wait_sec}")
            time.sleep(wait_sec)
    else:
        print(f"NO_NEW_MARKETS_TO_PUSH selectedEvents={len(selected_events)} allMarketsAlreadyInGuardian=true")

    if prop_bool(props, "betangel.nominateUnderSelection", True) and nomination_markets:
        col = prop_int(props, "betangel.nominatedSelectionColumn", 1)
        print(f"NOMINATE UNDER selections column={col} markets={len(nomination_markets)}")
        if args.dry_run:
            responses["setUnderNominations"] = {"dry_run": True, "markets": len(nomination_markets), "column": col}
        else:
            responses["setUnderNominations"] = ba.set_under_nominations(nomination_markets, col)

    rules = prop(props, "betangel.rulesFileName", "")
    if rules:
        col = prop_int(props, "betangel.guardianRulesColumn", 1)
        print(f"APPLY RULES {rules} markets={len(all_market_ids)} column={col}")
        if args.dry_run:
            responses["applyRules"] = {"dry_run": True, "rulesFileName": rules, "marketIds": all_market_ids, "column": col}
        else:
            responses["applyRules"] = ba.apply_rules(rules, all_market_ids, col)

    summary = {
        "utc": utc_now(),
        "dry_run": args.dry_run,
        "betangel_base_url": betangel_base,
        "host_header": args.host_header,
        "selected_events": [
            {
                "event_id": ev["event_id"],
                "event_name": ev["event_name"],
                "minutes_to_start": ev["minutes_to_start"],
                "volume": ev["volume"],
            }
            for ev in selected_events
        ],
        "selected_markets": all_selected_markets,
        "all_market_ids": all_market_ids,
        "ids_to_push": distinct_ids_to_push,
        "loaded_before_count": len(loaded),
        "responses": responses,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"SUMMARY_WRITTEN {out}")
    print(f"SELECTED_EVENTS={len(selected_events)} SELECTED_MARKETS={len(all_market_ids)} PUSHED={len(distinct_ids_to_push)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
