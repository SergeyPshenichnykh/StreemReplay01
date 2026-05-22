#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_REQUIRED = [
    "BEST_PRICE_ONLY",
    "VOLUME",
    "INPLAY_INFO",
    "SELECTION_INFO",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post_json(base_url: str, path: str, body: dict[str, Any], timeout: float, host_header: str | None = None) -> tuple[dict[str, Any], float]:
    url = base_url.rstrip("/") + path
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json", **({"Host": host_header} if host_header else {})},
    )

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"BetAngel HTTP {e.code} path={path} body={detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"BetAngel connection failed path={path}: {e}") from e

    dt_ms = (time.perf_counter() - t0) * 1000.0

    if status // 100 != 2:
        raise RuntimeError(f"BetAngel HTTP {status} path={path} body={data[:500]!r}")

    try:
        js = json.loads(data.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"BetAngel non-JSON response path={path} body={data[:500]!r}") from e

    if str(js.get("status", "OK")).upper() == "FAILED":
        raise RuntimeError(f"BetAngel FAILED path={path} body={json.dumps(js)[:1000]}")

    return js, dt_ms


def text(node: dict[str, Any], *names: str) -> str | None:
    for name in names:
        v = node.get(name)
        if v is not None and str(v).strip():
            return str(v)
    return None


def as_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def as_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(float(v))
    except Exception:
        return default


def price_size_map(node: Any) -> dict[str, float]:
    out: dict[str, float] = {}

    if node is None:
        return out

    if isinstance(node, list):
        for x in node:
            price = 0.0
            size = 0.0

            if isinstance(x, list) and len(x) >= 2:
                price = as_float(x[0])
                size = as_float(x[1])
            elif isinstance(x, dict):
                price = as_float(x.get("price", x.get("prc", x.get("odds"))))
                size = as_float(x.get("size", x.get("sz", x.get("amount"))))

            if price > 0 and size > 0:
                out[f"{price:.2f}"] = size

        return out

    if isinstance(node, dict):
        if any(k in node for k in ("price", "prc", "odds")):
            price = as_float(node.get("price", node.get("prc", node.get("odds"))))
            size = as_float(node.get("size", node.get("sz", node.get("amount"))))
            if price > 0 and size > 0:
                out[f"{price:.2f}"] = size
            return out

        for k, v in node.items():
            try:
                price = float(k)
            except Exception:
                continue

            if isinstance(v, dict):
                size = as_float(v.get("size", v.get("sz", v.get("amount"))))
            else:
                size = as_float(v)

            if price > 0 and size > 0:
                out[f"{price:.2f}"] = size

    return out


def add_single_level(out: dict[str, float], node: Any) -> None:
    if node is None:
        return

    price = 0.0
    size = 0.0

    if isinstance(node, list) and len(node) >= 2:
        price = as_float(node[0])
        size = as_float(node[1])
    elif isinstance(node, dict):
        price = as_float(node.get("price", node.get("prc", node.get("odds"))))
        size = as_float(node.get("size", node.get("sz", node.get("amount"))))

    if price > 0 and size > 0:
        out[f"{price:.2f}"] = size


def normalize_market_prices(raw: dict[str, Any]) -> dict[str, Any]:
    markets_raw = (((raw.get("result") or {}).get("markets")) or [])
    markets: list[dict[str, Any]] = []

    if not isinstance(markets_raw, list):
        markets_raw = []

    for jm in markets_raw:
        if not isinstance(jm, dict):
            continue

        market_id = text(jm, "id", "marketId")
        if not market_id:
            continue

        market = {
            "market_id": market_id,
            "name": text(jm, "name", "marketName"),
            "market_type": text(jm, "marketType"),
            "status": text(jm, "status", "marketStatus") or "OPEN",
            "market_time": text(jm, "marketTime", "startTime"),
            "in_play": bool(jm.get("inPlay", False)),
            "total_matched": as_float(jm.get("totalMatched"), 0.0),
            "runners": [],
        }

        selections = jm.get("selections") or []
        if isinstance(selections, list):
            for js in selections:
                if not isinstance(js, dict):
                    continue

                selection_id = as_int(js.get("id", js.get("selectionId")), 0)
                if selection_id <= 0:
                    continue

                atb: dict[str, float] = {}
                atl: dict[str, float] = {}

                for key in ("availableToBack", "atb", "back"):
                    atb.update(price_size_map(js.get(key)))
                add_single_level(atb, js.get("back1"))

                for key in ("availableToLay", "atl", "lay"):
                    atl.update(price_size_map(js.get(key)))
                add_single_level(atl, js.get("lay1"))

                runner = {
                    "selection_id": selection_id,
                    "name": text(js, "name", "runnerName", "selectionName"),
                    "status": text(js, "status", "runnerStatus") or "ACTIVE",
                    "ltp": as_float(js.get("ltp"), 0.0) if "ltp" in js else None,
                    "traded_volume": as_float(js.get("tradedVolume"), 0.0) if "tradedVolume" in js else None,
                    "available_to_back": atb,
                    "available_to_lay": atl,
                    "best_back": max((float(k) for k in atb.keys()), default=None),
                    "best_lay": min((float(k) for k in atl.keys()), default=None),
                }
                market["runners"].append(runner)

        markets.append(market)

    return {
        "market_count": len(markets),
        "runner_count": sum(len(m["runners"]) for m in markets),
        "markets": markets,
    }


