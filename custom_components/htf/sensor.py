"""HTF Selvbetjening sensor platform."""

from __future__ import annotations

import json
import logging
import re
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

    def _find_value(
        self,
        text: str,
        names: list[str],
    ) -> str | None:
        """Find a value in HTML/JavaScript."""

        for name in names:
            patterns = [
                rf'"{re.escape(name)}"\s*:\s*"([^"]+)"',
                rf"'{re.escape(name)}'\s*:\s*'([^']+)'",
                rf'"{re.escape(name)}"\s*:\s*(\d+)',
                rf"'{re.escape(name)}'\s*:\s*(\d+)",
                rf"\b{re.escape(name)}\b\s*:\s*['\"]([^'\"]+)['\"]",
                rf"\b{re.escape(name)}\b\s*:\s*(\d+)",
            ]

            for pattern in patterns:
                match = re.search(
                    pattern,
                    text,
                    re.IGNORECASE,
                )

                if match:
                    return match.group(1)

        return None

    def _find_consumption_point(
        self,
        dashboard_html: str,
    ) -> dict[str, str]:
        """Extract HTF consumption selection IDs."""

        result: dict[str, str] = {}

        consumption_point_id = self._find_value(
            dashboard_html,
            [
                "consumptionPointId",
                "ConsumptionPointId",
                "consumptionpointid",
            ],
        )

        consumer_id = self._find_value(
            dashboard_html,
            [
                "consumerId",
                "ConsumerId",
                "consumerid",
            ],
        )

        debtor_id = self._find_value(
            dashboard_html,
            [
                "debitorId",
                "DebtorId",
                "debtorId",
                "debitorid",
            ],
        )

        customer_id = self._find_value(
            dashboard_html,
            [
                "customerId",
                "CustomerId",
                "customerid",
            ],
        )

        if consumption_point_id:
            result["ConsumptionPointId"] = consumption_point_id

        if consumer_id:
            result["ConsumerId"] = consumer_id

        if debtor_id:
            result["DebtorId"] = debtor_id

        if customer_id:
            result["CustomerId"] = customer_id

        return result

    def _select_consumption_point(
        self,
        dashboard_html: str,
    ) -> None:
        """Select the user's consumption point."""

        ids = self._find_consumption_point(
            dashboard_html
        )

        _LOGGER.debug(
            "HTF consumption selection IDs found: %s",
            {
                key: value
                for key, value in ids.items()
                if key != "CustomerId"
            },
        )

        required = [
            "ConsumptionPointId",
            "ConsumerId",
            "DebtorId",
            "CustomerId",
        ]

        missing = [
            key
            for key in required
            if not ids.get(key)
        ]

        if missing:
            raise RuntimeError(
                "HTF consumption point IDs could not "
                "be found on the dashboard. Missing: "
                + ", ".join(missing)
            )

        response = self.session.post(
            f"{BASE}/umbraco/surface/customer2/"
            "SetSelectedConsumption",
            data={
                "ConsumptionPointId": ids[
                    "ConsumptionPointId"
                ],
                "ConsumerId": ids[
                    "ConsumerId"
                ],
                "DebtorId": ids[
                    "DebtorId"
                ],
                "CustomerId": ids[
                    "CustomerId"
                ],
            },
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

        _LOGGER.debug(
            "HTF SetSelectedConsumption completed: "
            "status=%s",
            response.status_code,
        )

        if response.text:
            _LOGGER.debug(
                "HTF SetSelectedConsumption response: %s",
                response.text[:500],
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
        # 2. Submit login.
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
        # HTF normally establishes/selects the consumption point
        # through the dashboard before /forbrug/ is opened.
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
            "HTF dashboard loaded: status=%s url=%s",
            dashboard.status_code,
            dashboard.url,
        )

        # ---------------------------------------------------------
        # 4. Select the consumption point.
        # ---------------------------------------------------------

        self._select_consumption_point(
            dashboard.text
        )

        # ---------------------------------------------------------
        # 5. Fetch consumption page.
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
            "HTF consumption page loaded: status=%s url=%s",
            page.status_code,
            page.url,
        )

        # ---------------------------------------------------------
        # 6. Extract embedded consumption JSON.
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

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise RuntimeError(
                "HTF returned invalid consumption JSON."
            ) from err

        # ---------------------------------------------------------
        # 7. Basic validation.
        # ---------------------------------------------------------

        if not isinstance(data, dict):
            raise RuntimeError(
                "HTF returned an unexpected consumption format."
            )

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

    # First refresh.
    #
    # Do not fail integration setup if HTF is temporarily
    # unavailable. The entities will remain available and
    # the coordinator will retry during the next update.
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
        """Return monthly data as attributes."""

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
        """Return yearly data as attributes."""

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