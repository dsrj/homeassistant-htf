from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

DOMAIN = "htf"
BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=6)

_LOGGER = logging.getLogger(__name__)


class HTFClient:
    def __init__(self, customer: str, pin: str):
        self.customer = customer
        self.pin = pin
        self.session = requests.Session()

    def fetch(self) -> dict[str, Any]:
        login = self.session.post(
            f"{BASE}/umbraco/surface/login2/PostLogin",
            data={
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
            timeout=30,
        )
        login.raise_for_status()

        page = self.session.get(
            f"{BASE}/forbrug/",
            timeout=30,
        )
        page.raise_for_status()

        soup = BeautifulSoup(page.text, "html.parser")
        node = soup.select_one("#consumption-data-json")

        if not node:
            raise RuntimeError(
                "HTF consumption-data-json was not found"
            )

        return json.loads(node.get_text(strip=True))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    client = HTFClient(
        entry.data["customer"],
        entry.data["pin"],
    )

    async def update():
        return await hass.async_add_executor_job(
            client.fetch
        )

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="HTF consumption",
        update_method=update,
        update_interval=SCAN_INTERVAL,
    )

    await coordinator.async_config_entry_first_refresh()

    async_add_entities(
        [
            HTFCurrentMonth(coordinator),
            HTFCurrentYear(coordinator),
        ]
    )


class HTFBase(CoordinatorEntity):
    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, "heating")},
        name="HTF Heating",
        manufacturer="Høje Taastrup Fjernvarme",
        model="Selvbetjening",
    )

    @property
    def meter(self):
        meters = (
            self.coordinator.data or {}
        ).get("meters", [])

        return meters[0] if meters else {}


class HTFCurrentMonth(
    HTFBase,
    SensorEntity,
):
    _attr_name = "HTF Heating Current Month"
    _attr_unique_id = "htf_heating_current_month"
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_icon = "mdi:fire"

    @property
    def native_value(self):
        now = dt_util.now()

        data = self.meter.get(
            "monthData",
            {},
        ).get(
            f"year_{now.year}",
            {},
        )

        months = data.get("monthNumbers", [])
        values = data.get("values", [])

        if now.month in months:
            return values[
                months.index(now.month)
            ]

        return 0


class HTFCurrentYear(
    HTFBase,
    SensorEntity,
):
    _attr_name = "HTF Heating Current Year"
    _attr_unique_id = "htf_heating_current_year"
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_icon = "mdi:fire"

    @property
    def native_value(self):
        now = dt_util.now()

        data = self.meter.get(
            "yearData",
            {},
        )

        years = data.get("yearNumbers", [])
        values = data.get("values", [])

        if now.year in years:
            return values[
                years.index(now.year)
            ]

        return 0
