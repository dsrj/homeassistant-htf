"""HTF return temperature support."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin

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
        """Inspect the dedicated return-temperature chart section."""
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
                cleaned = " ".join(
                    text.split()
                )

                if cleaned and len(cleaned) <= 150:
                    labels.add(cleaned)

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
    def _inspect_canvas(
        html: str,
    ) -> dict[str, Any]:
        """Inspect the return-temperature chart canvas."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        section = soup.select_one(
            "#returntemperature-page-section"
        )

        if section is None:
            return {
                "canvas_found": False,
                "canvas_attributes": [],
                "ancestors": [],
                "siblings": [],
            }

        canvas = section.select_one(
            "canvas.chart-canvas"
        )

        if canvas is None:
            canvas = section.find("canvas")

        if canvas is None:
            return {
                "canvas_found": False,
                "canvas_attributes": [],
                "ancestors": [],
                "siblings": [],
            }

        attributes = []

        for name, value in canvas.attrs.items():
            attributes.append(
                f"{name}={value}"
            )

        ancestors: list[str] = []

        parent = canvas.parent

        for _ in range(8):
            if parent is None:
                break

            if not getattr(
                parent,
                "name",
                None,
            ):
                break

            ancestors.append(
                ReturnTemperatureClient._describe_element(
                    parent
                )
            )

            parent = parent.parent

        siblings: list[str] = []

        if canvas.parent is not None:
            for sibling in canvas.parent.find_all(
                recursive=False
            ):
                if sibling is canvas:
                    continue

                if getattr(
                    sibling,
                    "name",
                    None,
                ):
                    siblings.append(
                        ReturnTemperatureClient._describe_element(
                            sibling
                        )
                    )

        return {
            "canvas_found": True,
            "canvas_attributes": sorted(attributes),
            "ancestors": ancestors,
            "siblings": sorted(siblings)[:50],
        }

    @staticmethod
    def _find_temperature_named_elements(
        html: str,
    ) -> list[str]:
        """Find elements with temperature-related attributes."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: set[str] = set()

        terms = (
            "temperature",
            "temperatur",
            "returntemp",
            "return_temperature",
            "returtemperatur",
            "returtemp",
            "return",
            "retur",
        )

        attributes = (
            "id",
            "name",
            "class",
            "data-name",
            "data-key",
            "data-variable",
            "data-field",
            "data-chart",
            "data-target",
            "data-url",
        )

        for element in soup.find_all(True):
            values: list[str] = []

            for attribute_name in attributes:
                value = element.get(
                    attribute_name
                )

                if value is None:
                    continue

                if isinstance(value, list):
                    values.extend(
                        str(item)
                        for item in value
                    )
                else:
                    values.append(
                        str(value)
                    )

            combined = " ".join(
                values
            ).lower()

            if any(
                term in combined
                for term in terms
            ):
                results.add(
                    ReturnTemperatureClient._describe_element(
                        element
                    )
                )

        return sorted(results)[:200]

    @staticmethod
    def _find_external_scripts(
        html: str,
    ) -> list[str]:
        """Find external JavaScript files loaded by the page."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        results: list[str] = []

        for script in soup.find_all("script"):
            src = script.get("src")

            if not src:
                continue

            absolute_url = urljoin(
                BASE,
                src,
            )

            if absolute_url not in results:
                results.append(
                    absolute_url
                )

            if len(results) >= 100:
                break

        return results

    def _inspect_external_scripts(
        self,
        html: str,
    ) -> list[str]:
        """Search external JavaScript for return-temperature clues."""
        script_urls = self._find_external_scripts(
            html
        )

        findings: list[str] = []

        terms = (
            "returnTempMeters",
            "goodReturnTemperatureData",
            "Returtemperatur",
            "returntemperature",
            "returtemperatur",
            "return_temperature",
            "returtemp",
            "temperature",
            "temperatur",
        )

        for script_url in script_urls:
            try:
                response = self.session.get(
                    script_url,
                    timeout=15,
                )

                if not response.ok:
                    continue

                script_text = response.text

                lowered = script_text.lower()

                matched = [
                    term
                    for term in terms
                    if term.lower() in lowered
                ]

                if not matched:
                    continue

                findings.append(
                    f"SCRIPT {script_url} "
                    f"matches={matched}"
                )

                # Find API-looking URLs and endpoint strings.
                urls = re.findall(
                    r"""["']((?:https?:)?//[^"']+|/[^"']*(?:api|umbraco|surface)[^"']*)["']""",
                    script_text,
                    flags=re.IGNORECASE,
                )

                for url in urls[:30]:
                    findings.append(
                        f"ENDPOINT {url}"
                    )

                # Find short contexts around the known HTF variables.
                for term in terms:
                    start = 0

                    while True:
                        position = lowered.find(
                            term.lower(),
                            start,
                        )

                        if position == -1:
                            break

                        context_start = max(
                            0,
                            position - 250,
                        )

                        context_end = min(
                            len(script_text),
                            position
                            + len(term)
                            + 500,
                        )

                        context = script_text[
                            context_start:context_end
                        ]

                        context = " ".join(
                            context.split()
                        )

                        # Avoid dumping huge script sections.
                        findings.append(
                            f"CONTEXT {term}: "
                            f"{context[:800]}"
                        )

                        start = (
                            position
                            + len(term)
                        )

                        if len(findings) >= 100:
                            return findings

            except requests.RequestException:
                continue

        return findings[:100]

    @staticmethod
    def _find_inline_temperature_context(
        html: str,
    ) -> list[str]:
        """Find short contexts around known temperature variables in the HTML."""
        results: list[str] = []

        terms = (
            "returnTempMeters",
            "goodReturnTemperatureData",
            "Returtemperatur",
            "returntemperature",
            "returtemperatur",
            "return_temperature",
            "returtemp",
        )

        lowered = html.lower()

        for term in terms:
            start = 0

            while True:
                position = lowered.find(
                    term.lower(),
                    start,
                )

                if position == -1:
                    break

                context_start = max(
                    0,
                    position - 250,
                )

                context_end = min(
                    len(html),
                    position
                    + len(term)
                    + 500,
                )

                context = html[
                    context_start:context_end
                ]

                context = " ".join(
                    context.split()
                )

                results.append(
                    f"{term}: {context[:800]}"
                )

                start = (
                    position
                    + len(term)
                )

                if len(results) >= 30:
                    return results

        return results

    def fetch(self) -> dict[str, Any]:
        """Fetch and diagnose the return-temperature chart."""
        try:
            self._login()
            self._select_consumption_point()

            _LOGGER.debug(
                "HTF return temperature: "
                "requesting /forbrugsalarm/"
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
                "HTF return temperature: "
                "page received (%s bytes)",
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

            section = (
                self._inspect_return_temperature_section(
                    html
                )
            )

            canvas = (
                self._inspect_canvas(
                    html
                )
            )

            named_elements = (
                self._find_temperature_named_elements(
                    html
                )
            )

            external_scripts = (
                self._find_external_scripts(
                    html
                )
            )

            inline_context = (
                self._find_inline_temperature_context(
                    html
                )
            )

            script_findings = (
                self._inspect_external_scripts(
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
                "section=%s",
                section,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "canvas=%s",
                canvas,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "temperature-named elements=%s",
                named_elements,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "external scripts count=%s",
                len(external_scripts),
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "inline temperature context=%s",
                inline_context,
            )

            _LOGGER.warning(
                "HTF return temperature diagnostic: "
                "external script findings=%s",
                script_findings,
            )

            return {
                "temperature": None,
                "diagnostic_variables": candidates,
                "diagnostic_elements": elements,
                "diagnostic_section": section,
                "diagnostic_canvas": canvas,
                "diagnostic_named_elements": named_elements,
                "diagnostic_external_scripts": external_scripts,
                "diagnostic_inline_context": inline_context,
                "diagnostic_script_findings": script_findings,
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