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

```text
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
