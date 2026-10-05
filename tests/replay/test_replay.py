"""Replay every recording in this folder through the live Distribution and hold the meter figures at their pinned level."""

import json
import os
from pathlib import Path

import pytest

from custom_components.zendure_ha.distribution import DistributionModi
from replay import replay

HERE = Path(__file__).parent
PINS = HERE / "benchmarks.json"  # per recording total_wh, median, p90, within100 and optionally the soc every pack starts at
SMART = next(k for k, v in DistributionModi.items() if v == "smart")


@pytest.mark.parametrize("name", sorted(p.name for p in HERE.glob("*.json") if p != PINS))
async def test_replay_benchmark(hass, entry, name: str) -> None:  # noqa: ANN001
    pins = json.loads(PINS.read_text(encoding="utf-8")) if PINS.is_file() else {}
    pin = pins.get(name, {})
    data = json.loads((HERE / name).read_text(encoding="utf-8"))
    if (soc := pin.get("soc")) is not None:  # a control-off window: every pack starts at this level; a device without a pack keeps its 0
        kwh = {spec["key"]: spec["kwh"] for spec in data["devices"]}
        for key, series in data["soc"].items():
            if kwh[key]:
                series[0] = soc
    r = await replay(hass, entry.entry_id, data, SMART)
    print(f"\n{name}  {r['hours']:.1f} h  import {r['import_wh']:.0f} Wh  export {r['export_wh']:.0f} Wh  total {r['total_wh']:.0f} Wh  recorded {r['recorded_wh']:.0f} Wh")
    print(f"  median {r['median']:.0f} W  p90 {r['p90']:.0f} W  p99 {r['p99']:.0f} W  within 100 W {r['within'][100]:.1f}%  switches {r['switches']}")
    assert r["hours"] > 0, f"{name} replayed nothing"

    # REPLAY_REPIN=1 pytest tests/replay -s: measure and write the numbers back; without a pin the recording only reports
    if os.environ.get("REPLAY_REPIN") or not pin:
        pins[name] = {**({"soc": soc} if soc is not None else {}), "total_wh": round(r["total_wh"]), "median": round(r["median"]), "p90": round(r["p90"]), "within100": round(r["within"][100], 1)}
        PINS.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
        return

    # one-sided: worse on any measure fails; far better means the pin is stale, re-pin so a later regression cannot hide under it
    assert r["total_wh"] <= pin["total_wh"] * 1.01 + 1, f"{name}: {r['total_wh']:.0f} Wh against {pin['total_wh']} Wh"
    assert r["median"] <= pin["median"] * 1.01 + 1, f"{name}: median {r['median']:.0f} W against {pin['median']} W"
    assert r["p90"] <= pin["p90"] * 1.01 + 1, f"{name}: p90 {r['p90']:.0f} W against {pin['p90']} W"
    assert r["within"][100] >= pin["within100"] - 1.0, f"{name}: within 100 W {r['within'][100]:.1f}% against {pin['within100']}%"
    assert r["total_wh"] >= pin["total_wh"] * 0.95 - 1, f"{name}: {r['total_wh']:.0f} Wh well under the pinned {pin['total_wh']} Wh, re-pin with REPLAY_REPIN=1"
