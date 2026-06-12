"""Tests for the zenSDK http error handling and the manual write retries of ZendureZenSdk."""

import asyncio
import contextlib
import importlib
import json as jsonlib
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

device_module = importlib.import_module("custom_components.zendure_ha.device")

LOGGER_NAME = "custom_components.zendure_ha.device"


class FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    async def text(self) -> str:
        return jsonlib.dumps(self._payload)


class FakeSession:
    """Fake aiohttp session; fails the first `failures` requests with TimeoutError."""

    def __init__(self, failures: int = 0, payload=None) -> None:
        self.failures = failures
        self.payload = payload if payload is not None else {}
        self.get_count = 0
        self.post_attempts = 0
        self.posts: list[dict] = []

    def _maybe_fail(self) -> None:
        if self.failures != 0:
            if self.failures > 0:
                self.failures -= 1
            raise TimeoutError

    async def get(self, _url, **_kwargs) -> FakeResponse:
        self.get_count += 1
        self._maybe_fail()
        return FakeResponse(self.payload)

    async def post(self, _url, json=None, **_kwargs) -> FakeResponse:
        self.post_attempts += 1
        self._maybe_fail()
        # deep copy, the caller reuses/mutates its dicts between attempts
        self.posts.append(jsonlib.loads(jsonlib.dumps(json)))
        return FakeResponse({})


def make_device(session: FakeSession, connection: int = 2):
    """Build a bare ZendureZenSdk without running the entity-heavy constructor."""
    dev = object.__new__(device_module.ZendureZenSdk)
    dev.session = session
    dev.name = "testdevice"
    dev.deviceId = "dev1"
    dev.snNumber = "SN123"
    dev.ipAddress = "127.0.0.1"
    dev.lastseen = datetime.min
    dev.httpid = 0
    dev._messageid = 0
    dev.mqtt = None
    dev.topic_write = "iot/test/dev1/properties/write"
    dev.unreachableSince = None
    dev.unreachableLogged = False
    dev.pendingWrites = {}
    dev.writeTask = None
    dev.connection = SimpleNamespace(value=connection)
    return dev


def set_fast_retries(monkeypatch, interval: float = 0.01, window: float = 0.25) -> None:
    monkeypatch.setattr(device_module, "CONST_WRITE_RETRY_INTERVAL", interval)
    monkeypatch.setattr(device_module, "CONST_WRITE_RETRY_WINDOW", window)


def errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------- httpGet


def test_httpget_success_returns_payload_and_key():
    async def run():
        session = FakeSession(payload={"properties": {"electricLevel": 50}})
        dev = make_device(session)
        assert await dev.httpGet("properties/report") == {
            "properties": {"electricLevel": 50}
        }
        assert await dev.httpGet("properties/report", "properties") == {
            "electricLevel": 50
        }
        assert await dev.httpGet("properties/report", "missing") == {}
        assert dev.lastseen > datetime.min

    asyncio.run(run())


