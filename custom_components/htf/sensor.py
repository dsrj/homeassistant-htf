"""HTF Selvbetjening sensor platform."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

DOMAIN = "htf"
BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=6)

_LOGGER = logging.getLogger(__name__)


class HTFClient:
    """Client for HTF Selvbetjening."""

    def __init__(self, customer: str, pin: str) -> None:
        self.customer = customer
        self.pin = pin
        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/154.0.0.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def _get_consumption_point(
        self,
        dashboard_html: str,
    ) -> dict[str, str]:
        """Extract consumption point information dynamically."""

        soup = BeautifulSoup(
            dashboard_html,
            "html.parser",
        )

        dropdown = soup.select_one(
            ".subheader-consumptionpoint-dropdown"
        )

        if not dropdown:
            raise RuntimeError(
                "HTF consumption point dropdown was not found."
            )

        option = dropdown.select_one(
            "option[selected]"
        )

        if not option:
            option = dropdown.select_one("option")

        if not option:
            raise RuntimeError(
                "HTF consumption point option was not found."
            )

        value = option.get("value")

        if not value:
            raise RuntimeError(
                "HTF consumption point value is empty."
            )

        parts = [
            part.strip()
            for part in value.split(";")
        ]

        if len(parts) != 4:
            raise RuntimeError(
                "Unexpected HTF consumption point format."
            )

        return {
            "ConsumptionPointId": parts[0],
            "ConsumerId": parts[1],
            "DebtorId": parts[2],
            "CustomerId": parts[3],
        }

    def _select_consumption_point(
        self,
        dashboard_html: str,
    ) -> None:
        """Tell HTF which consumption point is selected."""

        consumption_point = (
            self._get_consumption_point(
                dashboard_html
            )
        )

        response = self.session.post(
            f"{BASE}/umbraco/surface/customer2/"
            "SetSelectedConsumption",
            data=consumption_point,
            headers={
                "Referer": f"{BASE}/dashboard/",
                "Origin": BASE,
                "Content-Type": (
                    "application/x-www-form-urlencoded; "
                    "charset=UTF-8"
                ),
            },
            timeout=30,
        )

        response.raise_for_status()

        result = response.text.strip()

        _LOGGER.debug(
            "HTF SetSelectedConsumption returned HTTP %s.",
            response.status_code,
        )

        if result != "OK":
            _LOGGER.debug(
                "HTF did not return OK from "
                "SetSelectedConsumption. Continuing."
            )

    def _extract_consumption(
        self,
        html: str,
    ) -> dict[str, Any]:
        """Extract consumption data from /forbrug/."""

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        node = soup.select_one(
            "#consumption-data-json"
        )

        if not node:
            raise RuntimeError(
                "HTF consumption data was not found "
                "on the /forbrug/ page."
            )

        raw = node.get_text(
            strip=True
        )

        if not raw:
            raise RuntimeError(
                "HTF returned empty consumption data."
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as err:
            raise RuntimeError(
                "HTF returned invalid consumption JSON."
            ) from err

        if not isinstance(data, dict):
            raise RuntimeError(
                "HTF returned an unexpected "
                "consumption data format."
            )

        if not data.get("meters"):
            raise RuntimeError(
                "HTF returned no heating meters."
            )

        return data

    def _extract_javascript_value(
        self,
        html: str,
        variable_name: str,
    ) -> Any:
        """Extract a JSON object/array assigned to a JS variable."""

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        for script in soup.find_all("script"):
            script_text = script.string

            if not script_text:
                script_text = script.get_text()

            if not script_text:
                continue

            marker_position = script_text.find(
                variable_name
            )

            if marker_position == -1:
                continue

            equals_position = script_text.find(
                "=",
                marker_position + len(variable_name),
            )

            if equals_position == -1:
                continue

            value_start = equals_position + 1

            while (
                value_start < len(script_text)
                and script_text[value_start].isspace()
            ):
                value_start += 1

            if value_start >= len(script_text):
                continue

            first_char = script_text[value_start]

            if first_char not in "[{":
                continue

            opening = first_char
            closing = "]" if opening == "[" else "}"

            depth = 0
            in_string = False
            escaped = False

            for index in range(
                value_start,
                len(script_text),
            ):
                char = script_text[index]

                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False

                    continue

                if char == '"':
                    in_string = True
                    continue

                if char == opening:
                    depth += 1

                elif char == closing:
                    depth -= 1

                    if depth == 0:
                        raw = script_text[
                            value_start : index + 1
                        ]

                        try:
                            return json.loads(raw)
                        except json.JSONDecodeError:
                            return None

        return None

    def _find_latest_temperature(
        self,
        data: Any,
    ) -> float | None:
        """Find the latest temperature value."""

        if data is None:
            return None

        if isinstance(data, (int, float)):
            value = float(data)

            if -50 <= value <= 100:
                return value

            return None

        if isinstance(data, list):
            if not data:
                return None

            result = self._find_latest_temperature(
                data[-1]
            )

            if result is not None:
                return result

            for item in reversed(data[:-1]):
                result = self._find_latest_temperature(
                    item
                )

                if result is not None:
                    return result

            return None

        if not isinstance(data, dict):
            return None

        temperature_keys = (
            "returnTemperature",
            "returntemperature",
            "temperature",
            "temp",
            "value",
            "y",
        )

        for key in temperature_keys:
            value = data.get(key)

            if isinstance(
                value,
                (int, float),
            ):
                value = float(value)

                if -50 <= value <= 100:
                    return value

        for key in (
            "data",
            "values",
            "points",
            "temperatures",
            "returnTemperatures",
        ):
            if key in data:
                result = self._find_latest_temperature(
                    data[key]
                )

                if result is not None:
                    return result

        return None

    def _extract_return_temperature(
        self,
        html: str,
    ) -> dict[str, Any]:
        """Extract return temperature information."""

        return_data = self._extract_javascript_value(
            html,
            "returnTemperatureData",
        )

        good_data = self._extract_javascript_value(
            html,
            "goodReturnTemperatureData",
        )

        if return_data is None:
            raise RuntimeError(
                "HTF return temperature data was not found."
            )

        current_temperature = (
            self._find_latest_temperature(
                return_data
            )
        )

        if current_temperature is None:
            raise RuntimeError(
                "HTF return temperature data was found, "
                "but no temperature value could be extracted."
            )

        return {
            "current": current_temperature,
            "data": return_data,
            "good": good_data,
        }

    def _parse_number(
        self,
        text: str,
    ) -> float | None:
        """Parse a Danish number."""

        if not text:
            return None

        cleaned = (
            text.replace("\xa0", " ")
            .replace("DKK", "")
            .replace("kr.", "")
            .replace("kr", "")
            .strip()
        )

        match = re.search(
            r"-?\d[\d\s.,]*",
            cleaned,
        )

        if not match:
            return None

        value = match.group(0).strip()
        value = value.replace(" ", "")

        # Danish format:
        # 1.234,56
        #
        # Convert to:
        # 1234.56
        if "," in value:
            value = value.replace(
                ".",
                "",
            )
            value = value.replace(
                ",",
                ".",
            )
        else:
            # Plain decimal point.
            try:
                return float(value)
            except ValueError:
                return None

        try:
            return float(value)
        except ValueError:
            return None

    def _extract_bills(
        self,
        html: str,
    ) -> dict[str, Any]:
        """Extract bill and account information."""

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        text = soup.get_text(
            " ",
            strip=True,
        )

        lower_text = text.lower()

        balance = None
        latest_bill = None
        due_date = None

        # ---------------------------------------------------------
        # Look for balance-related rows.
        # ---------------------------------------------------------

        for row in soup.select("tr"):
            cells = [
                cell.get_text(
                    " ",
                    strip=True,
                )
                for cell in row.select(
                    "th, td"
                )
            ]

            if not cells:
                continue

            row_text = " ".join(cells)
            row_lower = row_text.lower()

            if (
                balance is None
                and (
                    "saldo" in row_lower
                    or "restsaldo" in row_lower
                    or "skyldig" in row_lower
                    or "til gode" in row_lower
                )
            ):
                for cell in reversed(cells):
                    value = self._parse_number(
                        cell
                    )

                    if value is not None:
                        balance = value
                        break

            if (
                latest_bill is None
                and (
                    "faktura" in row_lower
                    or "regning" in row_lower
                    or "beløb" in row_lower
                    or "beløb i alt" in row_lower
                )
            ):
                for cell in reversed(cells):
                    value = self._parse_number(
                        cell
                    )

                    if value is not None:
                        latest_bill = value
                        break

            if (
                due_date is None
                and (
                    "forfald" in row_lower
                    or "betalingsfrist" in row_lower
                )
            ):
                for cell in cells:
                    date_match = re.search(
                        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
                        cell,
                    )

                    if date_match:
                        due_date = (
                            date_match.group(0)
                        )
                        break

        # ---------------------------------------------------------
        # Fallback: search the complete page for a Danish
        # currency amount if no balance was found in a table.
        # ---------------------------------------------------------

        if balance is None:
            balance_match = re.search(
                r"(?:saldo|restsaldo|skyldig|til gode)"
                r".{0,100}?"
                r"(-?\d[\d\s.,]*)\s*(?:DKK|kr\.?|kr)",
                text,
                re.IGNORECASE,
            )

            if balance_match:
                balance = self._parse_number(
                    balance_match.group(1)
                )

        if latest_bill is None:
            bill_match = re.search(
                r"(?:faktura|regning|beløb)"
                r".{0,100}?"
                r"(\d[\d\s.,]*)\s*(?:DKK|kr\.?|kr)",
                text,
                re.IGNORECASE,
            )

            if bill_match:
                latest_bill = self._parse_number(
                    bill_match.group(1)
                )

        # Keep a compact text snapshot as an attribute.
        # This is useful for debugging without logging
        # the complete authenticated page.
        relevant_lines: list[str] = []

        for line in text.split("  "):
            line = line.strip()

            if not line:
                continue

            line_lower = line.lower()

            if any(
                keyword in line_lower
                for keyword in (
                    "saldo",
                    "faktura",
                    "regning",
                    "forfald",
                    "betalingsfrist",
                    "beløb",
                )
            ):
                relevant_lines.append(line)

        return {
            "balance": balance,
            "latest_bill": latest_bill,
            "due_date": due_date,
            "details": relevant_lines[:20],
        }

    def _extract_bills_safely(
        self,
        html: str,
    ) -> dict[str, Any]:
        """Extract bills without breaking consumption."""

        try:
            return self._extract_bills(html)
        except Exception as err:
            _LOGGER.warning(
                "HTF bill information could not "
                "be extracted: %s",
                err,
            )

            return {
                "balance": None,
                "latest_bill": None,
                "due_date": None,
                "details": [],
            }

    def fetch(self) -> dict[str, Any]:
        """Log in and retrieve HTF data."""

        # ---------------------------------------------------------
        # 1. Login page.
        # ---------------------------------------------------------

        login_page = self.session.get(
            f"{BASE}/login",
            headers={
                "Referer": BASE,
            },
            timeout=30,
        )

        login_page.raise_for_status()

        # ---------------------------------------------------------
        # 2. Login.
        # ---------------------------------------------------------

        login = self.session.post(
            f"{BASE}/umbraco/surface/login2/PostLogin",
            data={
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
            headers={
                "Referer": f"{BASE}/login",
                "Origin": BASE,
                "Content-Type": (
                    "application/x-www-form-urlencoded; "
                    "charset=UTF-8"
                ),
            },
            timeout=30,
            allow_redirects=True,
        )

        login.raise_for_status()

        _LOGGER.debug(
            "HTF login completed: status=%s url=%s",
            login.status_code,
            login.url,
        )

        # ---------------------------------------------------------
        # 3. Dashboard.
        # ---------------------------------------------------------

        dashboard = self.session.get(
            f"{BASE}/dashboard/",
            headers={
                "Referer": f"{BASE}/login",
            },
            timeout=30,
        )

        dashboard.raise_for_status()

        # ---------------------------------------------------------
        # 4. Select consumption point.
        # ---------------------------------------------------------

        try:
            self._select_consumption_point(
                dashboard.text
            )
        except Exception as err:
            _LOGGER.warning(
                "HTF consumption point selection "
                "could not be completed: %s",
                err,
            )

        # ---------------------------------------------------------
        # 5. Consumption.
        # ---------------------------------------------------------

        consumption_page = self.session.get(
            f"{BASE}/forbrug/",
            headers={
                "Referer": f"{BASE}/dashboard/",
            },
            timeout=30,
        )

        consumption_page.raise_for_status()

        consumption = self._extract_consumption(
            consumption_page.text
        )

        # ---------------------------------------------------------
        # 6. Return temperature.
        # ---------------------------------------------------------

        temperature_page = self.session.get(
            f"{BASE}/forbrugsalarm/",
            headers={
                "Referer": f"{BASE}/forbrug/",
            },
            timeout=30,
        )

        temperature_page.raise_for_status()

        try:
            return_temperature = (
                self._extract_return_temperature(
                    temperature_page.text
                )
            )
        except Exception as err:
            _LOGGER.warning(
                "HTF return temperature could not "
                "be extracted: %s",
                err,
            )

            return_temperature = {
                "current": None,
                "data": None,
                "good": None,
            }

        # ---------------------------------------------------------
        # 7. Bills.
        # ---------------------------------------------------------

        bills_page = self.session.get(
            f"{BASE}/kundeoplysninger/kontoudtog/",
            headers={
                "Referer": f"{BASE}/forbrugsalarm/",
            },
            timeout=30,
        )

        bills_page.raise_for_status()

        bills = self._extract_bills_safely(
            bills_page.text
        )

        return {
            "consumption": consumption,
            "return_temperature": return_temperature,
            "bills": bills,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up HTF sensors."""

    client = HTFClient(
        entry.data["customer"],
        entry.data["pin"],
    )

    async def update() -> dict[str, Any]:
        """Fetch HTF data."""

        return await hass.async_add_executor_job(
            client.fetch
        )

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="HTF consumption",
        update_method=update,
        update_interval=SCAN_INTERVAL,
    )

    try:
        await coordinator.async_refresh()
    except Exception as err:
        _LOGGER.error(
            "Unexpected error fetching HTF data: %s",
            err,
        )

    async_add_entities(
        [
            HTFCurrentMonth(coordinator),
            HTFCurrentYear(coordinator),
            HTFReturnTemperature(coordinator),
            HTFBalance(coordinator),
            HTFLatestBill(coordinator),
            HTFLatestBillDueDate(coordinator),
        ]
    )


