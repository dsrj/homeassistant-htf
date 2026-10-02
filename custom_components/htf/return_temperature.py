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


class ReturnTemperatureClient:
    """Client for HTF return-temperature data."""

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
                    "(Home Assistant HTF integration)"
                )
            }
        )

        self.dashboard_html = ""

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
        """Select the active consumption point."""
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
    def _get_json_from_page(
        html: str,
    ) -> dict[str, Any]:
        """Extract consumption-data-json from HTF page."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        element = soup.select_one(
            "#consumption-data-json"
        )

        if element is None:
            raise UpdateFailed(
                "HTF return-temperature JSON not found"
            )

        raw = element.get_text(
            strip=True
        )

        if not raw:
            raise UpdateFailed(
                "HTF return-temperature JSON is empty"
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise UpdateFailed(
                "HTF return-temperature JSON is invalid"
            ) from err

        if not isinstance(data, dict):
            raise UpdateFailed(
                "HTF return-temperature data has unexpected format"
            )

        return data

    @staticmethod
    def _get_meter(
        meters: Any,
        counter_number: int,
        meter_type: str,
    ) -> dict[str, Any] | None:
        """Find a meter by counter number and meter type."""
        if not isinstance(meters, list):
            return None

        for meter in meters:
            if not isinstance(meter, dict):
                continue

            info = meter.get("meterInfo")

            if not isinstance(info, dict):
                continue

            try:
                counter = int(
                    info.get("counterNumber")
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            actual_type = str(
                info.get("meterType2", "")
            ).strip().upper()

            if (
                counter == counter_number
                and actual_type == meter_type.upper()
            ):
                return meter

        return None

    @staticmethod
    def _month_values(
        meter: dict[str, Any],
    ) -> dict[str, float]:
        """Convert meter monthData to YYYY-MM values."""
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
                months,
                list,
            ) or not isinstance(
                values,
                list,
            ):
                continue

            try:
                year_number = int(year)
            except (
                TypeError,
                ValueError,
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

                if 1 <= month_number <= 12:
                    result[
                        f"{year_number:04d}-{month_number:02d}"
                    ] = numeric_value

        return result

    @staticmethod
    def _calculate_return_temperature(
        data: dict[str, Any],
    ) -> tuple[float, str]:
        """Calculate the HTF return-temperature value."""
        meters = data.get(
            "returnTempMeters"
        )

        if not isinstance(
            meters,
            list,
        ):
            raise UpdateFailed(
                "HTF return-temperature meters not found"
            )

        return_meter = ReturnTemperatureClient._get_meter(
            meters,
            5,
            "FV-RT",
        )

        volume_meter = ReturnTemperatureClient._get_meter(
            meters,
            2,
            "FV-M3",
        )

        if return_meter is None:
            raise UpdateFailed(
                "HTF FV-RT meter not found"
            )

        if volume_meter is None:
            raise UpdateFailed(
                "HTF FV-M3 meter not found"
            )

        return_values = (
            ReturnTemperatureClient._month_values(
                return_meter
            )
        )

        volume_values = (
            ReturnTemperatureClient._month_values(
                volume_meter
            )
        )

        common_dates = sorted(
            set(return_values)
            & set(volume_values)
        )

        if not common_dates:
            raise UpdateFailed(
                "HTF FV-RT and FV-M3 have no "
                "common monthly values"
            )

        for date_key in reversed(common_dates):
            rt_value = return_values[
                date_key
            ]

            m3_value = volume_values[
                date_key
            ]

            if m3_value <= 0:
                continue

            ratio = (
                rt_value / m3_value
            )

            _LOGGER.debug(
                "HTF return-temperature raw calculation: "
                "date=%s FV-RT=%.6f FV-M3=%.6f ratio=%.6f",
                date_key,
                rt_value,
                m3_value,
                ratio,
            )

            return (
                round(ratio, 2),
                date_key,
            )

        raise UpdateFailed(
            "HTF return-temperature calculation "
            "has no usable M3 value"
        )

    def fetch(self) -> dict[str, Any]:
        """Fetch HTF return temperature."""
        try:
            self._login()
            self._select_consumption_point()

            response = self.session.get(
                f"{BASE}/returtemperatur/",
                timeout=30,
            )
            response.raise_for_status()

            data = self._get_json_from_page(
                response.text
            )

            temperature, date_key = (
                self._calculate_return_temperature(
                    data
                )
            )

            target_data = data.get(
                "goodReturnTemperatureData",
                {},
            )

            year = date_key[:4]
            target = None

            if isinstance(
                target_data,
                dict,
            ):
                try:
                    target = float(
                        target_data.get(year)
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    target = None

            result: dict[str, Any] = {
                "temperature": temperature,
                "date": date_key,
            }

            if target is not None:
                result["target"] = target

            _LOGGER.info(
                "HTF return temperature: "
                "%.2f for %s",
                temperature,
                date_key,
            )

            if target is not None:
                _LOGGER.info(
                    "HTF return temperature target: "
                    "%.1f for %s",
                    target,
                    year,
                )

            return result

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