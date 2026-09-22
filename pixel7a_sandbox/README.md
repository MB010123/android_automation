# Pixel 7a #1 sandbox toolset

Standalone host tools for serial `3C071JEHN14705` only. This directory is
not part of the 20-bay farm daemon. Do not run `mobi_rent_agent/main.py`
from here. Do not load `slot_map.json`.

```bash
python pixel7a_sandbox/pixel7a_lifecycle_validate.py --serial 3C071JEHN14705
python pixel7a_sandbox/pixel7a_live_download.py --switch-after-download --serial 3C071JEHN14705
python pixel7a_sandbox/sandbox_switch_esim.py --serial 3C071JEHN14705
```

`--force-download` on the lifecycle script invokes EuiccManager even when a
profile is already enabled. Leave it off for repeatable verification of an
already-active eSIM.

Activation codes stay in `mobi_rent_agent/.env` as `PIXEL7A_ACTIVATION_CODE`.
Never print them.