class HTFBase(CoordinatorEntity):
    """Base HTF sensor."""

    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, "heating")},
        name="HTF Heating",
        manufacturer="Høje Taastrup Fjernvarme",
        model="Selvbetjening",
    )

    @property
    def meter(self) -> dict[str, Any]:
        """Return the first heating meter."""

        consumption = (
            self.coordinator.data or {}
        ).get("consumption", {})

        meters = consumption.get(
            "meters",
            [],
        )

        return meters[0] if meters else {}

    @property
    def bills(self) -> dict[str, Any]:
        """Return bill information."""

        return (
            self.coordinator.data or {}
        ).get(
            "bills",
            {},
        )


class HTFCurrentMonth(
    HTFBase,
    SensorEntity,
):
    """Current month heating consumption."""

    _attr_name = "HTF Heating Current Month"

    _attr_unique_id = (
        "htf_heating_current_month"
    )

    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )

    _attr_device_class = "energy"
    _attr_state_class = "total"
    _attr_icon = "mdi:fire"

    @property
    def native_value(self) -> float:
        """Return current month consumption."""

        now = dt_util.now()

        year_data = (
            self.meter
            .get("monthData", {})
            .get(
                f"year_{now.year}",
                {},
            )
        )

        months = year_data.get(
            "monthNumbers",
            [],
        )

        values = year_data.get(
            "values",
            [],
        )

        if now.month not in months:
            return 0.0

        index = months.index(now.month)

        if index >= len(values):
            return 0.0

        try:
            return float(values[index])
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return monthly data."""

        return {
            "month_data": self.meter.get(
                "monthData",
                {},
            ),
            "meter_info": self.meter.get(
                "meterInfo",
                {},
            ),
        }


class HTFCurrentYear(
    HTFBase,
    SensorEntity,
):
    """Current year heating consumption."""

    _attr_name = "HTF Heating Current Year"

    _attr_unique_id = (
        "htf_heating_current_year"
    )

    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )

    _attr_device_class = "energy"
    _attr_state_class = "total"
    _attr_icon = "mdi:fire"

    @property
    def native_value(self) -> float:
        """Return current year consumption."""

        now = dt_util.now()

        year_data = self.meter.get(
            "yearData",
            {},
        )

        years = year_data.get(
            "yearNumbers",
            [],
        )

        values = year_data.get(
            "values",
            [],
        )

        if now.year not in years:
            return 0.0

        index = years.index(now.year)

        if index >= len(values):
            return 0.0

        try:
            return float(values[index])
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return yearly data."""

        return {
            "year_data": self.meter.get(
                "yearData",
                {},
            ),
            "meter_info": self.meter.get(
                "meterInfo",
                {},
            ),
        }


