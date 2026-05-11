"""Integration test — real mDNS network scan for Zendure devices.

These tests hit the actual local network and require at least one
Zendure device to be powered on and reachable via multicast DNS.
They are skipped automatically when no device is found so they
never break CI on machines without a Zendure on the network.

Run manually:
    pytest tests/integration/test_mdns_discovery.py -v -s
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

import pytest
from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf

# Service types to probe — _http._tcp confirmed live; _zendure._tcp per zenSDK docs
_SERVICE_TYPES = ["_http._tcp.local.", "_zendure._tcp.local."]
_NAME_PREFIX = "Zendure-"
_SCAN_TIMEOUT = 5  # seconds — enough for one mDNS round-trip on a local network


@dataclass
class DiscoveredDevice:
    """A Zendure device found via mDNS."""

    name: str
    host: str
    port: int
    service_type: str
    properties: dict[bytes, bytes] = field(default_factory=dict)

    @property
    def sn(self) -> str:
        """Serial number extracted from the mDNS name."""
        import re

        raw = self.name.split("._")[0]
        match = re.search(r"-([A-Z0-9]{8,})$", raw)
        return match.group(1) if match else raw.split("-")[-1]


def scan_for_zendure_devices(timeout: int = _SCAN_TIMEOUT) -> list[DiscoveredDevice]:
    """Scan the local network for Zendure devices via mDNS.

    Browses all configured service types and collects every service
    whose name starts with 'Zendure-'.
    """
    found: list[DiscoveredDevice] = []
    zc = Zeroconf()

    def on_service_state_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        if not name.startswith(_NAME_PREFIX):
            return

        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            return

        addresses = info.parsed_scoped_addresses()
        host = addresses[0] if addresses else ""
        if not host:
            # fallback: resolve via addresses bytes
            try:
                host = socket.inet_ntoa(info.addresses[0])
            except (IndexError, OSError):
                host = ""

        found.append(
            DiscoveredDevice(
                name=name,
                host=host,
                port=info.port,
                service_type=service_type,
                properties=info.properties or {},
            )
        )

    browsers = [
        ServiceBrowser(zc, svc_type, handlers=[on_service_state_change])
        for svc_type in _SERVICE_TYPES
    ]

    time.sleep(timeout)

    for browser in browsers:
        browser.cancel()
    zc.close()

    return found


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def zendure_devices() -> list[DiscoveredDevice]:
    """Scan once per module and share results across tests."""
    return scan_for_zendure_devices()


@pytest.fixture(scope="module")
def require_device(zendure_devices: list[DiscoveredDevice]):
    """Skip the test if no Zendure device was found on the network."""
    if not zendure_devices:
        pytest.skip(
            "No Zendure device found on the local network — skipping integration test"
        )
    return zendure_devices


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMdnsDiscovery:
    """Real mDNS network scan — requires a powered-on Zendure device."""

    def test_at_least_one_device_found(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """Verify at least one Zendure device announces itself via mDNS."""
        assert len(require_device) >= 1, "Expected at least one Zendure device"

    def test_device_name_starts_with_zendure(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """Every discovered entry must carry the Zendure- prefix."""
        for device in require_device:
            assert device.name.startswith(_NAME_PREFIX), (
                f"Unexpected name: {device.name!r}"
            )

    def test_device_has_valid_ip(self, require_device: list[DiscoveredDevice]) -> None:
        """Each device must expose a reachable IP address."""
        for device in require_device:
            assert device.host, f"No IP for {device.name!r}"
            # Basic sanity: parseable as IPv4
            parts = device.host.split(".")
            assert len(parts) == 4, f"Not a valid IPv4: {device.host!r}"

    def test_device_has_valid_port(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """Port must be a usable TCP port number."""
        for device in require_device:
            assert 1 <= device.port <= 65535, (
                f"Invalid port {device.port} for {device.name!r}"
            )

    def test_sn_extractable(self, require_device: list[DiscoveredDevice]) -> None:
        """Serial number must be extractable from every device name."""
        for device in require_device:
            sn = device.sn
            assert sn, f"Could not extract SN from {device.name!r}"
            assert len(sn) >= 6, f"SN suspiciously short: {sn!r} from {device.name!r}"

    def test_http_tcp_service_type_present(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """At least one device should announce via _http._tcp.local. (confirmed)."""
        http_devices = [d for d in require_device if "_http._tcp" in d.service_type]
        assert http_devices, (
            "No device found on _http._tcp.local. — "
            "only _zendure._tcp devices present (unexpected)"
        )

    def test_print_discovered_devices(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """Print a human-readable summary (always passes, useful with -s)."""
        print(f"\n{'=' * 60}")
        print(f"Zendure devices found on local network: {len(require_device)}")
        print(f"{'=' * 60}")
        for d in require_device:
            print(f"  Name  : {d.name}")
            print(f"  SN    : {d.sn}")
            print(f"  Host  : {d.host}:{d.port}")
            print(f"  Type  : {d.service_type}")
            if d.properties:
                print(f"  Props : {d.properties}")
            print()


class TestMdnsZendureTcp:
    """_zendure._tcp service type — per zenSDK docs, unconfirmed on live hardware."""

    def test_zendure_tcp_devices_if_present(
        self, zendure_devices: list[DiscoveredDevice]
    ) -> None:
        """If _zendure._tcp devices are found, they must have valid IP and SN."""
        zendure_tcp = [d for d in zendure_devices if "_zendure._tcp" in d.service_type]
        if not zendure_tcp:
            pytest.skip(
                "No _zendure._tcp device found (expected — unconfirmed service type)"
            )
        for d in zendure_tcp:
            assert d.host, f"No IP for {d.name!r}"
            assert d.sn, f"No SN for {d.name!r}"

    def test_zendure_tcp_name_format(
        self, zendure_devices: list[DiscoveredDevice]
    ) -> None:
        """Name format per docs: 'Zendure-<Model>-<last12Mac>' on _zendure._tcp."""
        import re

        zendure_tcp = [d for d in zendure_devices if "_zendure._tcp" in d.service_type]
        if not zendure_tcp:
            pytest.skip("No _zendure._tcp device on the network")
        for d in zendure_tcp:
            raw = d.name.split("._")[0]
            # zenSDK docs say last segment is last 12 chars of MAC (hex, uppercase)
            # Our regex also matches serial numbers — both are alphanum ≥8 chars
            assert re.search(r"-([A-Z0-9]{8,})$", raw), (
                f"Name segment does not match expected SN/MAC format: {raw!r}"
            )


class TestLocalDiscoveryHttp:
    """Verify /properties/report HTTP call returns model + SN for live devices."""

    def test_local_discovery_returns_device_info(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """GET /properties/report on a live device returns snNumber and productModel."""
        import json
        import urllib.request

        device = require_device[0]
        url = f"http://{device.host}:{device.port}/properties/report"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
        except OSError as exc:
            pytest.skip(f"Could not reach {url}: {exc}")

        # /properties/report returns the raw device payload: sn + product at top level
        assert data.get("sn"), f"sn missing in /properties/report response: {data!r}"
        assert data.get("product"), f"product missing in /properties/report response: {data!r}"

    def test_local_discovery_sn_matches_mdns(
        self, require_device: list[DiscoveredDevice]
    ) -> None:
        """SN from /properties/report must match the SN extracted from the mDNS name."""
        import json
        import urllib.request

        device = require_device[0]
        url = f"http://{device.host}:{device.port}/properties/report"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
        except OSError as exc:
            pytest.skip(f"Could not reach {url}: {exc}")

        http_sn = data.get("sn", "")
        if not http_sn:
            pytest.skip("/properties/report did not return an sn field")

        assert http_sn == device.sn, (
            f"SN mismatch: mDNS={device.sn!r}, /properties/report={http_sn!r}"
        )


class TestMdnsScanWithoutDevice:
    """Validates scan behavior when no device is on the network.

    These tests run regardless of network state — they test the
    scanner logic itself, not the presence of a physical device.
    """

    def test_scan_returns_list(self) -> None:
        """scan_for_zendure_devices always returns a list, never raises."""
        # Very short timeout — we just check the return type, not real discovery
        result = scan_for_zendure_devices(timeout=1)
        assert isinstance(result, list)

    def test_discovered_device_sn_extraction(self) -> None:
        """DiscoveredDevice.sn extracts the serial correctly from known name formats."""
        device = DiscoveredDevice(
            name="Zendure-solarFlow800Pro2-EOD1NLN9P010318._http._tcp.local.",
            host="192.168.10.80",
            port=80,
            service_type="_http._tcp.local.",
        )
        assert device.sn == "EOD1NLN9P010318"

    def test_discovered_device_sn_fallback(self) -> None:
        """Short/unknown SN falls back to last dash-segment without raising."""
        device = DiscoveredDevice(
            name="Zendure-unknownModel-abc123._http._tcp.local.",
            host="192.168.10.80",
            port=80,
            service_type="_http._tcp.local.",
        )
        assert device.sn == "abc123"

    def test_zendure_tcp_name_format_regex(self) -> None:
        """zenSDK MAC-based name format is also matched by our SN regex."""
        import re

        # zenSDK docs example: Zendure-SolarFlow800-WOB1NHMAMXXXXX3
        device = DiscoveredDevice(
            name="Zendure-SolarFlow800-WOB1NHMAMXXXXX3._zendure._tcp.local.",
            host="192.168.10.80",
            port=80,
            service_type="_zendure._tcp.local.",
        )
        assert re.search(r"-([A-Z0-9]{8,})$", device.name.split("._")[0])
        assert device.sn == "WOB1NHMAMXXXXX3"
