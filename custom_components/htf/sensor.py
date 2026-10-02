"""HTF Selvbetjening sensor platform."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

DOMAIN = "htf"
BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=6)

_LOGGER = logging.getLogger(__name__)


class HTFClient:
    """Client for the HTF customer portal."""

    def __init__(self, customer: str, pin: str) -> None:
        self.customer = customer
        self.pin = pin
        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/154.0.0.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def fetch(self) -> dict[str, Any]:
        """Log in and retrieve consumption data."""

        login = self.session.post(
            f"{BASE}/umbraco/surface/login2/PostLogin",
            data={
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
            headers={
                "Referer": f"{BASE}/login",
                "Origin": BASE,
            },
            timeout=30,
            allow_redirects=True,
        )

        login.raise_for_status()

        _LOGGER.debug(
            "HTF login response: status=%s url=%s",
            login.status_code,
            login.url,
        )

        # Now request the actual consumption page.
        page = self.session.get(
            f"{BASE}/forbrug/",
            headers={
                "Referer": f"{BASE}/dashboard/",
            },
            timeout=30,
        )

        page.raise_for_status()

        soup = BeautifulSoup(
            page.text,
            "html.parser",
        )

        node = soup.select_one(
            "#consumption-data-json"
        )

        if not node:
            raise RuntimeError(
                "HTF login/session did not provide "
                "#consumption-data-json. "
                "The HTF credentials may be incorrect "
                "or the portal session could not be created."
            )

        raw = node.get_text(
            strip=True
        )

        if not raw:
            raise RuntimeError(
                "HTF consumption-data-json is empty."
            )

        try:
            return json.loads(raw)
        except json.JSONDecodeError as err:
            raise RuntimeError(
                "HTF returned invalid consumption JSON."
            ) from err


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up HTF sensors."""

    client = HTFClient(
        entry.data["customer"],
        entry.data["pin"],
    )

    async def update() -> dict[str, Any]:
        """Fetch HTF data."""
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
    """Base HTF sensor."""

    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, "heating")},
        name="HTF Heating",
        manufacturer="Høje Taastrup Fjernvarme",
        model="Selvbetjening",
    )

    @property
    def meter(self) -> dict[str, Any]:
        """Return the first HTF meter."""
        meters = (
            self.coordinator.data or {}
        ).get("meters", [])

        return (
            meters[0]
            if meters
            else {}
        )


class HTFCurrentMonth(
    HTFBase,
    SensorEntity,
):
    """Current month's HTF consumption."""

    _attr_name = "HTF Heating Current Month"
    _attr_unique_id = (
        "htf_heating_current_month"
    )
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_icon = "mdi:fire"
    _attr_state_class = "total"
    _attr_device_class = "energy"

    @property
    def native_value(self) -> float:
        """Return current month consumption."""
        now = dt_util.now()

        data = (
            self.meter
            .get("monthData", {})
            .get(
                f"year_{now.year}",
                {},
            )
        )

        months = data.get(
            "monthNumbers",
            [],
        )

        values = data.get(
            "values",
            [],
        )

        if now.month not in months:
            return 0.0

        index = months.index(now.month)

        if index >= len(values):
            return 0.0

        return float(values[index])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose all monthly data."""
        return {
            "month_data": self.meter.get(
                "monthData",
                {},
            ),
            "meter_info": self.meter.get(
                "meterInfo",
                {},
            ),
        }


class HTFCurrentYear(
    HTFBase,
    SensorEntity,
):
    """Current year's HTF consumption."""

    _attr_name = "HTF Heating Current Year"
    _attr_unique_id = (
        "htf_heating_current_year"
    )
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_icon = "mdi:fire"
    _attr_state_class = "total"
    _attr_device_class = "energy"

    @property
    def native_value(self) -> float:
        """Return current year consumption."""
        now = dt_util.now()

        data = self.meter.get(
            "yearData",
            {},
        )

        years = data.get(
            "yearNumbers",
            [],
        )

        values = data.get(
            "values",
            [],
        )

        if now.year not in years:
            return 0.0

        index = years.index(now.year)

        if index >= len(values):
            return 0.0

        return float(values[index])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose yearly data."""
        return {
            "year_data": self.meter.get(
                "yearData",
                {},
            ),
            "meter_info": self.meter.get(
                "meterInfo",
                {},
            ),
        }