class HTFReturnTemperature(
    HTFBase,
    SensorEntity,
):
    """Current district heating return temperature."""

    _attr_name = "HTF Return Temperature"

    _attr_unique_id = (
        "htf_return_temperature"
    )

    _attr_native_unit_of_measurement = "°C"

    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer-water"

    @property
    def native_value(self) -> float | None:
        """Return latest return temperature."""

        data = (
            self.coordinator.data or {}
        ).get(
            "return_temperature",
            {},
        )

        value = data.get("current")

        if value is None:
            return None

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return temperature data."""

        data = (
            self.coordinator.data or {}
        ).get(
            "return_temperature",
            {},
        )

        return {
            "good_return_temperature": data.get(
                "good"
            ),
            "return_temperature_data": data.get(
                "data"
            ),
        }


class HTFBalance(
    HTFBase,
    SensorEntity,
):
    """Current HTF account balance."""

    _attr_name = "HTF Balance"

    _attr_unique_id = "htf_balance"

    _attr_native_unit_of_measurement = "DKK"

    _attr_state_class = "measurement"

    _attr_icon = "mdi:cash"

    @property
    def native_value(self) -> float | None:
        """Return current balance."""

        value = self.bills.get("balance")

        if value is None:
            return None

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return bill details."""

        return {
            "latest_bill": self.bills.get(
                "latest_bill"
            ),
            "due_date": self.bills.get(
                "due_date"
            ),
            "details": self.bills.get(
                "details",
                [],
            ),
        }


class HTFLatestBill(
    HTFBase,
    SensorEntity,
):
    """Latest HTF bill."""

    _attr_name = "HTF Latest Bill"

    _attr_unique_id = "htf_latest_bill"

    _attr_native_unit_of_measurement = "DKK"

    _attr_state_class = "measurement"

    _attr_icon = "mdi:receipt-text"

    @property
    def native_value(self) -> float | None:
        """Return latest bill amount."""

        value = self.bills.get(
            "latest_bill"
        )

        if value is None:
            return None

        try:
            return float(value)
        except (
            TypeError,
            ValueError,
        ):
            return None

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return bill details."""

        return {
            "balance": self.bills.get(
                "balance"
            ),
            "due_date": self.bills.get(
                "due_date"
            ),
        }


class HTFLatestBillDueDate(
    HTFBase,
    SensorEntity,
):
    """Latest HTF bill due date."""

    _attr_name = "HTF Latest Bill Due Date"

    _attr_unique_id = (
        "htf_latest_bill_due_date"
    )

    _attr_icon = "mdi:calendar-clock"

    @property
    def native_value(self) -> str | None:
        """Return latest bill due date."""

        return self.bills.get(
            "due_date"
        )