def get_loaded_market_ids(base_url: str, timeout: float, host_header: str | None = None) -> tuple[list[str], dict[str, Any]]:
    body = {
        "marketsFilter": {"filter": "ALL"},
        "dataRequired": DATA_REQUIRED,
    }

    raw, dt_ms = post_json(base_url, "/markets/v1.0/getMarketPrices", body, timeout, host_header)
    ids: list[str] = []

    markets = (((raw.get("result") or {}).get("markets")) or [])
    if isinstance(markets, list):
        for m in markets:
            if isinstance(m, dict):
                mid = text(m, "id", "marketId")
                if mid and mid not in ids:
                    ids.append(mid)

    if ids:
        return ids, {"source": "getMarketPrices", "latency_ms": round(dt_ms, 3), "raw_status": raw.get("status")}

    raw2, dt2_ms = post_json(base_url, "/markets/v1.0/getMarkets", {}, timeout, host_header)
    markets2 = (((raw2.get("result") or {}).get("markets")) or [])
    if isinstance(markets2, list):
        for m in markets2:
            if isinstance(m, dict):
                mid = text(m, "id", "marketId")
                if mid and mid not in ids:
                    ids.append(mid)

    return ids, {"source": "getMarkets", "latency_ms": round(dt2_ms, 3), "raw_status": raw2.get("status")}


def get_market_prices(base_url: str, market_ids: list[str], timeout: float, host_header: str | None = None) -> tuple[dict[str, Any], float]:
    body = {
        "marketsFilter": {"filter": "SPECIFIED", "ids": market_ids},
        "dataRequired": DATA_REQUIRED,
    }
    return post_json(base_url, "/markets/v1.0/getMarketPrices", body, timeout, host_header)


def parse_market_ids(args: argparse.Namespace) -> list[str]:
    ids: list[str] = []

    for item in args.market_id or []:
        for x in str(item).replace(",", " ").split():
            if x.strip() and x.strip() not in ids:
                ids.append(x.strip())

    if args.markets:
        for x in Path(args.markets).read_text().splitlines():
            x = x.strip()
            if x and not x.startswith("#") and x not in ids:
                ids.append(x)

    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("BETANGEL_BASE_URL", "http://127.0.0.1:9000/api"))
    ap.add_argument("--market-id", action="append", help="Market id. Can be repeated or comma-separated.")
    ap.add_argument("--markets", help="Text file with one market id per line.")
    ap.add_argument("--use-loaded", action="store_true", help="Use currently loaded Guardian/Bet Angel markets.")
    ap.add_argument("--out", default="replay/live_betangel_snapshots.jsonl")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--interval-ms", type=int, default=1000)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--host-header", help="Override HTTP Host header, e.g. localhost for Bet Angel localhost binding.")
    ap.add_argument("--include-raw", action="store_true")
    args = ap.parse_args()

    market_ids = parse_market_ids(args)

    loaded_meta: dict[str, Any] | None = None
    if args.use_loaded or not market_ids:
        market_ids, loaded_meta = get_loaded_market_ids(args.base_url, args.timeout, args.host_header)

    if not market_ids:
        print("ERROR: no market ids. Load markets in Guardian or pass --market-id.", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"BETANGEL_PROBE base_url={args.base_url} markets={len(market_ids)} out={out_path}")

    with out_path.open("a", encoding="utf-8") as f:
        for sample_i in range(max(1, args.samples)):
            raw, dt_ms = get_market_prices(args.base_url, market_ids, args.timeout, args.host_header)
            normalized = normalize_market_prices(raw)

            row = {
                "type": "betangel_live_snapshot",
                "sample": sample_i + 1,
                "ts": time.time(),
                "utc": utc_now(),
                "base_url": args.base_url,
                "host_header": args.host_header,
                "market_ids": market_ids,
                "loaded_meta": loaded_meta,
                "latency_ms": round(dt_ms, 3),
                "raw_status": raw.get("status"),
                "normalized": normalized,
            }
            if args.include_raw:
                row["raw"] = raw

            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()

            print(
                "SNAPSHOT",
                f"sample={sample_i + 1}",
                f"latency_ms={dt_ms:.1f}",
                f"markets={normalized['market_count']}",
                f"runners={normalized['runner_count']}",
            )

            if sample_i + 1 < max(1, args.samples):
                time.sleep(max(0, args.interval_ms) / 1000.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
