"""HTF return-temperature client and calculation helpers."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.helpers.update_coordinator import UpdateFailed

BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=24)

_LOGGER = logging.getLogger(__name__)

# HTF's FV-M3 values in the return-temperature payload are represented
# in hundredths of a cubic metre. FV-RT is m³×°C.
M3_SCALE = 100.0


def _log_json(label: str, value: Any) -> None:
    """Write diagnostic JSON without credentials/cookies."""
    try:
        output = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except (TypeError, ValueError):
        output = repr(value)

    _LOGGER.debug(
        "HTF return-temperature diagnostic %s:\n%s",
        label,
        output,
    )


class ReturnTemperatureClient:
    """Synchronous HTF return-temperature client."""

    def __init__(
        self,
        customer: str,
        pin: str,
    ) -> None:
        """Initialize the client."""
        self.customer = customer
        self.pin = pin
        self.session = requests.Session()
        self.dashboard_html = ""

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Home Assistant HTF integration)"
                )
            }
        )

    def _login(self) -> None:
        """Log in and load the dashboard."""
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
                "HTF login was not accepted"
            )

        self.dashboard_html = response.text

    def _select_consumption_point(self) -> None:
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
        """Extract HTF's embedded return-temperature JSON."""
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
        """Find a return-temperature meter."""
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
        """Convert HTF monthData to YYYY-MM values."""
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

            try:
                year = int(
                    year_data.get(
                        "yearNumber"
                    )
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            months = year_data.get(
                "monthNumbers"
            )

            values = year_data.get(
                "values"
            )

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

                if 1 <= month_number <= 12:
                    result[
                        f"{year:04d}-{month_number:02d}"
                    ] = numeric_value

        return result

    @classmethod
    def _calculate(
        cls,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Calculate the HTF return-temperature series."""
        meters = data.get(
            "returnTempMeters"
        )

        if not isinstance(
            meters,
            list,
        ):
            meters = data.get(
                "meters"
            )

        if not isinstance(
            meters,
            list,
        ):
            raise UpdateFailed(
                "HTF return-temperature meters not found"
            )

        m3_meter = cls._get_meter(
            meters,
            2,
            "FV-M3",
        )

        ft_meter = cls._get_meter(
            meters,
            4,
            "FV-FT",
        )

        rt_meter = cls._get_meter(
            meters,
            5,
            "FV-RT",
        )

        if m3_meter is None:
            raise UpdateFailed(
                "HTF FV-M3 meter not found"
            )

        if ft_meter is None:
            raise UpdateFailed(
                "HTF FV-FT meter not found"
            )

        if rt_meter is None:
            raise UpdateFailed(
                "HTF FV-RT meter not found"
            )

        m3_values = cls._month_values(
            m3_meter
        )

        ft_values = cls._month_values(
            ft_meter
        )

        rt_values = cls._month_values(
            rt_meter
        )

        common_dates = sorted(
            set(m3_values)
            & set(ft_values)
            & set(rt_values)
        )

        if not common_dates:
            raise UpdateFailed(
                "HTF return-temperature meters "
                "have no common dates"
            )

        series: dict[str, float] = {}

        for date_key in common_dates:
            raw_m3 = m3_values[
                date_key
            ]

            raw_ft = ft_values[
                date_key
            ]

            raw_rt = rt_values[
                date_key
            ]

            if raw_m3 <= 0:
                continue

            scaled_m3 = (
                raw_m3 / M3_SCALE
            )

            temperature = (
                raw_rt / scaled_m3
            )

            series[date_key] = round(
                temperature,
                2,
            )

            _LOGGER.debug(
                "HTF return-temperature calculation: "
                "date=%s raw_FV_RT=%s "
                "raw_FV_FT=%s "
                "raw_FV_M3=%s "
                "scaled_FV_M3=%s "
                "temperature=%.2f",
                date_key,
                raw_rt,
                raw_ft,
                raw_m3,
                scaled_m3,
                temperature,
            )

        if not series:
            raise UpdateFailed(
                "HTF return-temperature calculation "
                "has no usable values"
            )

        latest_date = sorted(
            series
        )[-1]

        latest_temperature = series[
            latest_date
        ]

        targets = data.get(
            "goodReturnTemperatureData",
            {},
        )

        target = None

        if isinstance(
            targets,
            dict,
        ):
            year = latest_date[:4]

            try:
                target = float(
                    targets.get(year)
                )
            except (
                TypeError,
                ValueError,
            ):
                target = None

        latest_raw = {
            "date": latest_date,
            "FV-M3": m3_values[
                latest_date
            ],
            "FV-FT": ft_values[
                latest_date
            ],
            "FV-RT": rt_values[
                latest_date
            ],
            "FV-M3_scaled_m3": (
                m3_values[
                    latest_date
                ] / M3_SCALE
            ),
            "raw_ratio": (
                rt_values[
                    latest_date
                ]
                / m3_values[
                    latest_date
                ]
            ),
            "temperature_c": (
                latest_temperature
            ),
            "m3_scale": M3_SCALE,
        }

        return {
            "temperature": latest_temperature,
            "date": latest_date,
            "target": target,
            "series": series,
            "raw": latest_raw,
            "good_return_temperature_data": targets,
            "return_temperature_chart_scale_y_max": (
                data.get(
                    "returnTemperatureChartScaleYMax"
                )
            ),
            "meter_info": {
                "FV-M3": m3_meter.get(
                    "meterInfo",
                    {},
                ),
                "FV-FT": ft_meter.get(
                    "meterInfo",
                    {},
                ),
                "FV-RT": rt_meter.get(
                    "meterInfo",
                    {},
                ),
            },
        }

    def fetch(self) -> dict[str, Any]:
        """Fetch and calculate HTF return temperature."""
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

            _log_json(
                "raw API data",
                data,
            )

            result = self._calculate(
                data
            )

            _log_json(
                "calculated result",
                result,
            )

            _LOGGER.info(
                "HTF return temperature: "
                "%.2f C for %s",
                result["temperature"],
                result["date"],
            )

            if result.get(
                "target"
            ) is not None:
                _LOGGER.info(
                    "HTF return temperature "
                    "target: %.1f C",
                    result["target"],
                )

            return result

        except requests.RequestException as err:
            raise UpdateFailed(
                "HTF return-temperature "
                f"network error: {err}"
            ) from err