"""HTF return temperature support."""

from __future__ import annotations

import logging
import re
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
    """Client for the HTF return-temperature page."""

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
        _LOGGER.debug(
            "HTF return temperature: opening login page"
        )

        response = self.session.get(
            f"{BASE}/login",
            timeout=30,
        )
        response.raise_for_status()

        _LOGGER.debug(
            "HTF return temperature: submitting login"
        )

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

        _LOGGER.debug(
            "HTF return temperature: opening dashboard"
        )

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

        _LOGGER.debug(
            "HTF return temperature: consumption point selected"
        )

    @staticmethod
    def _find_temperature_candidates(
        html: str,
    ) -> list[str]:
        """Find names in the page that may relate to temperature."""
        candidates: set[str] = set()

        patterns = (
            r"\b[A-Za-z_$][\w$]*[Tt]emperature[\w$]*\b",
            r"\b[A-Za-z_$][\w$]*[Tt]emp[\w$]*\b",
            r"\b[A-Za-z_$][\w$]*[Rr]eturn[\w$]*\b",
            r"\b[A-Za-z_$][\w$]*[Ff]remløb[\w$]*\b",
        )

        for pattern in patterns:
            candidates.update(
                re.findall(
                    pattern,
                    html,
                )
            )

        return sorted(candidates)

    @staticmethod
    def _find_relevant_elements(
        html: str,
    ) -> list[str]:
        """Find HTML elements with temperature-related names."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: set[str] = set()

        for element in soup.find_all(
            ["div", "span", "p", "td", "th", "script"],
        ):
            text = element.get_text(
                " ",
                strip=True,
            )

            if not text:
                continue

            lowered = text.lower()

            if any(
                term in lowered
                for term in (
                    "temperatur",
                    "temperature",
                    "returtemperatur",
                    "return temperature",
                    "retur",
                )
            ):
                # Only record the tag and a short structural
                # description. Do not log the actual contents.
                element_id = element.get("id")
                element_class = element.get("class")

                description = element.name

                if element_id:
                    description += (
                        f"#{element_id}"
                    )

                if element_class:
                    classes = " ".join(
                        element_class
                        if isinstance(
                            element_class,
                            list,
                        )
                        else [str(element_class)]
                    )

                    description += (
                        f".{classes.replace(' ', '.')}"
                    )

                results.add(description)

        return sorted(results)

    @staticmethod
    def _find_json_scripts(
        html: str,
    ) -> list[str]:
        """Find script blocks that appear to contain temperature data."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: list[str] = []

        for script in soup.find_all(
            "script"
        ):
            text = script.string or script.get_text()

            if not text:
                continue

            lowered = text.lower()

            if any(
                term in lowered
                for term in (
                    "temperature",
                    "temperatur",
                    "returntemperature",
                    "return_temperature",
                    "returtemperatur",
                )
            ):
                # Record only the first line/shape, not the contents.
                first_line = (
                    text.strip()
                    .splitlines()[0][:200]
                    if text.strip()
                    else ""
                )

                if first_line:
                    results.append(
                        first_line
                    )

        return results[:20]

    def fetch(self) -> dict[str, Any]:
        """Fetch and inspect the return-temperature page."""
        try:
            self._login()
            self._select_consumption_point()

            _LOGGER.debug(
                "HTF return temperature: requesting /forbrugsalarm/"
            )

            response = self.session.get(
                f"{BASE}/forbrugsalarm/",
                timeout=30,
            )
            response.raise_for_status()

            html = response.text

            if not html:
                raise UpdateFailed(
                    "HTF return temperature page was empty"
                )

            _LOGGER.debug(
                "HTF return temperature: page received "
                "(%s bytes)",
                len(html),
            )

            candidates = (
                self._find_temperature_candidates(
                    html
                )
            )

            elements = (
                self._find_relevant_elements(
                    html
                )
            )

            scripts = (
                self._find_json_scripts(
                    html
                )
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "possible variables=%s",
                candidates,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "relevant elements=%s",
                elements,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "temperature-related scripts=%s",
                len(scripts),
            )

            return {
                "temperature": None,
                "diagnostic_variables": candidates,
                "diagnostic_elements": elements,
                "diagnostic_script_count": len(
                    scripts
                ),
            }

        except requests.RequestException as err:
            raise UpdateFailed(
                f"HTF return temperature network error: {err}"
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
                f"Unable to fetch HTF return temperature: {err}"
            ) from err