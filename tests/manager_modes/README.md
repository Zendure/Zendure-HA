# Tests

Behavioural tests for the Zendure Home Assistant integration.
Primarily the `ZendureManager` power-distribution logic across every `ManagerMode`.

## Setup

The suite runs against a real Home Assistant install, same as upstream CI:

```bash
# Python >= 3.14 (Home Assistant requirement)
uv venv --python 3.14 .venv-ha-test
uv pip install --python .venv-ha-test/bin/python -r requirements.txt -r requirements_test.txt
```

## Running the tests

```bash
# All tests (manager mode conformance + upstream scenario tests)
.venv-ha-test/bin/python -m pytest tests/ -q

# Only the manager-mode suite (the CSV-driven conformance tests)
.venv-ha-test/bin/python -m pytest tests/manager_modes -q

# A single parametrized case, by id (list the exact ids with -v first)
.venv-ha-test/bin/python -m pytest "tests/manager_modes/test_matching.py::test_matching_matches_spec[MATCHING-r108-p1=300-pv=200-not full]"
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
| `conftest.py` | — | upstream `pytest_homeassistant_custom_component` plugin + shared fixtures |
| `manager_modes/harness.py` | — | plant-model harness (`drive_metered`, `FakeDevice`) |
| `manager_modes/*.csv` | — | per-mode spec data (source of truth) |

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

