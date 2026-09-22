# Full-box SMS test configuration (preparation only)

Prepared: `2026-09-21T19:21:44.504420+00:00` · Run id: `20260921T192144Z`

**No SMS was sent during this step.**

## Outbound strategy (recommended)

All 20 slots → **+19522287088** (1 distinct recipient).

| Slot | ADB serial | VoidFix | SIM2 MSISDN | Outbound recipient | Expected inbound reply target |
|------|------------|---------|-------------|-------------------|------------------------------|
| 1 | `18171FDF6005WG` | 1386 | +19522287088 | +19522287088 | +19522287088 |
| 2 | `19141FDF6OO8T9` | 1389 | +16514722709 | +19522287088 | +16514722709 |
| 3 | `19161FDF6004AD` | 1393 | +17633287165 | +19522287088 | +17633287165 |
| 4 | `19161FDF6005B9` | 1394 | +17633468225 | +19522287088 | +17633468225 |
| 5 | `19161FDF6OO411` | 1395 | +16126499401 | +19522287088 | +16126499401 |
| 6 | `19281FDF6OO1T4` | 1402 | +17633390175 | +19522287088 | +17633390175 |
| 7 | `1A181FDF6006KN` | 1403 | +16126499481 | +19522287088 | +16126499481 |
| 8 | `1B131FDF60090W` | 1404 | +17633573736 | +19522287088 | +17633573736 |
| 9 | `1B301FDF6004HF` | 1405 | +16126499603 | +19522287088 | +16126499603 |
| 10 | `1C021FDF600GKL` | 1406 | +17633406158 | +19522287088 | +17633406158 |
| 11 | `1C071FDF6004MS` | 1407 | +16126499651 | +19522287088 | +16126499651 |
| 12 | `1C071FDF6006YS` | 1408 | +17634382664 | +19522287088 | +17634382664 |
| 13 | `1C111FDF600CNB` | 1409 | +16126499683 | +19522287088 | +16126499683 |
| 14 | `1C141FDF600GXF` | 1410 | +17633935186 | +19522287088 | +17633935186 |
| 15 | `21051FDF600EM9` | 1411 | +16126499573 | +19522287088 | +16126499573 |
| 16 | `23101FDF60058N` | 1412 | +17634384448 | +19522287088 | +17634384448 |
| 17 | `25061FDF6006KF` | 1413 | +17633887556 | +19522287088 | +17633887556 |
| 18 | `25261FDF60017D` | 1414 | +17634388552 | +19522287088 | +17634388552 |
| 19 | `1C101FDF6009EZ` | 1415 | +16126499751 | +19522287088 | +16126499751 |
| 20 | `1A271FDF600BX2` | 1417 | +17633935255 | +19522287088 | +17633935255 |

## Allowlist

Current: `19522287088`

**Central hub test:** no additions required.

**Ring test (optional):** would need **19** additions: 16126499401, 16126499481, 16126499573, 16126499603, 16126499651, 16126499683, 16126499751, 16514722709, 17633287165, 17633390175, 17633406158, 17633468225, 17633573736, 17633887556, 17633935186, 17633935255, 17634382664, 17634384448, 17634388552

## Inbound verification (summary)

1. Hub replies once per slot to each **Expected inbound reply target** with the planned `IN slot=NN` token.
2. Match `device_id` from webhook/`ingest_inbound` to slot; confirm `from_number` is the hub.
3. Outbound leg: outbox + `deliveredDate` per `provider_message_id`.

## Eventual command path

`python tools/fullbox_sms_simultaneous_test.py --config _tmp_fullbox_test_config.json --execute`

(Tool sends only after explicit `--execute`; uses `build_sms_dispatch_service`.)
