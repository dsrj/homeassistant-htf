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
        """Extract HTF consumption JSON."""
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
    def _collect_all_meters(
        data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Collect meters from all known HTF JSON locations."""
        meters: list[dict[str, Any]] = []

        for key in (
            "meters",
            "returnTempMeters",
        ):
            value = data.get(key)

            if not isinstance(
                value,
                list,
            ):
                continue

            for meter in value:
                if not isinstance(
                    meter,
                    dict,
                ):
                    continue

                if meter not in meters:
                    meters.append(meter)

        return meters

    @staticmethod
    def _meter_matches(
        meter: dict[str, Any],
        counter_number: int,
        meter_type2: str | None = None,
    ) -> bool:
        """Check whether a meter matches the requested type."""
        info = meter.get(
            "meterInfo"
        )

        if not isinstance(
            info,
            dict,
        ):
            return False

        if meter_type2:
            actual_type = str(
                info.get(
                    "meterType2",
                    "",
                )
            ).upper()

            if actual_type == meter_type2.upper():
                return True

        value = info.get(
            "counterNumber"
        )

        try:
            return int(value) == counter_number
        except (
            TypeError,
            ValueError,
        ):
            return False

    @staticmethod
    def _find_meter(
        meters: list[dict[str, Any]],
        counter_number: int,
        meter_type2: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a meter."""
        for meter in meters:
            if ReturnTemperatureClient._meter_matches(
                meter,
                counter_number,
                meter_type2,
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
        """Calculate return temperature exactly like HTF JavaScript."""
        meters = (
            ReturnTemperatureClient._collect_all_meters(
                data
            )
        )

        if not meters:
            raise UpdateFailed(
                "HTF contains no meter data"
            )

        return_meter = (
            ReturnTemperatureClient._find_meter(
                meters,
                COUNTER_RETURN_TEMPERATURE,
                "FV-RT",
            )
        )

        volume_meter = (
            ReturnTemperatureClient._find_meter(
                meters,
                COUNTER_M3,
                "FV-M3",
            )
        )

        if return_meter is None:
            raise UpdateFailed(
                "HTF return-temperature meter "
                "(FV-RT / counterNumber 5) not found"
            )

        if volume_meter is None:
            raise UpdateFailed(
                "HTF M3 meter "
                "(FV-M3 / counterNumber 2) not found"
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

        # KEEP THIS CALCULATION.
        #
        # This is the calculation from the earlier
        # working version that produced the correct
        # HTF value, including 30.00 °C.
        #
        # HTF CoolingController.js:
        #
        # completeLineChartValues =
        #     dataRT.map((rt, i) => rt / dataM3[i])
        #
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

            return (
                round(
                    temperature,
                    2,
                ),
                date_key,
            )

        raise UpdateFailed(
            "HTF return-temperature calculation "
            "failed because M3 values are zero"
        )

    @staticmethod
    def _get_good_return_temperature_data(
        html: str,
    ) -> dict[str, float]:
        """Extract HTF yearly good-return-temperature targets."""
        data = ReturnTemperatureClient._get_consumption_json(
            html
        )

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
        """Fetch HTF return temperature."""
        try:
            self._login()
            self._select_consumption_point()

            # IMPORTANT:
            # Return temperature continues to come from
            # /forbrug/ using the working FV-RT / FV-M3
            # calculation above.
            response = self.session.get(
                f"{BASE}/forbrug/",
                timeout=30,
            )
            response.raise_for_status()

            data = self._get_consumption_json(
                response.text
            )

            temperature, date_key = (
                self._calculate_temperature(
                    data
                )
            )

            # Fetch HTF's own yearly target separately.
            # This does not change the return-temperature
            # calculation.
            target_response = self.session.get(
                f"{BASE}/returtemperatur/",
                timeout=30,
            )
            target_response.raise_for_status()

            targets = (
                self._get_good_return_temperature_data(
                    target_response.text
                )
            )

            target = targets.get(
                date_key[:4]
            )

            _LOGGER.info(
                "HTF return temperature: "
                "%.2f °C for %s",
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
    DataUpdateCoordinator[
        dict[str, Any]
    ]
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