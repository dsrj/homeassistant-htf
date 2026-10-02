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
    """Client for HTF Selvbetjening."""

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

    def _get_consumption_point(
        self,
        dashboard_html: str,
    ) -> dict[str, str]:
        """Extract consumption point IDs from the dashboard."""

        soup = BeautifulSoup(
            dashboard_html,
            "html.parser",
        )

        dropdown = soup.select_one(
            ".subheader-consumptionpoint-dropdown"
        )

        if not dropdown:
            raise RuntimeError(
                "HTF consumption point dropdown was not found."
            )

        option = dropdown.select_one(
            "option[selected]"
        )

        if not option:
            option = dropdown.select_one("option")

        if not option:
            raise RuntimeError(
                "HTF consumption point option was not found."
            )

        value = option.get("value")

        if not value:
            raise RuntimeError(
                "HTF consumption point value is empty."
            )

        parts = value.split(";")

        if len(parts) != 4:
            raise RuntimeError(
                "Unexpected HTF consumption point format."
            )

        consumption_point_id = parts[0]
        consumer_id = parts[1]
        debtor_id = parts[2]
        customer_id = parts[3]

        # Do NOT log these values.
        # They are account-specific information.
        _LOGGER.debug(
            "HTF consumption point information "
            "was extracted from the dashboard."
        )

        return {
            "ConsumptionPointId": consumption_point_id,
            "ConsumerId": consumer_id,
            "DebtorId": debtor_id,
            "CustomerId": customer_id,
        }

    def _select_consumption_point(
        self,
        dashboard_html: str,
    ) -> None:
        """Select the consumption point for this session."""

        consumption_data = self._get_consumption_point(
            dashboard_html
        )

        response = self.session.post(
            f"{BASE}/umbraco/surface/customer2/"
            "SetSelectedConsumption",
            data=consumption_data,
            headers={
                "Referer": f"{BASE}/dashboard/",
                "Origin": BASE,
                "Content-Type": (
                    "application/x-www-form-urlencoded; "
                    "charset=UTF-8"
                ),
            },
            timeout=30,
        )

        response.raise_for_status()

        result = response.text.strip()

        _LOGGER.debug(
            "HTF consumption point selection completed: "
            "status=%s",
            response.status_code,
        )

        if result != "OK":
            raise RuntimeError(
                "HTF rejected the consumption point selection."
            )

    def fetch(self) -> dict[str, Any]:
        """Log in and retrieve HTF consumption data."""

        # ---------------------------------------------------------
        # 1. Open login page.
        # ---------------------------------------------------------

        login_page = self.session.get(
            f"{BASE}/login",
            headers={
                "Referer": BASE,
            },
            timeout=30,
        )

        login_page.raise_for_status()

        _LOGGER.debug(
            "HTF login page loaded: status=%s",
            login_page.status_code,
        )

        # ---------------------------------------------------------
        # 2. Log in.
        # ---------------------------------------------------------

        login = self.session.post(
            f"{BASE}/umbraco/surface/login2/PostLogin",
            data={
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
            headers={
                "Referer": f"{BASE}/login",
                "Origin": BASE,
                "Content-Type": (
                    "application/x-www-form-urlencoded; "
                    "charset=UTF-8"
                ),
            },
            timeout=30,
            allow_redirects=True,
        )

        login.raise_for_status()

        _LOGGER.debug(
            "HTF login completed: status=%s url=%s",
            login.status_code,
            login.url,
        )

        # ---------------------------------------------------------
        # 3. Open dashboard.
        #
        # The dashboard contains the selected installation:
        #
        # <option value="...;...;...;..." selected>
        #
        # We extract those values dynamically.
        # ---------------------------------------------------------

        dashboard = self.session.get(
            f"{BASE}/dashboard/",
            headers={
                "Referer": f"{BASE}/login",
            },
            timeout=30,
        )

        dashboard.raise_for_status()

        _LOGGER.debug(
            "HTF dashboard loaded: status=%s",
            dashboard.status_code,
        )

        # ---------------------------------------------------------
        # 4. Select consumption point.
        # ---------------------------------------------------------

        self._select_consumption_point(
            dashboard.text
        )

        # ---------------------------------------------------------
        # 5. Open consumption page.
        # ---------------------------------------------------------

        page = self.session.get(
            f"{BASE}/forbrug/",
            headers={
                "Referer": f"{BASE}/dashboard/",
            },
            timeout=30,
        )

        page.raise_for_status()

        _LOGGER.debug(
            "HTF consumption page loaded: status=%s",
            page.status_code,
        )

        # ---------------------------------------------------------
        # 6. Find embedded consumption JSON.
        # ---------------------------------------------------------

        soup = BeautifulSoup(
            page.text,
            "html.parser",
        )

        node = soup.select_one(
            "#consumption-data-json"
        )

        if not node:
            raise RuntimeError(
                "HTF consumption data was not found "
                "after selecting the consumption point."
            )

        raw = node.get_text(
            strip=True
        )

        if not raw:
            raise RuntimeError(
                "HTF returned empty consumption data."
            )

        # ---------------------------------------------------------
        # 7. Parse JSON.
        # ---------------------------------------------------------

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise RuntimeError(
                "HTF returned invalid consumption JSON."
            ) from err

        if not isinstance(data, dict):
            raise RuntimeError(
                "HTF returned an unexpected consumption format."
            )

        # ---------------------------------------------------------
        # 8. Validate meter data.
        # ---------------------------------------------------------

        meters = data.get("meters")

        if not meters:
            raise RuntimeError(
                "HTF returned no heating meters."
            )

        _LOGGER.debug(
            "HTF consumption data received successfully."
        )

        return data


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
        """Fetch data from HTF."""

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

    # Initial update.
    #
    # We deliberately do not make integration setup fail
    # if HTF is temporarily unavailable.
    try:
        await coordinator.async_refresh()
    except Exception as err:
        _LOGGER.error(
            "Unexpected error fetching HTF consumption data: %s",
            err,
        )

    async_add_entities(
        [
            HTFCurrentMonth(coordinator),
            HTFCurrentYear(coordinator),
        ]
    )


class HTFBase(CoordinatorEntity):
    """Base class for HTF sensors."""

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

        return meters[0] if meters else {}


class HTFCurrentMonth(
    HTFBase,
    SensorEntity,
):
    """Current month heating consumption."""

    _attr_name = "HTF Heating Current Month"

    _attr_unique_id = (
        "htf_heating_current_month"
    )

    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )

    _attr_device_class = "energy"
    _attr_state_class = "total"
    _attr_icon = "mdi:fire"

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

        try:
            return float(values[index])
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return monthly data."""

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
    """Current year heating consumption."""

    _attr_name = "HTF Heating Current Year"

    _attr_unique_id = (
        "htf_heating_current_year"
    )

    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )

    _attr_device_class = "energy"
    _attr_state_class = "total"
    _attr_icon = "mdi:fire"

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

        try:
            return float(values[index])
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return yearly data."""

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