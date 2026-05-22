#!/usr/bin/env python3
import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="?", default="replay/second_leg_debug.jsonl")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    p = Path(args.jsonl)
    if not p.exists():
        print(f"ERROR: file not found: {p}", file=sys.stderr)
        return 2

    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    if not rows:
        print(f"ERROR: empty jsonl: {p}", file=sys.stderr)
        return 2

    state_c = Counter(str(r.get("state")) for r in rows)
    pkg_mode_c = Counter(str((r.get("package_compact") or {}).get("mode")) for r in rows)
    pkg_reason_c = Counter(str((r.get("package_compact") or {}).get("reason")) for r in rows)
    rex_c = Counter(str((r.get("second_leg_recovery_exec") or {}).get("status")) for r in rows)
    rf_c = Counter(str((r.get("recovery_shadow_fill") or {}).get("status")) for r in rows)
    rmfx_c = Counter(str((r.get("recovery_maker_fill_exec") or {}).get("status")) for r in rows)

    neg = []
    blocked = []
    bad_exec = []
    pending = []
    maker_fill_signal = []
    maker_fill_exec = []
    maker_fill_unsafe = []

    for r in rows:
        comb = r.get("second_leg_combined_profile", {}) or {}
        w = comb.get("worst")
        if w is not None and float(w) < 0:
            neg.append(r)

        pkg = r.get("package_compact", {}) or {}
        if (
            r.get("state") == "RECOVERY_BLOCKED_SHADOW"
            or pkg.get("reason") == "recovery_blocked_trr_budget_exhausted_negative_slc"
        ):
            blocked.append(r)

        if r.get("recovery_package_preview"):
            pending.append(r)

        rf = r.get("recovery_shadow_fill", {}) or {}
        if rf.get("status") in ("PARTIAL_SIGNAL", "FULL_SIGNAL"):
            maker_fill_signal.append(r)

        rmfx = r.get("recovery_maker_fill_exec", {}) or {}
        if rmfx.get("status") == "EXECUTED_SHADOW":
            maker_fill_exec.append(r)
        if rmfx.get("status") == "SKIPPED_UNSAFE":
            maker_fill_unsafe.append(r)

        rex = r.get("second_leg_recovery_exec", {}) or {}
        if rex.get("status") == "EXECUTED_SHADOW":
            before = float(rex.get("current_worst_before"))
            wf = float(rex.get("worst_if_full_preview"))
            imp = float(rex.get("worst_improvement_preview"))
            cap = float(rex.get("capital"))

            if wf < before - 1e-9:
                bad_exec.append(("worse_recovery_exec", r.get("frame"), before, wf, imp))
            if imp <= 0:
                bad_exec.append(("non_positive_recovery_imp", r.get("frame"), imp))
            if cap <= 0 or cap > 2.00001:
                bad_exec.append(("bad_recovery_cap", r.get("frame"), cap))

    last = rows[-1]
    slc = last.get("second_leg_combined_profile", {}) or {}

    print("rows:", len(rows))
    print("frames:", rows[0].get("frame"), "->", rows[-1].get("frame"))
    print()
    print("states:", state_c.most_common(20))
    print("package_modes:", pkg_mode_c.most_common(20))
    print("package_reasons:", pkg_reason_c.most_common(20))
    print("recovery_exec_status:", rex_c.most_common(20))
    print("recovery_shadow_fill_status:", rf_c.most_common(20))
    print("recovery_maker_fill_exec_status:", rmfx_c.most_common(20))
    print()
    print("negative_slc_rows:", len(neg))
    print("blocked_rows:", len(blocked))
    print("pending_maker_recovery_rows:", len(pending))
    print("maker_fill_signal_rows:", len(maker_fill_signal))
    print("maker_fill_exec_rows:", len(maker_fill_exec))
    print("maker_fill_unsafe_rows:", len(maker_fill_unsafe))
    print("bad_exec:", len(bad_exec))
    print()
    print("final_slc_worst:", slc.get("worst"))
    print("final_slc_best:", slc.get("best"))
    print("final_slc_ok:", slc.get("ok"))
    print("final_filled_second_leg:", last.get("filled_second_leg"))

    if pending:
        print()
        print("PENDING SAMPLE:")
        for r in pending[:10]:
            rp = r.get("recovery_package_preview") or {}
            rf = r.get("recovery_shadow_fill") or {}
            print(
                "frame", r.get("frame"),
                "ep", r.get("epoch"),
                "state", r.get("state"),
                "rp", rp.get("mode"),
                "actions", rp.get("action_count"),
                "cap", rp.get("capital"),
                "rf", rf.get("status"),
                "fills", rf.get("filled_actions"),
                "stake", rf.get("filled_stake"),
            )

    for x in bad_exec[:30]:
        print("BAD", x)

    if args.strict:
        ok = (
            slc.get("ok") is True
            and len(bad_exec) == 0
            and len(maker_fill_unsafe) == 0
        )
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
