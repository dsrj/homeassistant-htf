"""Sensors for Høje Taastrup Fjernvarme Selvbetjening."""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PIN, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTemperature

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

BASE = "https://selvbetjening.htf.dk"
LOGIN_PAGE = f"{BASE}/login"
LOGIN_POST = f"{BASE}/umbraco/surface/login2/PostLogin"
CUSTOMER_PAGE = f"{BASE}/dashboard/"
SET_SELECTED = f"{BASE}/umbraco/surface/customer2/SetSelectedConsumption"
CONSUMPTION_PAGE = f"{BASE}/forbrug/"
RETURN_TEMP_PAGE = f"{BASE}/forbrugsalarm/"
ACCOUNT_PAGE = f"{BASE}/kundeoplysninger/kontoudtog/"

SCAN_INTERVAL = timedelta(hours=24)


def _number(value: Any) -> float | None:
    """Convert a value to float."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None

    return None


def _get_meter(consumption: dict[str, Any]) -> dict[str, Any]:
    meters = consumption.get("meters")

    if isinstance(meters, list) and meters:
        first = meters[0]

        if isinstance(first, dict):
            return first

    return {}


def _get_month_data(consumption: dict[str, Any]) -> dict[str, Any]:
    meter = _get_meter(consumption)

    data = meter.get("monthData")

    if isinstance(data, dict):
        return data

    data = consumption.get("monthData")

    return data if isinstance(data, dict) else {}


def _get_year_data(consumption: dict[str, Any]) -> dict[str, Any]:
    meter = _get_meter(consumption)

    data = meter.get("yearData")

    if isinstance(data, dict):
        return data

    data = consumption.get("yearData")

    return data if isinstance(data, dict) else {}


def _current_month(consumption: dict[str, Any]) -> float | None:
    """Return current calendar month's heating consumption in MWh."""
    data = _get_month_data(consumption)

    now = dt_util.now()

    year_data = data.get(f"year_{now.year}")

    if not isinstance(year_data, dict):
        return None

    values = year_data.get("values", [])
    months = year_data.get("monthNumbers", [])

    if not isinstance(values, list):
        return None

    if isinstance(months, list):
        for index, month in enumerate(months):
            if month == now.month and index < len(values):
                return _number(values[index])

    return _number(values[-1]) if values else None


def _current_year(consumption: dict[str, Any]) -> float | None:
    """Return current calendar year's heating consumption in MWh."""
    data = _get_year_data(consumption)

    now = dt_util.now()

    years = data.get("yearNumbers")
    values = data.get("values")

    if not isinstance(years, list) or not isinstance(values, list):
        return None

    for index, year in enumerate(years):
        if year == now.year and index < len(values):
            return _number(values[index])

    return None


