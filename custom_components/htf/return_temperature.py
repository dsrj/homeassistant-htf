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

COUNTER_M3 = 2
COUNTER_FLOW_TEMPERATURE = 4
COUNTER_RETURN_TEMPERATURE = 5


class ReturnTemperatureClient:
    """Client for HTF return temperature."""

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
        """Select the HTF consumption point."""
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
    def _get_return_temperature_json(
        html: str,
    ) -> dict[str, Any]:
        """Extract JSON from the HTF return-temperature page."""
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
    def _find_meter(
        meters: list[dict[str, Any]],
        counter_number: int,
        meter_type2: str,
    ) -> dict[str, Any] | None:
        """Find a return-temperature meter."""
        for meter in meters:
            info = meter.get("meterInfo")

            if not isinstance(info, dict):
                continue

            actual_counter = info.get(
                "counterNumber"
            )

            actual_type = str(
                info.get(
                    "meterType2",
                    "",
                )
            ).upper()

            try:
                counter_matches = (
                    int(actual_counter)
                    == counter_number
                )
            except (
                TypeError,
                ValueError,
            ):
                counter_matches = False

            if (
                counter_matches
                and actual_type == meter_type2.upper()
            ):
                return meter

        return None

    @staticmethod
    def _build_month_values(
        meter: dict[str, Any],
    ) -> dict[str, float]:
        """Convert HTF monthData into YYYY-MM values."""
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

            if year is None:
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

                if not 1 <= month_number <= 12:
                    continue

                result[
                    f"{year_number:04d}-"
                    f"{month_number:02d}"
                ] = numeric_value

        return result

    @staticmethod
    def _calculate_temperature(
        data: dict[str, Any],
    ) -> tuple[float, str]:
        """Calculate return temperature from HTF return-temperature data."""
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

        return_meter = ReturnTemperatureClient._find_meter(
            meters,
            COUNTER_RETURN_TEMPERATURE,
            "FV-RT",
        )

        m3_meter = ReturnTemperatureClient._find_meter(
            meters,
            COUNTER_M3,
            "FV-M3",
        )

        if return_meter is None:
            raise UpdateFailed(
                "HTF return-temperature meter "
                "(FV-RT / counterNumber 5) not found"
            )

        if m3_meter is None:
            raise UpdateFailed(
                "HTF M3 meter "
                "(FV-M3 / counterNumber 2) not found"
            )

        return_values = (
            ReturnTemperatureClient._build_month_values(
                return_meter
            )
        )

        m3_values = (
            ReturnTemperatureClient._build_month_values(
                m3_meter
            )
        )

        common_dates = sorted(
            set(return_values)
            & set(m3_values)
        )

        if not common_dates:
            raise UpdateFailed(
                "HTF return-temperature and M3 "
                "meters have no common dates"
            )

        latest_date = common_dates[-1]

        return_value = return_values[
            latest_date
        ]

        m3_value = m3_values[
            latest_date
        ]

        if m3_value == 0:
            raise UpdateFailed(
                "HTF M3 value is zero"
            )

        temperature = (
            return_value / m3_value
        )

        return (
            round(
                temperature,
                2,
            ),
            latest_date,
        )

    @staticmethod
    def _get_good_return_temperature_data(
        data: dict[str, Any],
    ) -> dict[str, float]:
        """Extract yearly HTF return-temperature targets."""
        values = data.get(
            "goodReturnTemperatureData"
        )

        if not isinstance(
            values,
            dict,
        ):
            return {}

        result: dict[str, float] = {}

        for year, value in values.items():
            try:
                result[str(year)] = float(value)
            except (
                TypeError,
                ValueError,
            ):
                continue

        return result

    def fetch(self) -> dict[str, Any]:
        """Fetch return temperature from /returtemperatur/."""
        try:
            self._login()
            self._select_consumption_point()

            response = self.session.get(
                f"{BASE}/returtemperatur/",
                timeout=30,
            )
            response.raise_for_status()

            data = self._get_return_temperature_json(
                response.text
            )

            temperature, date_key = (
                self._calculate_temperature(
                    data
                )
            )

            targets = (
                self._get_good_return_temperature_data(
                    data
                )
            )

            target = targets.get(
                date_key[:4]
            )

            _LOGGER.info(
                "HTF return temperature: %.2f °C for %s",
                temperature,
                date_key,
            )

            if target is not None:
                _LOGGER.info(
                    "HTF good return temperature target: "
                    "%.1f °C for %s",
                    target,
                    date_key[:4],
                )

            return {
                "temperature": temperature,
                "date": date_key,
                "target": target,
                "good_return_temperature_data": targets,
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