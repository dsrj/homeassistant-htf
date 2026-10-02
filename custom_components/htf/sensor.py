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

    # -------------------------------------------------------------
    # CONSUMPTION POINT
    # -------------------------------------------------------------

    def _get_consumption_point(
        self,
        dashboard_html: str,
    ) -> dict[str, str]:
        """Extract HTF consumption point IDs dynamically."""

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
        """Select the HTF consumption point."""

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

        if response.text.strip() != "OK":
            _LOGGER.debug(
                "HTF SetSelectedConsumption returned "
                "a non-OK response. Continuing."
            )

    # -------------------------------------------------------------
    # CONSUMPTION
    # -------------------------------------------------------------

    def _extract_consumption(
        self,
        html: str,
    ) -> dict[str, Any]:
        """Extract complete consumption dataset."""

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
                "consumption format."
            )

        if not data.get("meters"):
            raise RuntimeError(
                "HTF returned no heating meters."
            )

        return data

    # -------------------------------------------------------------
    # JAVASCRIPT DATA
    # -------------------------------------------------------------

    def _extract_javascript_value(
        self,
        html: str,
        variable_name: str,
    ) -> Any:
        """Extract a JSON object/array assigned to JavaScript."""

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

            marker = script_text.find(
                variable_name
            )

            if marker == -1:
                continue

            equals = script_text.find(
                "=",
                marker + len(variable_name),
            )

            if equals == -1:
                continue

            start = equals + 1

            while (
                start < len(script_text)
                and script_text[start].isspace()
            ):
                start += 1

            if start >= len(script_text):
                continue

            if script_text[start] not in "[{":
                continue

            opening = script_text[start]
            closing = "]" if opening == "[" else "}"

            depth = 0
            in_string = False
            escaped = False

            for index in range(
                start,
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
                            start : index + 1
                        ]

                        try:
                            return json.loads(raw)
                        except json.JSONDecodeError:
                            return None

        return None

    # -------------------------------------------------------------
    # RETURN TEMPERATURE
    # -------------------------------------------------------------

    def _find_latest_temperature(
        self,
        data: Any,
    ) -> float | None:
        """Find the latest temperature value recursively."""

        if data is None:
            return None

        if isinstance(
            data,
            (int, float),
        ):
            value = float(data)

            if -50 <= value <= 100:
                return value

            return None

        if isinstance(data, list):
            for item in reversed(data):
                result = self._find_latest_temperature(
                    item
                )

                if result is not None:
                    return result

            return None

        if not isinstance(data, dict):
            return None

        keys = (
            "returnTemperature",
            "returntemperature",
            "temperature",
            "temp",
            "value",
            "y",
        )

        for key in keys:
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
        """Extract complete return-temperature information."""

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

        current = self._find_latest_temperature(
            return_data
        )

        return {
            "current": current,
            "data": return_data,
            "good": good_data,
        }

    # -------------------------------------------------------------
    # BILLS
    # -------------------------------------------------------------

    def _parse_number(
        self,
        text: str,
    ) -> float | None:
        """Parse Danish-style numbers."""

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

        if "," in value:
            value = value.replace(
                ".",
                "",
            )
            value = value.replace(
                ",",
                ".",
            )

        try:
            return float(value)
        except ValueError:
            return None

    def _extract_bills(
        self,
        html: str,
    ) -> dict[str, Any]:
        """Extract as much bill/account information as possible."""

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        tables: list[list[dict[str, str]]] = []

        for table in soup.find_all("table"):
            rows: list[dict[str, str]] = []

            headers = [
                cell.get_text(
                    " ",
                    strip=True,
                )
                for cell in table.find_all(
                    "th"
                )
            ]

            for row in table.find_all("tr"):
                cells = [
                    cell.get_text(
                        " ",
                        strip=True,
                    )
                    for cell in row.find_all(
                        ["th", "td"]
                    )
                ]

                if not cells:
                    continue

                row_data: dict[str, str] = {}

                if headers and len(headers) == len(cells):
                    for header, value in zip(
                        headers,
                        cells,
                    ):
                        row_data[header] = value
                else:
                    for index, value in enumerate(
                        cells
                    ):
                        row_data[
                            f"column_{index + 1}"
                        ] = value

                rows.append(row_data)

            if rows:
                tables.append(rows)

        page_text = soup.get_text(
            " ",
            strip=True,
        )

        balance = None
        latest_bill = None
        due_date = None

        # Search table rows.
        for table in tables:
            for row in table:
                row_text = " ".join(
                    str(value)
                    for value in row.values()
                )

                lower = row_text.lower()

                if balance is None and any(
                    term in lower
                    for term in (
                        "saldo",
                        "restsaldo",
                        "skyldig",
                        "til gode",
                    )
                ):
                    for value in reversed(
                        list(row.values())
                    ):
                        parsed = self._parse_number(
                            value
                        )

                        if parsed is not None:
                            balance = parsed
                            break

                if latest_bill is None and any(
                    term in lower
                    for term in (
                        "faktura",
                        "regning",
                        "beløb",
                        "total",
                    )
                ):
                    for value in reversed(
                        list(row.values())
                    ):
                        parsed = self._parse_number(
                            value
                        )

                        if parsed is not None:
                            latest_bill = parsed
                            break

                if due_date is None and any(
                    term in lower
                    for term in (
                        "forfald",
                        "betalingsfrist",
                    )
                ):
                    date_match = re.search(
                        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
                        row_text,
                    )

                    if date_match:
                        due_date = (
                            date_match.group(0)
                        )

        # Fallback balance search.
        if balance is None:
            match = re.search(
                r"(?:saldo|restsaldo|skyldig|til gode)"
                r".{0,100}?"
                r"(-?\d[\d\s.,]*)"
                r"\s*(?:DKK|kr\.?|kr)",
                page_text,
                re.IGNORECASE,
            )

            if match:
                balance = self._parse_number(
                    match.group(1)
                )

        # Fallback bill search.
        if latest_bill is None:
            match = re.search(
                r"(?:faktura|regning|beløb)"
                r".{0,100}?"
                r"(\d[\d\s.,]*)"
                r"\s*(?:DKK|kr\.?|kr)",
                page_text,
                re.IGNORECASE,
            )

            if match:
                latest_bill = self._parse_number(
                    match.group(1)
                )

        # Fallback due date.
        if due_date is None:
            match = re.search(
                r"(?:forfald|betalingsfrist)"
                r".{0,100}?"
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                page_text,
                re.IGNORECASE,
            )

            if match:
                due_date = match.group(1)

        return {
            "balance": balance,
            "latest_bill": latest_bill,
            "due_date": due_date,
            "tables": tables,
            "page_text": page_text[:5000],
        }

    # -------------------------------------------------------------
    # MAIN FETCH
    # -------------------------------------------------------------

    def fetch(self) -> dict[str, Any]:
        """Retrieve all available HTF information."""

        # Login page.
        login_page = self.session.get(
            f"{BASE}/login",
            headers={
                "Referer": BASE,
            },
            timeout=30,
        )

        login_page.raise_for_status()

        # Login.
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

        # Dashboard.
        dashboard = self.session.get(
            f"{BASE}/dashboard/",
            headers={
                "Referer": f"{BASE}/login",
            },
            timeout=30,
        )

        dashboard.raise_for_status()

        # Select installation.
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

        # Consumption.
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

        # Return temperature.
        return_temperature_page = self.session.get(
            f"{BASE}/forbrugsalarm/",
            headers={
                "Referer": f"{BASE}/forbrug/",
            },
            timeout=30,
        )

        return_temperature_page.raise_for_status()

        try:
            return_temperature = (
                self._extract_return_temperature(
                    return_temperature_page.text
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

        # Bills.
        bills_page = self.session.get(
            f"{BASE}/kundeoplysninger/kontoudtog/",
            headers={
                "Referer": f"{BASE}/forbrugsalarm/",
            },
            timeout=30,
        )

        bills_page.raise_for_status()

        try:
            bills = self._extract_bills(
                bills_page.text
            )
        except Exception as err:
            _LOGGER.warning(
                "HTF bill information could not "
                "be extracted: %s",
                err,
            )

            bills = {
                "balance": None,
                "latest_bill": None,
                "due_date": None,
                "tables": [],
                "page_text": "",
            }

        return {
            "consumption": consumption,
            "return_temperature": return_temperature,
            "bills": bills,
        }


# -----------------------------------------------------------------
# HOME ASSISTANT
# -----------------------------------------------------------------


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
        name="HTF",
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
            HTFCurrentMonthDailyAverage(coordinator),
            HTFCurrentYear(coordinator),
            HTFReturnTemperature(coordinator),
            HTFBalance(coordinator),
            HTFLatestBill(coordinator),
            HTFLatestBillDueDate(coordinator),
            HTFBillStatus(coordinator),
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
    def consumption(self) -> dict[str, Any]:
        """Return consumption data."""

        return (
            self.coordinator.data or {}
        ).get(
            "consumption",
            {},
        )

    @property
    def meter(self) -> dict[str, Any]:
        """Return first meter."""

        meters = self.consumption.get(
            "meters",
            [],
        )

        return meters[0] if meters else {}

    @property
    def return_temperature_data(
        self,
    ) -> dict[str, Any]:
        """Return temperature data."""

        return (
            self.coordinator.data or {}
        ).get(
            "return_temperature",
            {},
        )

    @property
    def bills(self) -> dict[str, Any]:
        """Return bill data."""

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
    """Current month consumption."""

    _attr_name = "HTF Heating Current Month"
    _attr_unique_id = "htf_heating_current_month"

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

        data = (
            self.meter
            .get("monthData", {})
            .get(
                f"year_{now.year}",
                {},
            )
        )

        months = data.get(
            "monthNumbers",
            [],
        )

        values = data.get(
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
        """Return complete monthly history."""

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


class HTFCurrentMonthDailyAverage(
    HTFBase,
    SensorEntity,
):
    """Estimated daily average for current month."""

    _attr_name = "HTF Heating Daily Average"
    _attr_unique_id = "htf_heating_daily_average"

    _attr_native_unit_of_measurement = "kWh/day"

    _attr_state_class = "measurement"
    _attr_icon = "mdi:chart-timeline-variant"

    @property
    def native_value(self) -> float:
        """Calculate current-month daily average."""

        now = dt_util.now()

        data = (
            self.meter
            .get("monthData", {})
            .get(
                f"year_{now.year}",
                {},
            )
        )

        months = data.get(
            "monthNumbers",
            [],
        )

        values = data.get(
            "values",
            [],
        )

        if now.month not in months:
            return 0.0

        index = months.index(now.month)

        if index >= len(values):
            return 0.0

        try:
            monthly_mwh = float(
                values[index]
            )
        except (
            TypeError,
            ValueError,
        ):
            return 0.0

        days = max(
            now.day,
            1,
        )

        return round(
            monthly_mwh
            * 1000
            / days,
            3,
        )

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return calculation details."""

        now = dt_util.now()

        return {
            "calculation": (
                "Current monthly HTF value divided "
                "by elapsed days."
            ),
            "month": now.month,
            "days_elapsed": now.day,
            "source_is_monthly": True,
            "not_actual_daily_meter_data": True,
        }


class HTFCurrentYear(
    HTFBase,
    SensorEntity,
):
    """Current year consumption."""

    _attr_name = "HTF Heating Current Year"
    _attr_unique_id = "htf_heating_current_year"

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

        data = self.meter.get(
            "yearData",
            {},
        )

        years = data.get(
            "yearNumbers",
            [],
        )

        values = data.get(
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
        """Return complete yearly history."""

        return {
            "year_data": self.meter.get(
                "yearData",
                {},
            ),
            "month_data": self.meter.get(
                "monthData",
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
    """Current return temperature."""

    _attr_name = "HTF Return Temperature"
    _attr_unique_id = "htf_return_temperature"

    _attr_native_unit_of_measurement = "°C"

    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer-water"

    @property
    def native_value(self) -> float | None:
        """Return latest temperature."""

        value = self.return_temperature_data.get(
            "current"
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
        """Return complete temperature history."""

        return {
            "good_return_temperature": (
                self.return_temperature_data.get(
                    "good"
                )
            ),
            "return_temperature_history": (
                self.return_temperature_data.get(
                    "data"
                )
            ),
        }


class HTFBalance(
    HTFBase,
    SensorEntity,
):
    """Current account balance."""

    _attr_name = "HTF Balance"
    _attr_unique_id = "htf_balance"

    _attr_native_unit_of_measurement = "DKK"

    _attr_state_class = "measurement"
    _attr_icon = "mdi:cash"

    @property
    def native_value(self) -> float | None:
        """Return account balance."""

        value = self.bills.get(
            "balance"
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
        """Return bill/account information."""

        return {
            "latest_bill": self.bills.get(
                "latest_bill"
            ),
            "due_date": self.bills.get(
                "due_date"
            ),
            "tables": self.bills.get(
                "tables",
                [],
            ),
        }


class HTFLatestBill(
    HTFBase,
    SensorEntity,
):
    """Latest bill amount."""

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
        """Return bill information."""

        return {
            "balance": self.bills.get(
                "balance"
            ),
            "due_date": self.bills.get(
                "due_date"
            ),
            "tables": self.bills.get(
                "tables",
                [],
            ),
        }


class HTFLatestBillDueDate(
    HTFBase,
    SensorEntity,
):
    """Latest bill due date."""

    _attr_name = "HTF Latest Bill Due Date"
    _attr_unique_id = (
        "htf_latest_bill_due_date"
    )

    _attr_icon = "mdi:calendar-clock"

    @property
    def native_value(self) -> str | None:
        """Return due date."""

        return self.bills.get(
            "due_date"
        )

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return bill information."""

        return {
            "latest_bill": self.bills.get(
                "latest_bill"
            ),
            "balance": self.bills.get(
                "balance"
            ),
        }


class HTFBillStatus(
    HTFBase,
    SensorEntity,
):
    """HTF account/bill status."""

    _attr_name = "HTF Bill Status"
    _attr_unique_id = "htf_bill_status"

    _attr_icon = "mdi:clipboard-check"

    @property
    def native_value(self) -> str:
        """Return a simple account status."""

        balance = self.bills.get(
            "balance"
        )

        if balance is None:
            return "Unknown"

        try:
            balance_value = float(balance)
        except (
            TypeError,
            ValueError,
        ):
            return "Unknown"

        if balance_value > 0:
            return "Amount Due"

        if balance_value < 0:
            return "Credit"

        return "Settled"

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return complete account status information."""

        return {
            "balance": self.bills.get(
                "balance"
            ),
            "latest_bill": self.bills.get(
                "latest_bill"
            ),
            "due_date": self.bills.get(
                "due_date"
            ),
            "bill_tables": self.bills.get(
                "tables",
                [],
            ),
        }