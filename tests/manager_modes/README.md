# Tests

Behavioural tests for the Zendure Home Assistant integration.
Primarily the `ZendureManager` power-distribution logic across every `ManagerMode`.

## Setup

The suite runs against a real Home Assistant install:

```bash
# Python >= 3.14
uv venv --python 3.14 .venv-ha-test
uv pip install --python .venv-ha-test/bin/python -r requirements.txt -r requirements_test.txt
```

## Running the tests

```bash
# All tests
.venv-ha-test/bin/python -m pytest tests/ 

# Only the manager-mode suite (the CSV-driven conformance tests)
.venv-ha-test/bin/python -m pytest tests/manager_modes

# A single parametrized case, by id
.venv-ha-test/bin/python -m pytest "tests/manager_modes/test_matching.py::test_matching_matches_spec[108:MATCHING:p1=300:pv=200:not full]"
```

## Layout

| Path | Tests | What it covers |
|---|---:|---|
| `manager_modes/test_matching.py` | ~170 | MATCHING mode — CSV-driven conformance |
| `manager_modes/test_matching_discharge.py` | ~84 | MATCHING_DISCHARGE — CSV-driven |
| `manager_modes/test_matching_charge.py` | ~108 | MATCHING_CHARGE — CSV-driven |
| `manager_modes/test_store_solar.py` | ~80 | STORE_SOLAR — CSV-driven |
| `manager_modes/test_manual.py` | ~84 | MANUAL — CSV-driven |
| `manager_modes/test_off.py` | 9 | OFF — no distribution, state = OFF |
| `manager_modes/test_smoke_import.py` | 1 | the real manager imports under the HA stack |
| `manager_modes/test_soc_boundaries.py` | 6 | socSet / minSoc thresholds (SimpleNamespace fakes) |
| `manager_modes/harness.py` | — | plant-model harness (`drive_metered`, `FakeDevice`) |
| `manager_modes/data/*.csv` | — | per-mode spec data (source of truth) |

### CSV columns

| Column | Meaning |
|---|---|
| `case` | unique case number (first column) |
| `mode` | manager mode (`MATCHING`, `MANUAL`, …) |
| `input_w` | P1 meter input (W); MANUAL: the manual power |
| `pv_w`, `soc` | device 1: solar (W), SoC label (`EMPTY` / `FULL` / `not full` / `any`) |
| `battery_discharging_w`, `battery_charging_w`, `device_to_grid_w` | device 1 expected outputs (W) |
| `pv2_w` … `device2_to_grid_w` | device 2 (W, default: none — all five empty = single-device case) |
| `level`, `level2` | explicit electric level (%, default: label representative — EMPTY=5, FULL=100, not full=50); never on `any` rows |
| `fuse_w` | shared fuse-group maxpower (W, default: per-device groups) |
| `notes` | expected scenario description; `PINNED:` marks a known-failing divergence row |
| `offgrid_w`, `offgrid2_w` | off-grid consumers (W, default 0) |
| `exports`, `exports2` | `0` = no export bypass (`gridReverse` disabled; default: `True`) |

An empty cell always means "use the default / not applicable" — each
column lists its default above.

### Two-device rows

CSV rows may describe a second device by filling the optional columns:
`pv2_w`, `soc2`, `battery2_discharging_w`, `battery2_charging_w`,
`device2_to_grid_w` (all five must be filled for a two-device case).
Optional per-device `level`/`level2` override the SoC label's representative
electric level (only on concrete-label rows, never `any`);
optional `fuse_w` wires both devices into one shared fuse group with that maxpower;
optional `offgrid_w`/`offgrid2_w` give a device off-grid consumers (W, default 0);
optional `exports`/`exports2` (`0` = `exports_bypass` False, default True).


Example (MATCHING):

| row | P1 | dev1 | dev2 | fuse | expected dev1 / dev2 |
|---|---|---|---|---|---|
| 203 | 200 | not full, level 75 | not full, level 25 | — | 200/0/200 · 0/0/0 |
| 207 | 1500 | not full, level 75 | not full, level 25 | 1200 | 900/0/900 · 300/0/300 |


## Two testing styles

1. **Data(CSV)-driven conformance** (`manager_modes/test_<mode>.py` + `harness.py`).
   Each row of `manager_modes/<mode>.csv` is a case. The real `powerChanged` is
   driven through a **residual P1 meter** and a physical battery plant until steady state, then 
   the result is asserted against the row (`Device to grid` / `Battery Discharging` /
   `Battery Charging`). `any` SoC rows expand to EMPTY / FULL / not-full.

2. **Command-assertion** (`manager_modes/test_soc_boundaries.py`).
   Build a minimal fake device + bare manager harness from the `SimpleNamespace`
   fakes defined in `test_soc_boundaries.py` and assert on the `power_discharge` /
   `power_charge` **calls** the manager makes.

