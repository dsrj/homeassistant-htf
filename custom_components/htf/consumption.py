"""HTF consumption data and sensors."""
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
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

BASE = "https://selvbetjening.htf.dk"
LOGIN_PAGE = f"{BASE}/login"
LOGIN_POST = f"{BASE}/umbraco/surface/login2/PostLogin"
DASHBOARD_PAGE = f"{BASE}/dashboard/"
SET_SELECTED = (
    f"{BASE}/umbraco/surface/customer2/"
    "SetSelectedConsumption"
)
CONSUMPTION_PAGE = f"{BASE}/forbrug/"

SCAN_INTERVAL = timedelta(hours=24)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        try:
            return float(
                value.strip().replace(",", ".")
            )
        except ValueError:
            return None

    return None


def _find_consumption_json(
    html: str,
) -> dict[str, Any]:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    element = soup.find(
        id="consumption-data-json"
    )

    if element is None:
        return {}

    raw = element.get_text(
        strip=True
    )

    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return (
        data
        if isinstance(data, dict)
        else {}
    )


def _get_meter(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    meters = consumption.get("meters")

    if isinstance(meters, list) and meters:
        if isinstance(meters[0], dict):
            return meters[0]

    return {}


def _get_month_data(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    meter = _get_meter(consumption)

    data = meter.get("monthData")

    if isinstance(data, dict):
        return data

    data = consumption.get("monthData")

    return (
        data
        if isinstance(data, dict)
        else {}
    )


def _get_year_data(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    meter = _get_meter(consumption)

    data = meter.get("yearData")

    if isinstance(data, dict):
        return data

    data = consumption.get("yearData")

    return (
        data
        if isinstance(data, dict)
        else {}
    )


def current_month(
    consumption: dict[str, Any],
) -> float | None:
    data = _get_month_data(
        consumption
    )

    now = dt_util.now()

    year_data = data.get(
        f"year_{now.year}"
    )

    if not isinstance(year_data, dict):
        return None

    values = year_data.get(
        "values",
        [],
    )

    months = year_data.get(
        "monthNumbers",
        [],
    )

    if not isinstance(values, list):
        return None

    if isinstance(months, list):
        for index, month in enumerate(
            months
        ):
            if (
                month == now.month
                and index < len(values)
            ):
                return _number(
                    values[index]
                )

    return (
        _number(values[-1])
        if values
        else None
    )


def current_year(
    consumption: dict[str, Any],
) -> float | None:
    data = _get_year_data(
        consumption
    )

    now = dt_util.now()

    years = data.get(
        "yearNumbers"
    )

    values = data.get(
        "values"
    )

    if not isinstance(years, list):
        return None

    if not isinstance(values, list):
        return None

    for index, year in enumerate(
        years
    ):
        if (
            year == now.year
            and index < len(values)
        ):
            return _number(
                values[index]
            )

    return None


class ConsumptionClient:
    """HTF client dedicated to consumption."""

    def __init__(
        self,
        customer: str,
        pin: str,
    ) -> None:
        self.customer = customer
        self.pin = pin

        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Home Assistant HTF integration)"
                ),
                "Accept": (
                    "text/html,"
                    "application/xhtml+xml,"
                    "application/json"
                ),
            }
        )

    def fetch(
        self,
    ) -> dict[str, Any]:
        try:
            _LOGGER.debug(
                "HTF consumption: "
                "opening login page"
            )

            self.session.get(
                LOGIN_PAGE,
                timeout=30,
            ).raise_for_status()

            _LOGGER.debug(
                "HTF consumption: "
                "submitting login"
            )

            response = self.session.post(
                LOGIN_POST,
                data={
                    "kundenr": self.customer,
                    "pinkode": self.pin,
                },
                timeout=30,
            )

            response.raise_for_status()

            _LOGGER.debug(
                "HTF consumption: "
                "opening dashboard"
            )

            dashboard = self.session.get(
                DASHBOARD_PAGE,
                timeout=30,
            )

            dashboard.raise_for_status()

            soup = BeautifulSoup(
                dashboard.text,
                "html.parser",
            )

            option = None

            for candidate in soup.find_all(
                "option"
            ):
                value = candidate.get(
                    "value"
                )

                if (
                    isinstance(value, str)
                    and value.count(";") >= 3
                ):
                    option = value
                    break

            if not option:
                raise UpdateFailed(
                    "HTF consumption point "
                    "was not found"
                )

            _LOGGER.debug(
                "HTF consumption: "
                "consumption point found"
            )

            selected = self.session.post(
                SET_SELECTED,
                data={
                    "value": option
                },
                timeout=30,
            )

            selected.raise_for_status()

            _LOGGER.debug(
                "HTF consumption: "
                "requesting /forbrug/"
            )

            consumption_page = (
                self.session.get(
                    CONSUMPTION_PAGE,
                    timeout=30,
                )
            )

            consumption_page.raise_for_status()

            data = _find_consumption_json(
                consumption_page.text
            )

            if not data:
                raise UpdateFailed(
                    "HTF consumption data "
                    "was not found"
                )

            _LOGGER.info(
                "HTF consumption: "
                "data update successful"
            )

            return data

        except requests.RequestException as err:
            raise UpdateFailed(
                f"HTF consumption network error: {err}"
            ) from err


async def async_update(
    hass: HomeAssistant,
    customer: str,
    pin: str,
) -> dict[str, Any]:
    client = ConsumptionClient(
        customer,
        pin,
    )

    return await hass.async_add_executor_job(
        client.fetch
    )


class ConsumptionCoordinator(
    DataUpdateCoordinator[
        dict[str, Any]
    ]
):
    """Coordinator for HTF consumption."""

    def __init__(
        self,
        hass: HomeAssistant,
        customer: str,
        pin: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="HTF Consumption",
            update_method=lambda: (
                async_update(
                    hass,
                    customer,
                    pin,
                )
            ),
            update_interval=SCAN_INTERVAL,
        )