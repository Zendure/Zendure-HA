from __future__ import annotations

from custom_components.zendure_ha.api import find_zensdk_sn


def _mixed_device_list() -> list:
    """Cloud legacy device first, zenSDK device second (appended by local discovery merge)."""
    return [
        {"snNumber": "HYP001", "productModel": "hyper2000", "ip": ""},
        {"snNumber": "EOD1NLN9P010318", "productModel": "solarFlow800Pro2", "ip": "192.168.10.80"},
    ]


class TestFindZensdkSn:
    """find_zensdk_sn must return the SN matching device_ip, not index 0."""

    def test_mixed_setup_returns_zensdk_sn(self) -> None:
        """In a mixed list the zenSDK device is NOT index 0 — must match by ip."""
        sn = find_zensdk_sn(_mixed_device_list(), "192.168.10.80")

        assert sn == "EOD1NLN9P010318", (
            f"Expected zenSDK SN 'EOD1NLN9P010318', got '{sn}'. "
            "device_list[0] is a legacy cloud device — wrong SN would be sent."
        )

    def test_single_device_returns_its_sn(self) -> None:
        """Single-device list with matching ip → correct SN."""
        device_list = [{"snNumber": "EOD1NLN9P010318", "ip": "192.168.10.80"}]
        assert find_zensdk_sn(device_list, "192.168.10.80") == "EOD1NLN9P010318"

    def test_no_ip_match_returns_empty(self) -> None:
        """No device with matching ip → empty string, not a wrong SN."""
        assert find_zensdk_sn(_mixed_device_list(), "192.168.10.99") == ""

    def test_empty_list_returns_empty(self) -> None:
        """Empty device list → empty string, no IndexError."""
        assert find_zensdk_sn([], "192.168.10.80") == ""
