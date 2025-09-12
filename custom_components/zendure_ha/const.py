"""Constants for Zendure."""

from datetime import timedelta
from enum import Enum

DOMAIN = "zendure_ha"

CONF_APPTOKEN = "token"
CONF_P1METER = "p1meter"
CONF_PRICE = "price"
CONF_MQTTLOG = "mqttlog"
CONF_MQTTLOCAL = "mqttlocal"
CONF_MQTTSERVER = "mqttserver"
CONF_MQTTPORT = "mqttport"
CONF_MQTTUSER = "mqttuser"
CONF_MQTTPSW = "mqttpsw"
CONF_WIFISSID = "wifissid"
CONF_WIFIPSW = "wifipsw"

CONF_HAKEY = "C*dafwArEOXK"


class AcMode:
    INPUT = 1
    OUTPUT = 2


class DeviceState(Enum):
    OFFLINE = 0
    SOCEMPTY = 1
    SOCFULL = 2
    INACTIVE = 3
    STARTING = 4
    ACTIVE = 5


class ManagerState(Enum):
    IDLE = 0
    CHARGING = 1
    DISCHARGING = 2
    WAITING = 3


class SmartMode:
    NONE = 0
    MANUAL = 1
    MATCHING = 2
    MATCHING_DISCHARGE = 3
    MATCHING_CHARGE = 4
    FAST_UPDATE = 100
    MIN_POWER = 50
    START_POWER = 100
    TIMEFAST = 2.2
    TIMEZERO = 4
    TIMEIDLE = 10
    TIMERESET = 150
    Threshold = 3.5
    P1_MIN_UPDATE = timedelta(milliseconds=400)
    IGNORE_DELTA = 3
    ZENSDK = 2
    CONNECTED = 10
    SOCFULL = 1
    SOCEMPTY = 2
    MIN_SOC_CHARGE_WINDOW = 2
    KWHSTEP = 0.5