def _find_number(
    obj: Any,
    wanted_keys: set[str],
) -> float | None:
    """Recursively find a numeric value belonging to wanted keys."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = re.sub(
                r"[^a-z0-9]",
                "",
                str(key).lower(),
            )

            if normalized in wanted_keys:
                number = _number(value)

                if number is not None:
                    return number

        for value in obj.values():
            found = _find_number(value, wanted_keys)

            if found is not None:
                return found

    elif isinstance(obj, list):
        for value in obj:
            found = _find_number(value, wanted_keys)

            if found is not None:
                return found

    return None


def _extract_json_objects(text: str) -> list[Any]:
    """Extract possible JSON objects from script text."""
    results: list[Any] = []

    for match in re.finditer(
        r"(?s)(\{.*?\}|\[.*?\])",
        text,
    ):
        candidate = match.group(1)

        try:
            results.append(json.loads(candidate))
        except (json.JSONDecodeError, TypeError):
            continue

    return results


def _extract_return_temperature(
    html: str,
) -> float | None:
    """Try several patterns to find return temperature."""
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    key_names = {
        "returntemperature",
        "returtemperatur",
        "returtemperaturc",
        "returtemperaturvalue",
        "returntemp",
        "returntempvalue",
        "returntemperaturevalue",
        "returntemperaturec",
        "returtemp",
        "returtempvalue",
        "returtempc",
        "returtemperature",
    }

    # ---------------------------------------------------------
    # 1. Search HTML data attributes.
    # ---------------------------------------------------------

    for element in soup.find_all(True):
        for attr, value in element.attrs.items():
            attr_normalized = re.sub(
                r"[^a-z0-9]",
                "",
                str(attr).lower(),
            )

            if (
                attr_normalized in key_names
                or "returtemperatur" in attr_normalized
                or "returntemperature" in attr_normalized
                or "returntemp" in attr_normalized
            ):
                number = _number(value)

                if number is not None and -50 <= number <= 150:
                    return number

    # ---------------------------------------------------------
    # 2. Search JavaScript.
    # ---------------------------------------------------------

    for script in soup.find_all("script"):
        script_text = script.string or script.get_text()

        if not script_text:
            continue

        patterns = [
            (
                r"(?i)"
                r"(?:returnTemperature|returnTemp|returTemperatur|returTemp)"
                r"\s*[:=]\s*[\"']?"
                r"(-?\d+(?:[.,]\d+)?)"
            ),
            (
                r"(?i)"
                r"(?:return_temperature|return_temperature_value|"
                r"retur_temperatur)"
                r"\s*[:=]\s*[\"']?"
                r"(-?\d+(?:[.,]\d+)?)"
            ),
            (
                r"(?i)"
                r"(?:return|retur)"
                r"[^:=,\n]{0,80}"
                r"(?:temperature|temperatur|temp)"
                r"[^:=,\n]{0,30}"
                r"[:=]\s*[\"']?"
                r"(-?\d+(?:[.,]\d+)?)"
            ),
        ]

        for pattern in patterns:
            match = re.search(pattern, script_text)

            if match:
                number = _number(match.group(1))

                if number is not None and -50 <= number <= 150:
                    return number

        # Try JSON fragments inside scripts.
        for obj in _extract_json_objects(script_text):
            found = _find_number(
                obj,
                key_names,
            )

            if found is not None and -50 <= found <= 150:
                return found

    # ---------------------------------------------------------
    # 3. Search visible page text.
    # ---------------------------------------------------------

    text = soup.get_text(" ", strip=True)

    text_patterns = [
        (
            r"(?i)"
            r"(?:returtemperatur|return\s+temperature|"
            r"returntemperatur)"
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

    for pattern in text_patterns:
        match = re.search(pattern, text)

        if match:
            number = _number(match.group(1))

            if number is not None and -50 <= number <= 150:
                return number

    # ---------------------------------------------------------
    # 4. Search raw HTML near return-temperature identifiers.
    # ---------------------------------------------------------

    source_patterns = [
        (
            r"(?is)"
            r"(?:returtemperatur|returntemperature|"
            r"returtemp|returntemp)"
            r".{0,250}?"
            r"(-?\d+(?:[.,]\d+)?)"
            r"\s*(?:°\s*C|degC|celsius)?"
        ),
    ]

    for pattern in source_patterns:
        match = re.search(pattern, html)

        if match:
            number = _number(match.group(1))

            if number is not None and -50 <= number <= 150:
                return number

    return None


def _parse_danish_amount(
    value: str,
) -> float | None:
    """Parse Danish amount such as 2.686,03."""
    if not value:
        return None

    text = value.strip()

    text = re.sub(
        r"[^\d,.\-]",
        "",
        text,
    )

    if not text:
        return None

    if "," in text:
        text = text.replace(".", "")
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def _parse_bills(html: str) -> dict[str, Any]:
    """Parse account statement data."""
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    tables: list[list[dict[str, str]]] = []

    for table in soup.find_all("table"):
        rows: list[dict[str, str]] = []

        headers = [
            cell.get_text(" ", strip=True)
            for cell in table.find_all("th")
        ]

        if not headers:
            first_row = table.find("tr")

            if first_row:
                headers = [
                    cell.get_text(" ", strip=True)
                    for cell in first_row.find_all(
                        ["th", "td"]
                    )
                ]

        for row in table.find_all("tr"):
            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all("td")
            ]

            if not cells:
                continue

            if headers and len(headers) == len(cells):
                rows.append(
                    dict(zip(headers, cells))
                )

        if rows:
            tables.append(rows)

    amounts: list[float] = []
    bill_rows: list[dict[str, str]] = []

    for table in tables:
        for row in table:
            amount_text = ""
            description = ""

            for key, value in row.items():
                key_normalized = re.sub(
                    r"[^a-z0-9]",
                    "",
                    key.lower(),
                )

                if key_normalized in {
                    "beløb",
                    "beloeb",
                    "amount",
                }:
                    amount_text = value

                if key_normalized in {
                    "beskrivelse",
                    "description",
                }:
                    description = value

            amount = _parse_danish_amount(
                amount_text
            )

            if amount is not None:
                amounts.append(amount)

            if description:
                description_normalized = (
                    description.lower()
                )

                if (
                    "aconto" in description_normalized
                    or "opgørelse" in description_normalized
                    or "opgorelse" in description_normalized
                ):
                    bill_rows.append(row)

    balance = (
        round(sum(amounts), 2)
        if amounts
        else None
    )

    latest_bill = None
    due_date = None

    if bill_rows:
        latest = bill_rows[0]

        for key, value in latest.items():
            key_normalized = re.sub(
                r"[^a-z0-9]",
                "",
                key.lower(),
            )

            if key_normalized in {
                "beløb",
                "beloeb",
                "amount",
            }:
                latest_bill = _parse_danish_amount(
                    value
                )

            elif key_normalized in {
                "forfaldsdato",
                "duedate",
            }:
                due_date = value or None

    return {
        "balance": balance,
        "latest_bill": latest_bill,
        "due_date": due_date,
    }


def _find_json_block(
    soup: BeautifulSoup,
    element_id: str,
) -> dict[str, Any]:
    """Read JSON from a hidden HTML element."""
    element = soup.find(id=element_id)

    if element is None:
        return {}

    raw = element.get_text(
        strip=True
    )

    if not raw:
        return {}

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return (
        value
        if isinstance(value, dict)
        else {}
    )


class HTFClient:
    """Synchronous client for HTF portal."""

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

    def _get(
        self,
        url: str,
    ) -> requests.Response:
        response = self.session.get(
            url,
            timeout=30,
        )

        response.raise_for_status()

        return response

    def _post(
        self,
        url: str,
        data: dict[str, str],
    ) -> requests.Response:
        response = self.session.post(
            url,
            data=data,
            timeout=30,
        )

        response.raise_for_status()

        return response

    def login(self) -> None:
        _LOGGER.debug(
            "HTF: opening login page"
        )

        self._get(LOGIN_PAGE)

        _LOGGER.debug(
            "HTF: submitting login"
        )

        response = self._post(
            LOGIN_POST,
            {
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
        )

        if response.status_code >= 400:
            raise UpdateFailed(
                "HTF login failed"
            )

        _LOGGER.debug(
            "HTF: login successful"
        )

    def select_consumption_point(self) -> None:
        _LOGGER.debug(
            "HTF: opening dashboard"
        )

        dashboard = self._get(
            CUSTOMER_PAGE
        )

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
                "HTF consumption point was not found"
            )

        _LOGGER.debug(
            "HTF: consumption point found"
        )

        response = self._post(
            SET_SELECTED,
            {
                "value": option
            },
        )

        if response.status_code >= 400:
            raise UpdateFailed(
                "HTF consumption point selection failed"
            )

        _LOGGER.debug(
            "HTF: consumption point selected"
        )

    def _get_consumption(
        self,
    ) -> dict[str, Any]:
        _LOGGER.debug(
            "HTF: requesting /forbrug/"
        )

        response = self._get(
            CONSUMPTION_PAGE
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        data = _find_json_block(
            soup,
            "consumption-data-json",
        )

        if not data:
            raise UpdateFailed(
                "HTF consumption data was not found"
            )

        _LOGGER.debug(
            "HTF: consumption data received"
        )

        return data

    def _get_return_temperature(
        self,
    ) -> float | None:
        _LOGGER.debug(
            "HTF: requesting /forbrugsalarm/"
        )

        response = self._get(
            RETURN_TEMP_PAGE
        )

        value = _extract_return_temperature(
            response.text
        )

        if value is None:
            _LOGGER.debug(
                "HTF: return temperature not found"
            )
        else:
            _LOGGER.debug(
                "HTF: return temperature found"
            )

        return value

    def _get_bills(
        self,
    ) -> dict[str, Any]:
        _LOGGER.debug(
            "HTF: requesting "
            "/kundeoplysninger/kontoudtog/"
        )

        response = self._get(
            ACCOUNT_PAGE
        )

        data = _parse_bills(
            response.text
        )

        _LOGGER.debug(
            "HTF: account data received"
        )

        return data

    def fetch(self) -> dict[str, Any]:
        """Fetch all HTF data."""
        try:
            self.login()

            self.select_consumption_point()

            consumption = (
                self._get_consumption()
            )

            return_temperature = (
                self._get_return_temperature()
            )

            bills = self._get_bills()

            return {
                "consumption": consumption,
                "return_temperature": (
                    return_temperature
                ),
                "bills": bills,
            }

        except requests.RequestException as err:
            raise UpdateFailed(
                f"HTF network request failed: {err}"
            ) from err

        except UpdateFailed:
            raise

        except Exception as err:
            raise UpdateFailed(
                f"HTF data update failed: {err}"
            ) from err


async def _async_update_data(
    hass: HomeAssistant,
    customer: str,
    pin: str,
) -> dict[str, Any]:
    """Fetch HTF data outside event loop."""
    client = HTFClient(
        customer,
        pin,
    )

    return await hass.async_add_executor_job(
        client.fetch
    )


class HTFCoordinator(
    DataUpdateCoordinator[dict[str, Any]]
):
    """Coordinate HTF updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        customer: str,
        pin: str,
    ) -> None:
        self.customer = customer
        self.pin = pin

        super().__init__(
            hass,
            _LOGGER,
            name="HTF Selvbetjening",
            update_method=lambda: (
                _async_update_data(
                    hass,
                    customer,
                    pin,
                )
            ),
            update_interval=SCAN_INTERVAL,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up HTF sensors."""
    customer = entry.data.get(
        "customer",
        entry.data.get(
            CONF_USERNAME,
            "",
        ),
    )

    pin = entry.data.get(
        "pin",
        entry.data.get(
            CONF_PIN,
            "",
        ),
    )

    coordinator = HTFCoordinator(
        hass,
        customer,
        pin,
    )

    async def _async_first_refresh(
        coordinator: HTFCoordinator,
    ) -> None:
        try:
            _LOGGER.debug(
                "HTF: starting first background refresh"
            )

            await coordinator.async_config_entry_first_refresh()

            _LOGGER.info(
                "HTF: first refresh completed successfully"
            )

        except Exception:
            _LOGGER.exception(
                "HTF: first refresh failed"
            )

    hass.async_create_task(
        _async_first_refresh(coordinator)
    )

    entities = [
        HTFCurrentMonthSensor(
            coordinator
        ),
        HTFDailyAverageSensor(
            coordinator
        ),
        HTFCurrentYearSensor(
            coordinator
        ),
        HTFReturnTemperatureSensor(
            coordinator
        ),
        HTFBalanceSensor(
            coordinator
        ),
        HTFLatestBillSensor(
            coordinator
        ),
        HTFLatestBillDueDateSensor(
            coordinator
        ),
        HTFBillStatusSensor(
            coordinator
        ),
    ]

    async_add_entities(
        entities
    )


class HTFEntity(
    CoordinatorEntity[HTFCoordinator]
):
    """Base HTF entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(
            coordinator
        )

    @property
    def device_info(
        self,
    ) -> dict[str, Any]:
        return {
            "identifiers": {
                (DOMAIN, "heating")
            },
            "name": "HTF Heating",
            "manufacturer": (
                "Høje Taastrup Fjernvarme"
            ),
            "configuration_url": BASE,
        }


class HTFCurrentMonthSensor(
    HTFEntity,
    SensorEntity,
):
    """Current month's consumption."""

    _attr_name = "Current Month"
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_device_class = (
        SensorDeviceClass.ENERGY
    )
    _attr_state_class = (
        SensorStateClass.TOTAL
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        return _current_month(
            self.coordinator.data[
                "consumption"
            ]
        )


class HTFDailyAverageSensor(
    HTFEntity,
    SensorEntity,
):
    """Daily average consumption."""

    _attr_name = "Daily Average"
    _attr_native_unit_of_measurement = (
        "kWh/day"
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        current = _current_month(
            self.coordinator.data[
                "consumption"
            ]
        )

        if current is None:
            return None

        now = dt_util.now()

        days_elapsed = now.day

        if days_elapsed <= 0:
            return None

        return round(
            (current * 1000)
            / days_elapsed,
            1,
        )


class HTFCurrentYearSensor(
    HTFEntity,
    SensorEntity,
):
    """Current year's consumption."""

    _attr_name = "Current Year"
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_device_class = (
        SensorDeviceClass.ENERGY
    )
    _attr_state_class = (
        SensorStateClass.TOTAL
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        return _current_year(
            self.coordinator.data[
                "consumption"
            ]
        )


class HTFReturnTemperatureSensor(
    HTFEntity,
    SensorEntity,
):
    """District heating return temperature."""

    _attr_name = "Return Temperature"
    _attr_native_unit_of_measurement = (
        UnitOfTemperature.CELSIUS
    )
    _attr_device_class = (
        SensorDeviceClass.TEMPERATURE
    )
    _attr_state_class = (
        SensorStateClass.MEASUREMENT
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        return self.coordinator.data.get(
            "return_temperature"
        )


class HTFBalanceSensor(
    HTFEntity,
    SensorEntity,
):
    """Current account balance."""

    _attr_name = "Balance"
    _attr_native_unit_of_measurement = (
        "DKK"
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        return self.coordinator.data.get(
            "bills",
            {},
        ).get("balance")


class HTFLatestBillSensor(
    HTFEntity,
    SensorEntity,
):
    """Latest bill amount."""

    _attr_name = "Latest Bill"
    _attr_native_unit_of_measurement = (
        "DKK"
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        return self.coordinator.data.get(
            "bills",
            {},
        ).get("latest_bill")


class HTFLatestBillDueDateSensor(
    HTFEntity,
    SensorEntity,
):
    """Latest bill due date."""

    _attr_name = "Latest Bill Due Date"

    @property
    def native_value(
        self,
    ) -> str | None:
        return self.coordinator.data.get(
            "bills",
            {},
        ).get("due_date")


class HTFBillStatusSensor(
    HTFEntity,
    SensorEntity,
):
    """Current bill status."""

    _attr_name = "Bill Status"

    @property
    def native_value(self) -> str:
        balance = self.coordinator.data.get(
            "bills",
            {},
        ).get("balance")

        if balance is None:
            return "Unknown"

        if balance > 0.01:
            return "Amount Due"

        if balance < -0.01:
            return "Credit"

        return "Settled"