def test_httpget_failure_returns_empty_and_marks_offline(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def run():
        dev = make_device(FakeSession(failures=-1))
        assert await dev.httpGet("properties/report") == {}
        assert dev.lastseen == datetime.min
        assert dev.unreachableSince is not None

    asyncio.run(run())
    assert errors(caplog) == []
    assert any(
        "TimeoutError" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    )


def test_http_error_logged_only_once_after_one_minute(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def run():
        dev = make_device(FakeSession(failures=-1))
        await dev.httpGet("properties/report")
        assert errors(caplog) == []

        # second failure within the first minute is still not an error
        await dev.httpGet("properties/report")
        assert errors(caplog) == []

        # pretend the outage started more than a minute ago
        dev.unreachableSince = datetime.now() - timedelta(seconds=61)
        await dev.httpGet("properties/report")
        assert len(errors(caplog)) == 1
        assert dev.unreachableLogged

        # subsequent failures do not repeat the error
        await dev.httpGet("properties/report")
        assert len(errors(caplog)) == 1

    asyncio.run(run())


def test_recovery_resets_state_and_logs(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def run():
        dev = make_device(FakeSession(failures=1))
        await dev.httpGet("properties/report")
        dev.unreachableSince = datetime.now() - timedelta(seconds=61)
        dev.unreachableLogged = True

        await dev.httpGet("properties/report")
        assert dev.unreachableSince is None
        assert not dev.unreachableLogged
        assert dev.lastseen > datetime.min
        assert any("reachable again" in r.message for r in caplog.records)

        # next outage starts a fresh grace period without an immediate error
        caplog.clear()
        dev.session = FakeSession(failures=-1)
        await dev.httpGet("properties/report")
        assert errors(caplog) == []

    asyncio.run(run())


def test_http_success_does_not_reduce_lastseen():
    async def run():
        dev = make_device(FakeSession())
        future = datetime.now() + timedelta(minutes=5)
        dev.lastseen = future
        await dev.httpGet("properties/report")
        assert dev.lastseen == future

    asyncio.run(run())


# ---------------------------------------------------------------- httpPost


def test_httppost_adds_id_and_sn():
    async def run():
        session = FakeSession()
        dev = make_device(session)
        assert await dev.httpPost("properties/write", {"properties": {"minSoc": 20}})
        assert await dev.httpPost("properties/write", {"properties": {"minSoc": 30}})
        assert [p["id"] for p in session.posts] == [1, 2]
        assert all(p["sn"] == "SN123" for p in session.posts)
        assert dev.lastseen > datetime.min

    asyncio.run(run())


def test_httppost_failure_returns_false_without_error_log(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def run():
        dev = make_device(FakeSession(failures=-1))
        assert not await dev.httpPost(
            "properties/write", {"properties": {"minSoc": 20}}
        )
        assert dev.lastseen == datetime.min

    asyncio.run(run())
    assert errors(caplog) == []


# ---------------------------------------------------------------- manual writes (httpWrite)


def test_httpwrite_success_needs_no_retry():
    async def run():
        session = FakeSession()
        dev = make_device(session)
        await dev.httpWrite({"minSoc": 20})
        assert session.posts[0]["properties"] == {"minSoc": 20}
        assert dev.pendingWrites == {}
        assert dev.writeTask is None

    asyncio.run(run())


def test_httpwrite_retries_until_device_is_back(monkeypatch):
    set_fast_retries(monkeypatch)

    async def run():
        session = FakeSession(failures=3)
        dev = make_device(session)
        await dev.httpWrite({"minSoc": 20})
        assert dev.pendingWrites == {"minSoc": 20}
        assert dev.writeTask is not None

        await dev.writeTask
        assert dev.pendingWrites == {}
        assert session.posts[-1]["properties"] == {"minSoc": 20}

    asyncio.run(run())


def test_httpwrite_latest_value_wins(monkeypatch):
    set_fast_retries(monkeypatch)

    async def run():
        session = FakeSession(failures=-1)
        dev = make_device(session)
        await dev.httpWrite({"outputLimit": 50})
        task = dev.writeTask
        assert task is not None

        # a new manual value for the same property replaces the pending one
        await dev.httpWrite({"outputLimit": 120})
        assert dev.writeTask is task, "no second retry task should be started"
        assert dev.pendingWrites == {"outputLimit": 120}

        session.failures = 0
        await task
        assert dev.pendingWrites == {}
        assert session.posts[-1]["properties"] == {"outputLimit": 120}

    asyncio.run(run())


def test_httpwrite_merges_different_properties(monkeypatch):
    set_fast_retries(monkeypatch)

    async def run():
        session = FakeSession(failures=-1)
        dev = make_device(session)
        await dev.httpWrite({"minSoc": 20})
        await dev.httpWrite({"socSet": 90})
        assert dev.pendingWrites == {"minSoc": 20, "socSet": 90}

        session.failures = 0
        await dev.writeTask
        assert dev.pendingWrites == {}
        assert session.posts[-1]["properties"] == {"minSoc": 20, "socSet": 90}

    asyncio.run(run())


def test_httpwrite_gives_up_after_retry_window(monkeypatch, caplog):
    set_fast_retries(monkeypatch, interval=0.01, window=0.05)
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

    async def run():
        dev = make_device(FakeSession(failures=-1))
        await dev.httpWrite({"minSoc": 20})
        await dev.writeTask
        assert dev.pendingWrites == {}

    asyncio.run(run())
    assert any("Unable to write" in r.message for r in errors(caplog))


def test_httpwrite_while_retrying_does_not_post_concurrently(monkeypatch):
    set_fast_retries(monkeypatch, interval=1.0, window=5.0)

    async def run():
        session = FakeSession(failures=-1)
        dev = make_device(session)
        await dev.httpWrite({"outputLimit": 50})
        assert session.post_attempts == 1
        task = dev.writeTask
        assert task is not None

        # while the retry task is active, a new write is merged but not posted immediately
        await dev.httpWrite({"minSoc": 20})
        assert session.post_attempts == 1
        assert dev.pendingWrites == {"outputLimit": 50, "minSoc": 20}

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())


def test_clearpending_keeps_replaced_value():
    dev = make_device(FakeSession())
    dev.pendingWrites = {"outputLimit": 120, "minSoc": 20}
    # outputLimit was replaced while {"outputLimit": 50, "minSoc": 20} was in flight
    dev.clearPending({"outputLimit": 50, "minSoc": 20})
    assert dev.pendingWrites == {"outputLimit": 120}


# ---------------------------------------------------------------- regulation commands (doCommand)


def test_docommand_does_not_retry(monkeypatch):
    set_fast_retries(monkeypatch)

    async def run():
        session = FakeSession(failures=-1)
        dev = make_device(session)
        await dev.doCommand(
            {
                "properties": {
                    "smartMode": 1,
                    "acMode": 2,
                    "outputLimit": 100,
                    "inputLimit": 0,
                }
            }
        )
        assert dev.writeTask is None
        assert dev.pendingWrites == {}
        assert session.posts == []

    asyncio.run(run())


def test_docommand_success_supersedes_pending_manual_writes():
    async def run():
        session = FakeSession()
        dev = make_device(session)
        dev.pendingWrites = {"outputLimit": 50, "minSoc": 20}
        await dev.doCommand(
            {
                "properties": {
                    "smartMode": 1,
                    "acMode": 2,
                    "outputLimit": 100,
                    "inputLimit": 0,
                }
            }
        )
        assert dev.pendingWrites == {"minSoc": 20}

    asyncio.run(run())


def test_docommand_cloud_mode_uses_mqtt_not_http():
    async def run():
        session = FakeSession()
        dev = make_device(session, connection=0)
        await dev.doCommand({"properties": {"outputLimit": 100}})
        assert session.posts == []

    asyncio.run(run())


# ---------------------------------------------------------------- reboot scenario


def test_command_during_30s_reboot_is_applied_afterwards(monkeypatch):
    """The device reboots for ~30s every 11 minutes without internet; a manual command sent then must not be lost."""
    set_fast_retries(monkeypatch, interval=0.01, window=1.0)

    async def run():
        session = FakeSession(failures=-1)
        dev = make_device(session)
        await dev.httpWrite({"acMode": 1})

        # device comes back a couple of retry intervals later
        await asyncio.sleep(0.03)
        session.failures = 0
        await dev.writeTask
        assert dev.pendingWrites == {}
        assert session.posts[-1]["properties"] == {"acMode": 1}
        assert dev.lastseen > datetime.min

    asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
