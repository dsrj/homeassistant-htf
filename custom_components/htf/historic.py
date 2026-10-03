"""HTF historical portal data sensors."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.components.sensor import SensorEntity

from . import DOMAIN

BASE = "https://selvbetjening.htf.dk"

# Historical data only needs to be refreshed occasionally.
# The HTF portal history is refreshed every 15 days.
SCAN_INTERVAL = timedelta(days=15)

_LOGGER = logging.getLogger(__name__)


class HTFHistoricClient:
    """Standalone client for downloading HTF portal history."""

    def __init__(
        self,
        customer: str,
        pin: str,
    ) -> None:
        """Initialize."""
        self.customer = customer
        self.pin = pin

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Home Assistant HTF historical data)"
                )
            }
        )

        self.dashboard_html = ""

    def login(self) -> None:
        """Log in to HTF."""
        response = self.session.get(
            f"{BASE}/login",
            timeout=30,
        )
        response.raise_for_status()

        response = self.session.post(
            f"{BASE}/umbraco/surface/login2/PostLogin",
            data={
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded; "
                    "charset=UTF-8"
                )
            },
            timeout=30,
        )

        response.raise_for_status()

        response = self.session.get(
            f"{BASE}/dashboard/",
            timeout=30,
        )

        response.raise_for_status()

        if (
            "/login" in response.url.lower()
            or "subheader-consumptionpoint-dropdown"
            not in response.text
        ):
            raise UpdateFailed(
                "HTF historical data login was not accepted"
            )

        self.dashboard_html = response.text

    def select_consumption_point(self) -> None:
        """Select the active HTF consumption point."""
        soup = BeautifulSoup(
            self.dashboard_html,
            "html.parser",
        )

        dropdown = soup.select_one(
            "select.subheader-consumptionpoint-dropdown"
        )

        if dropdown is None:
            raise UpdateFailed(
                "HTF consumption point dropdown not found"
            )

        option = dropdown.find(
            "option",
            selected=True,
        )

        if option is None:
            option = dropdown.find(
                "option"
            )

        if option is None:
            raise UpdateFailed(
                "HTF consumption point not found"
            )

        value = option.get("value")

        if not value:
            raise UpdateFailed(
                "HTF consumption point value is empty"
            )

        parts = [
            part.strip()
            for part in value.split(";")
        ]

        if len(parts) < 4:
            raise UpdateFailed(
                "Unexpected HTF consumption point format"
            )

        response = self.session.post(
            f"{BASE}/umbraco/surface/customer2/"
            "SetSelectedConsumption",
            data={
                "ConsumptionPointId": parts[0],
                "ConsumerId": parts[1],
                "DebtorId": parts[2],
                "CustomerId": parts[3],
            },
            timeout=30,
        )

        response.raise_for_status()

    def page(
        self,
        path: str,
    ) -> str:
        """Download an authenticated HTF page."""
        response = self.session.get(
            f"{BASE}{path}",
            timeout=30,
        )

        response.raise_for_status()

        if "/login" in response.url.lower():
            raise UpdateFailed(
                "HTF historical data session expired"
            )

        return response.text

    @staticmethod
    def extract_json(
        html: str,
    ) -> dict[str, Any]:
        """Extract HTF portal JSON."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        element = soup.select_one(
            "#consumption-data-json"
        )

        if element is None:
            raise UpdateFailed(
                "HTF portal JSON element not found"
            )

        raw = element.get_text(
            strip=True
        )

        if not raw:
            raise UpdateFailed(
                "HTF portal JSON is empty"
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise UpdateFailed(
                "HTF portal JSON is invalid"
            ) from err

        if not isinstance(data, dict):
            raise UpdateFailed(
                "HTF portal JSON has unexpected format"
            )

        return data

    @staticmethod
    def extract_tables(
        html: str,
    ) -> list[list[list[str]]]:
        """Extract billing tables exactly as displayed by HTF."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        tables: list[list[list[str]]] = []

        for table in soup.find_all("table"):
            rows: list[list[str]] = []

            for row in table.find_all("tr"):
                cells = [
                    cell.get_text(
                        " ",
                        strip=True,
                    )
                    for cell in row.find_all(
                        ["th", "td"]
                    )
                ]

                if cells:
                    rows.append(cells)

            if rows:
                tables.append(rows)

        return tables

    def fetch(
        self,
    ) -> dict[str, Any]:
        """Download all three HTF historical datasets."""
        self.login()
        self.select_consumption_point()

        # ---------------------------------------------------------
        # 1. CONSUMPTION HISTORY
        # ---------------------------------------------------------

        consumption_html = self.page(
            "/forbrug/"
        )

        consumption = self.extract_json(
            consumption_html
        )

        # ---------------------------------------------------------
        # 2. BILLING HISTORY
        # ---------------------------------------------------------

        bills_html = self.page(
            "/kundeoplysninger/kontoudtog/"
        )

        bill_tables = self.extract_tables(
            bills_html
        )

        # ---------------------------------------------------------
        # 3. RETURN TEMPERATURE HISTORY
        # ---------------------------------------------------------

        return_temperature_html = self.page(
            "/returtemperatur/"
        )

        return_temperature = self.extract_json(
            return_temperature_html
        )

        _LOGGER.info(
            "HTF historical portal data downloaded successfully"
        )

        return {
            "consumption": consumption,
            "bills": bill_tables,
            "return_temperature": return_temperature,
        }


class HTFHistoricCoordinator(
    DataUpdateCoordinator[dict[str, Any]]
):
    """Coordinate historical HTF portal data."""

    def __init__(
        self,
        hass: HomeAssistant,
        customer: str,
        pin: str,
    ) -> None:
        """Initialize."""
        self.client = HTFHistoricClient(
            customer,
            pin,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="HTF Historical Portal Data",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(
        self,
    ) -> dict[str, Any]:
        """Download historical portal data."""
        try:
            return await self.hass.async_add_executor_job(
                self.client.fetch
            )

        except UpdateFailed:
            raise

        except requests.RequestException as err:
            raise UpdateFailed(
                f"HTF historical portal network error: {err}"
            ) from err

        except Exception as err:
            _LOGGER.exception(
                "HTF historical portal update failed"
            )

            raise UpdateFailed(
                f"Unable to download HTF historical data: {err}"
            ) from err


class HTFHistoricBase(
    CoordinatorEntity[HTFHistoricCoordinator],
    SensorEntity,
):
    """Base historical HTF sensor."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: HTFHistoricCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

    @property
    def device_info(self):
        """Return device information."""
        return {
            "identifiers": {
                (
                    DOMAIN,
                    "historic",
                )
            },
            "name": "HTF Historical Data",
            "manufacturer": "Høje Taastrup Fjernvarme",
            "configuration_url": BASE,
        }


class HTFHistoricConsumption(
    HTFHistoricBase
):
    """Complete raw consumption portal data."""

    _attr_name = (
        "HTF Historic Consumption"
    )

    _attr_icon = "mdi:table"

    def __init__(
        self,
        coordinator: HTFHistoricCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_historic_consumption"
        )

    @property
    def native_value(self) -> str:
        """Return sensor state."""
        return "Available"

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return raw HTF portal JSON."""
        data = self.coordinator.data or {}

        return {
            "source": (
                "HTF /forbrug/"
            ),
            "portal_json": data.get(
                "consumption",
                {},
            ),
        }


class HTFHistoricBills(
    HTFHistoricBase
):
    """Complete raw billing tables."""

    _attr_name = (
        "HTF Historic Bills"
    )

    _attr_icon = "mdi:receipt-text"

    def __init__(
        self,
        coordinator: HTFHistoricCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_historic_bills"
        )

    @property
    def native_value(self) -> str:
        """Return sensor state."""
        return "Available"

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return complete HTF billing tables."""
        data = self.coordinator.data or {}

        return {
            "source": (
                "HTF /kundeoplysninger/kontoudtog/"
            ),
            "portal_tables": data.get(
                "bills",
                [],
            ),
        }


class HTFHistoricReturnTemperature(
    HTFHistoricBase
):
    """Complete raw return-temperature portal data."""

    _attr_name = (
        "HTF Historic Return Temperature"
    )

    _attr_icon = "mdi:thermometer"

    def __init__(
        self,
        coordinator: HTFHistoricCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_historic_return_temperature"
        )

    @property
    def native_value(self) -> str:
        """Return sensor state."""
        return "Available"

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return raw HTF portal JSON."""
        data = self.coordinator.data or {}

        return {
            "source": (
                "HTF /returtemperatur/"
            ),
            "portal_json": data.get(
                "return_temperature",
                {},
            ),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up HTF historical sensors."""
    coordinator = HTFHistoricCoordinator(
        hass,
        entry.data["customer"],
        entry.data["pin"],
    )

    async_add_entities(
        [
            HTFHistoricConsumption(
                coordinator
            ),
            HTFHistoricBills(
                coordinator
            ),
            HTFHistoricReturnTemperature(
                coordinator
            ),
        ]
    )

    # Fetch immediately once when the integration starts.
    # Subsequent automatic refreshes happen every 15 days.
    hass.async_create_task(
        coordinator.async_config_entry_first_refresh()
    )