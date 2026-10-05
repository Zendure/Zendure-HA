"""The manifest declares exactly what the driver matches on."""

import json
from pathlib import Path

from custom_components.zendure_ha.zendure.driver import ZendureDriver

MANIFEST = Path(__file__).resolve().parents[1] / "custom_components" / "zendure_ha" / "manifest.json"


def test_manifest_matchers_are_the_drivers_matchers() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["zeroconf"] == ZendureDriver.ZEROCONF
    assert manifest["bluetooth"] == ZendureDriver.BLUETOOTH
    assert "bluetooth" in manifest["dependencies"]
    assert manifest["single_config_entry"] is True
