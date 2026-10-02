"""HTF return-temperature data."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

BASE = "https://selvbetjening.htf.dk"

LOGIN_PAGE = f"{BASE}/login"

LOGIN_POST = (
    f"{BASE}/umbraco/surface/login2/PostLogin"
)

DASHBOARD_PAGE = f"{BASE}/dashboard/"

SET_SELECTED = (
    f"{BASE}/umbraco/surface/customer2/"
    "SetSelectedConsumption"
)

RETURN_TEMP_PAGE = (
    f"{BASE}/forbrugsalarm/"
)

SCAN_INTERVAL = timedelta(hours=24)


def _number(
    value: Any,
) -> float | None:
    if value is None or isinstance(
        value,
        bool,
    ):
        return None

    if isinstance(
        value,
        (int, float),
    ):
        return float(value)

    if isinstance(
        value,
        str,
    ):
        match = re.search(
            r"-?\d+(?:[.,]\d+)?",
            value,
        )

        if match:
            try:
                return float(
                    match.group(0).replace(
                        ",",
                        ".",
                    )
                )
            except ValueError:
                return None

    return None


def extract_return_temperature(
    html: str,
) -> float | None:
    """Try multiple patterns for return temperature."""
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    # ---------------------------------------------------------
    # 1. HTML/data attributes
    # ---------------------------------------------------------

    for element in soup.find_all(
        True
    ):
        for attr, value in element.attrs.items():
            name = re.sub(
                r"[^a-z0-9]",
                "",
                str(attr).lower(),
            )

            if (
                "returtemperatur"
                in name
                or "returntemperature"
                in name
                or "returntemp"
                in name
                or name
                in {
                    "returtemp",
                    "returtemperatur",
                }
            ):
                number = _number(
                    value
                )

                if (
                    number is not None
                    and -50 <= number <= 150
                ):
                    return number

    # ---------------------------------------------------------
    # 2. Search raw JavaScript/HTML
    # ---------------------------------------------------------

    patterns = [
        (
            r"(?is)"
            r"(?:returtemperatur|"
            r"returntemperature)"
            r".{0,500}?"
            r"(?:value|data|temp|"
            r"temperature|temperatur)?"
            r"\s*[:=]\s*[\"']?"
            r"(-?\d+(?:[.,]\d+)?)"
        ),
        (
            r"(?is)"
            r"(?:returtemp|returntemp)"
            r".{0,300}?"
            r"[:=]\s*[\"']?"
            r"(-?\d+(?:[.,]\d+)?)"
        ),
        (
            r"(?is)"
            r"(?:return|retur)"
            r".{0,100}"
            r"(?:temperature|"
            r"temperatur|temp)"
            r".{0,100}"
            r"(-?\d+(?:[.,]\d+)?)"
        ),
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            html,
        )

        if match:
            number = _number(
                match.group(1)
            )

            if (
                number is not None
                and -50 <= number <= 150
            ):
                return number

    # ---------------------------------------------------------
    # 3. Visible text
    # ---------------------------------------------------------

    text = soup.get_text(
        " ",
        strip=True,
    )

    visible_patterns = [
        (
            r"(?i)"
            r"(?:returtemperatur|"
            r"return\s+temperature)"
            r"\s*[:\-]?\s*"
            r"(-?\d+(?:[.,]\d+)?)"
            r"\s*°?\s*C?"
        ),
        (
            r"(?i)"
            r"(?:returtemp|returntemp)"
            r"\s*[:\-]?\s*"
            r"(-?\d+(?:[.,]\d+)?)"
            r"\s*°?\s*C?"
        ),
    ]

    for pattern in visible_patterns:
        match = re.search(
            pattern,
            text,
        )

        if match:
            number = _number(
                match.group(1)
            )

            if (
                number is not None
                and -50 <= number <= 150
            ):
                return number

    return None


class ReturnTemperatureClient:
    """HTF client dedicated to return temperature."""

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
                "HTF return temperature: "
                "opening login page"
            )

            self.session.get(
                LOGIN_PAGE,
                timeout=30,
            ).raise_for_status()

            _LOGGER.debug(
                "HTF return temperature: "
                "submitting login"
            )

            self.session.post(
                LOGIN_POST,
                data={
                    "kundenr": self.customer,
                    "pinkode": self.pin,
                },
                timeout=30,
            ).raise_for_status()

            # Select the same consumption point.
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

            if option:
                self.session.post(
                    SET_SELECTED,
                    data={
                        "value": option
                    },
                    timeout=30,
                ).raise_for_status()

            _LOGGER.debug(
                "HTF return temperature: "
                "requesting /forbrugsalarm/"
            )

            response = self.session.get(
                RETURN_TEMP_PAGE,
                timeout=30,
            )

            response.raise_for_status()

            value = extract_return_temperature(
                response.text
            )

            if value is None:
                _LOGGER.warning(
                    "HTF return temperature: "
                    "value not found"
                )
            else:
                _LOGGER.info(
                    "HTF return temperature: "
                    "value found"
                )

            return {
                "return_temperature": value
            }

        except requests.RequestException as err:
            raise UpdateFailed(
                "HTF return temperature "
                f"network error: {err}"
            ) from err


async def async_update(
    hass,
    customer: str,
    pin: str,
) -> dict[str, Any]:
    client = ReturnTemperatureClient(
        customer,
        pin,
    )

    return await hass.async_add_executor_job(
        client.fetch
    )


class ReturnTemperatureCoordinator(
    DataUpdateCoordinator[
        dict[str, Any]
    ]
):
    """Coordinator for return temperature."""

    def __init__(
        self,
        hass,
        customer: str,
        pin: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="HTF Return Temperature",
            update_method=lambda: (
                async_update(
                    hass,
                    customer,
                    pin,
                )
            ),
            update_interval=SCAN_INTERVAL,
        )