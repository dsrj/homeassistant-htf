"""HTF return temperature support."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=24)

# HTF's own CoolingController.js identifies these meters:
# FV-M3  -> counterNumber 2
# FV-RT  -> counterNumber 5
COUNTER_M3 = 2
COUNTER_RETURN_TEMPERATURE = 5


class ReturnTemperatureClient:
    """Client for HTF return temperature."""

    def __init__(
        self,
        customer: str,
        pin: str,
    ) -> None:
        """Initialize the client."""
        self.customer = customer
        self.pin = pin

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Home Assistant HTF integration)"
                )
            }
        )

    def _login(self) -> None:
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
            "subheader-consumptionpoint-dropdown"
            not in response.text
            and "/login" in response.url.lower()
        ):
            raise UpdateFailed(
                "HTF login was not accepted"
            )

        self.dashboard_html = response.text

    def _select_consumption_point(self) -> None:
        """Select the same consumption point as the main integration."""
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
            option = dropdown.find("option")

        if option is None:
            raise UpdateFailed(
                "HTF consumption point not found"
            )

        value = option.get("value")

        if not value:
            raise UpdateFailed(
                "HTF consumption point value empty"
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

    @staticmethod
    def _get_consumption_json(
        html: str,
    ) -> dict[str, Any]:
        """Extract HTF consumption JSON from the page."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        element = soup.select_one(
            "#consumption-data-json"
        )

        if element is None:
            raise UpdateFailed(
                "HTF consumption data JSON not found"
            )

        raw = element.get_text(
            strip=True
        )

        if not raw:
            raise UpdateFailed(
                "HTF consumption data JSON is empty"
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise UpdateFailed(
                "HTF consumption data JSON is invalid"
            ) from err

        if not isinstance(data, dict):
            raise UpdateFailed(
                "HTF consumption data has unexpected format"
            )

        return data

    @staticmethod
    def _find_meter(
        meters: list[dict[str, Any]],
        counter_number: int,
    ) -> dict[str, Any] | None:
        """Find a meter by HTF counter number."""
        for meter in meters:
            meter_info = meter.get(
                "meterInfo"
            )

            if not isinstance(
                meter_info,
                dict,
            ):
                continue

            value = meter_info.get(
                "counterNumber"
            )

            try:
                if int(value) == counter_number:
                    return meter
            except (
                TypeError,
                ValueError,
            ):
                continue

        return None

    @staticmethod
    def _build_month_values(
        meter: dict[str, Any],
    ) -> dict[str, float]:
        """Convert HTF monthData into YYYY-MM -> value."""
        result: dict[str, float] = {}

        month_data = meter.get(
            "monthData"
        )

        if not isinstance(
            month_data,
            dict,
        ):
            return result

        for year_data in month_data.values():
            if not isinstance(
                year_data,
                dict,
            ):
                continue

            year = year_data.get(
                "yearNumber"
            )

            months = year_data.get(
                "monthNumbers"
            )

            values = year_data.get(
                "values"
            )

            if not isinstance(
                year,
                int,
            ):
                continue

            if not isinstance(
                months,
                list,
            ):
                continue

            if not isinstance(
                values,
                list,
            ):
                continue

            for month, value in zip(
                months,
                values,
            ):
                try:
                    month_number = int(month)
                    numeric_value = float(value)
                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                if not 1 <= month_number <= 12:
                    continue

                key = (
                    f"{year:04d}-"
                    f"{month_number:02d}"
                )

                result[key] = numeric_value

        return result

    @staticmethod
    def _calculate_latest_return_temperature(
        data: dict[str, Any],
    ) -> tuple[float, str]:
        """Calculate the latest HTF return temperature."""
        meters = data.get(
            "meters"
        )

        # Some HTF pages use returnTempMeters
        # before the JavaScript normalizes them to meters.
        if not isinstance(
            meters,
            list,
        ):
            meters = data.get(
                "returnTempMeters"
            )

        if not isinstance(
            meters,
            list,
        ):
            raise UpdateFailed(
                "HTF meter data not found"
            )

        normalized_meters = [
            meter
            for meter in meters
            if isinstance(
                meter,
                dict,
            )
        ]

        return_meter = (
            ReturnTemperatureClient._find_meter(
                normalized_meters,
                COUNTER_RETURN_TEMPERATURE,
            )
        )

        volume_meter = (
            ReturnTemperatureClient._find_meter(
                normalized_meters,
                COUNTER_M3,
            )
        )

        if return_meter is None:
            raise UpdateFailed(
                "HTF return-temperature meter "
                "(counterNumber 5) not found"
            )

        if volume_meter is None:
            raise UpdateFailed(
                "HTF M3 meter "
                "(counterNumber 2) not found"
            )

        return_values = (
            ReturnTemperatureClient._build_month_values(
                return_meter
            )
        )

        volume_values = (
            ReturnTemperatureClient._build_month_values(
                volume_meter
            )
        )

        if not return_values:
            raise UpdateFailed(
                "HTF return-temperature meter "
                "contains no monthly values"
            )

        if not volume_values:
            raise UpdateFailed(
                "HTF M3 meter contains no monthly values"
            )

        common_dates = sorted(
            set(return_values)
            & set(volume_values)
        )

        if not common_dates:
            raise UpdateFailed(
                "HTF return-temperature and M3 "
                "meters have no common dates"
            )

        # HTF's own JavaScript uses:
        #
        # dataRT / dataM3
        #
        # for the return-temperature chart.
        for date_key in reversed(
            common_dates
        ):
            return_value = return_values[
                date_key
            ]

            volume_value = volume_values[
                date_key
            ]

            if volume_value == 0:
                continue

            temperature = (
                return_value / volume_value
            )

            if not (
                -20 <= temperature <= 100
            ):
                _LOGGER.warning(
                    "HTF calculated an unusual "
                    "return temperature: %.3f °C "
                    "for %s",
                    temperature,
                    date_key,
                )

            return (
                round(
                    temperature,
                    2,
                ),
                date_key,
            )

        raise UpdateFailed(
            "HTF return-temperature calculation "
            "failed because the latest M3 value is zero"
        )

    def fetch(self) -> dict[str, Any]:
        """Fetch and calculate HTF return temperature."""
        try:
            self._login()
            self._select_consumption_point()

            _LOGGER.debug(
                "HTF return temperature: "
                "requesting /forbrug/"
            )

            response = self.session.get(
                f"{BASE}/forbrug/",
                timeout=30,
            )
            response.raise_for_status()

            data = self._get_consumption_json(
                response.text
            )

            temperature, date_key = (
                self._calculate_latest_return_temperature(
                    data
                )
            )

            _LOGGER.info(
                "HTF return temperature: "
                "%.2f °C for %s",
                temperature,
                date_key,
            )

            return {
                "temperature": temperature,
                "date": date_key,
            }

        except requests.RequestException as err:
            raise UpdateFailed(
                "HTF return temperature network error: "
                f"{err}"
            ) from err


class ReturnTemperatureCoordinator(
    DataUpdateCoordinator[dict[str, Any]]
):
    """Coordinate HTF return-temperature updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        customer: str,
        pin: str,
    ) -> None:
        """Initialize."""
        self.client = ReturnTemperatureClient(
            customer,
            pin,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="HTF Return Temperature",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(
        self,
    ) -> dict[str, Any]:
        """Fetch return temperature."""
        try:
            return await self.hass.async_add_executor_job(
                self.client.fetch
            )

        except UpdateFailed:
            raise

        except Exception as err:
            _LOGGER.exception(
                "HTF return temperature: unexpected error"
            )

            raise UpdateFailed(
                "Unable to fetch HTF return temperature: "
                f"{err}"
            ) from err