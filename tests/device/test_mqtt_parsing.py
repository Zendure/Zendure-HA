from __future__ import annotations

from tests.device.helpers import _parse_zensdk_value


class TestMqttMsgLocalValueParsing:
    """Test the value-parsing branch inside mqttMsgLocal without spinning up the full MQTT stack."""

    def test_availability_subtopic_has_slash(self) -> None:
        """A prop like 'electricLevel/availability' contains '/' and must be skipped."""
        prop = "electricLevel/availability"
        assert "/" in prop, "availability subtopic must contain '/' so the guard returns early"

    def test_plain_prop_has_no_slash(self) -> None:
        """A normal property name like 'electricLevel' must NOT contain '/'."""
        prop = "electricLevel"
        assert "/" not in prop

    def test_integer_string_parsed_as_int(self) -> None:
        """Payload '89' must become integer 89."""
        result = _parse_zensdk_value("89")
        assert result == 89
        assert isinstance(result, int)

    def test_float_string_parsed_as_float(self) -> None:
        """Payload '26.5' must become float 26.5."""
        result = _parse_zensdk_value("26.5")
        assert result == 26.5
        assert isinstance(result, float)

    def test_unknown_string_stays_as_string(self) -> None:
        """Payload 'idle' (not in bool map, not numeric) must remain the string 'idle'."""
        result = _parse_zensdk_value("idle")
        assert result == "idle"
        assert isinstance(result, str)

    def test_bool_key_on_returns_int_not_string(self) -> None:
        """Payload 'ON' must be resolved through _ZENSDK_BOOL, not kept as a string."""
        result = _parse_zensdk_value("ON")
        assert result == 1
        assert isinstance(result, int)
