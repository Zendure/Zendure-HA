"""Constants for the Zendure integration."""

from enum import IntEnum
from math import sqrt
from typing import Any

DOMAIN = "zendure_ha"

PANEL_TITLE = "Zendure"

class Phase(IntEnum):
    all = 0
    l1 = 1
    l2 = 2
    l3 = 3

    def __str__(self) -> str:
        return self.name.upper()


PhaseModi = {None: "unknown", Phase.all: "all", Phase.l1: "l1", Phase.l2: "l2", Phase.l3: "l3"}  # the select options: the key is the semantic value, the label its name


def watt_factor(state: Any, phase: Phase = Phase.all) -> float:
    # multiplier that turns a sensor reading into watts; a current needs a voltage, and that follows from the phase
    unit = state.attributes.get("unit_of_measurement") if state is not None else None
    if unit in ("kW", "kilowatt", "kilowatts"):
        return 1000.0
    if unit == "A":
        return 400.0 * sqrt(3) if phase == Phase.all else 230.0
    return 1.0

