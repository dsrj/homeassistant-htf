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
            r"\b[A-Za-z_$][\w$]*[Rr]etur[\w$]*\b",
        )

        for pattern in patterns:
            candidates.update(
                re.findall(pattern, html)
            )

        return sorted(candidates)

    @staticmethod
    def _describe_element(element: Any) -> str:
        """Return a safe structural description of an HTML element."""
        description = element.name

        element_id = element.get("id")
        if element_id:
            description += f"#{element_id}"

        element_class = element.get("class")
        if element_class:
            if isinstance(element_class, list):
                classes = " ".join(element_class)
            else:
                classes = str(element_class)

            description += (
                f".{classes.replace(' ', '.')}"
            )

        return description

    @staticmethod
    def _find_relevant_elements(
        html: str,
    ) -> list[str]:
        """Find HTML elements related to return temperature."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: set[str] = set()

        for element in soup.find_all(
            [
                "div",
                "span",
                "p",
                "td",
                "th",
                "label",
                "canvas",
                "svg",
                "script",
            ]
        ):
            text = element.get_text(
                " ",
                strip=True,
            )

            lowered = text.lower()

            if any(
                term in lowered
                for term in (
                    "temperatur",
                    "temperature",
                    "returtemperatur",
                    "return temperature",
                    "returntemp",
                    "returtemp",
                    "retur",
                )
            ):
                results.add(
                    ReturnTemperatureClient._describe_element(
                        element
                    )
                )

        return sorted(results)

    @staticmethod
    def _inspect_return_temperature_section(
        html: str,
    ) -> dict[str, Any]:
        """Inspect the dedicated return-temperature page section."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        section = soup.select_one(
            "#returntemperature-page-section"
        )

        if section is None:
            return {
                "section_found": False,
                "section_children": [],
                "section_data_attributes": [],
                "section_numeric_values": [],
                "section_labels": [],
            }

        children: set[str] = set()
        data_attributes: set[str] = set()
        labels: set[str] = set()

        for element in section.find_all(True):
            children.add(
                ReturnTemperatureClient._describe_element(
                    element
                )
            )

            for attribute_name, attribute_value in (
                element.attrs.items()
            ):
                if attribute_name.startswith("data-"):
                    data_attributes.add(
                        f"{attribute_name}={attribute_value}"
                    )

            text = element.get_text(
                " ",
                strip=True,
            )

            lowered = text.lower()

            if any(
                term in lowered
                for term in (
                    "temperatur",
                    "temperature",
                    "retur",
                    "returntemp",
                )
            ):
                if text:
                    # Keep only short structural labels.
                    cleaned = " ".join(
                        text.split()
                    )

                    if len(cleaned) <= 150:
                        labels.add(cleaned)

        # Numeric values are collected from the dedicated section,
        # but only values that look like temperatures are retained.
        section_text = section.get_text(
            " ",
            strip=True,
        )

        numeric_values: list[str] = []

        for match in re.findall(
            r"(?<![\d.,])\d{1,3}(?:[.,]\d{1,3})?(?![\d.,])",
            section_text,
        ):
            try:
                value = float(
                    match.replace(",", ".")
                )
            except ValueError:
                continue

            # Return temperatures are normally in a human-readable
            # Celsius range. This is only diagnostic filtering.
            if -20 <= value <= 100:
                numeric_values.append(match)

        return {
            "section_found": True,
            "section_tag": section.name,
            "section_id": section.get("id"),
            "section_class": section.get("class"),
            "section_children": sorted(children)[:100],
            "section_data_attributes": sorted(
                data_attributes
            )[:100],
            "section_numeric_values": numeric_values[:100],
            "section_labels": sorted(labels)[:50],
        }

    @staticmethod
    def _find_temperature_data_attributes(
        html: str,
    ) -> list[str]:
        """Find data-* attributes whose names may contain temperature data."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: set[str] = set()

        for element in soup.find_all(True):
            for name, value in element.attrs.items():
                lowered = name.lower()

                if (
                    name.startswith("data-")
                    and any(
                        term in lowered
                        for term in (
                            "temp",
                            "temperatur",
                            "retur",
                            "return",
                        )
                    )
                ):
                    results.add(
                        f"{name}={value}"
                    )

        return sorted(results)[:100]

    @staticmethod
    def _find_chart_elements(
        html: str,
    ) -> list[str]:
        """Find chart-related elements inside the return-temperature section."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        section = soup.select_one(
            "#returntemperature-page-section"
        )

        if section is None:
            return []

        results: set[str] = set()

        for element in section.find_all(
            [
                "canvas",
                "svg",
                "iframe",
                "img",
                "script",
            ]
        ):
            results.add(
                ReturnTemperatureClient._describe_element(
                    element
                )
            )

        return sorted(results)

    @staticmethod
    def _find_temperature_scripts(
        html: str,
    ) -> list[str]:
        """Find scripts containing return-temperature configuration."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: list[str] = []

        for script in soup.find_all("script"):
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
                    "returntemp",
                    "returtemp",
                )
            ):
                first_line = (
                    text.strip()
                    .splitlines()[0][:200]
                    if text.strip()
                    else ""
                )

                if first_line:
                    results.append(first_line)

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
                "HTF return temperature: page received (%s bytes)",
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
                self._find_temperature_scripts(
                    html
                )
            )

            section = (
                self._inspect_return_temperature_section(
                    html
                )
            )

            data_attributes = (
                self._find_temperature_data_attributes(
                    html
                )
            )

            chart_elements = (
                self._find_chart_elements(
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

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "section=%s",
                section,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "temperature data attributes=%s",
                data_attributes,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "chart elements=%s",
                chart_elements,
            )

            return {
                "temperature": None,
                "diagnostic_variables": candidates,
                "diagnostic_elements": elements,
                "diagnostic_script_count": len(scripts),
                "diagnostic_section": section,
                "diagnostic_data_attributes": data_attributes,
                "diagnostic_chart_elements": chart_elements,
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