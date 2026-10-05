"""
Threshold loader for CNI insights detectors.

Loads `thresholds.json` once at import time and exposes the BATTERY and SOLAR
dicts. Detectors should read values from these dicts so operating thresholds
can be tuned without code changes.

Usage:
    from thresholds import BATTERY, SOLAR

    target_value = BATTERY["high_cell_temp"]["max_temp_c"]
"""

import json
from pathlib import Path

_THRESHOLDS_PATH = Path(__file__).parent / "thresholds.json"

with open(_THRESHOLDS_PATH, "r") as _f:
    _THRESHOLDS = json.load(_f)

BATTERY = _THRESHOLDS["battery"]
SOLAR   = _THRESHOLDS["solar"]
