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
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

DOMAIN = "htf"
BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=6)

_LOGGER = logging.getLogger(__name__)


def _clean_number(value: Any) -> float | None:
    """Convert a value to float."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    # Handle Danish number formatting.
    text = text.replace(" ", "")
    text = text.replace(".", "") if "," in text else text
    text = text.replace(",", ".")

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _find_first_number(value: Any) -> float | None:
    """Find the first numeric value recursively."""
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, dict):
        for item in value.values():
            result = _find_first_number(item)
            if result is not None:
                return result

    if isinstance(value, list):
        for item in value:
            result = _find_first_number(item)
            if result is not None:
                return result

    return _clean_number(value)


class HTFClient:
    """Blocking HTF HTTP client."""

    def __init__(self, customer: str, pin: str) -> None:
        self.customer = customer
        self.pin = pin
        self.session = requests.Session()

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Home Assistant HTF integration)"
                ),
            }
        )

    def _login(self) -> None:
        """Log in to HTF."""
        login_page = self.session.get(
            f"{BASE}/login",
            timeout=30,
        )
        login_page.raise_for_status()

        response = self.session.post(
            f"{BASE}/umbraco/surface/login2/PostLogin",
            data={
                "kundenr": self.customer,
                "pinkode": self.pin,
            },
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded; charset=UTF-8"
                ),
            },
            timeout=30,
        )
        response.raise_for_status()

        # The login endpoint can return OK, JSON, HTML or a redirect.
        # Verify that the session actually reaches the dashboard.
        dashboard = self.session.get(
            f"{BASE}/dashboard/",
            timeout=30,
        )
        dashboard.raise_for_status()

        if (
            "subheader-consumptionpoint-dropdown" not in dashboard.text
            and "/login" in dashboard.url.lower()
        ):
            raise UpdateFailed("HTF login was not accepted.")

        self.dashboard_html = dashboard.text

    def _select_consumption_point(self) -> None:
        """Select the consumption point dynamically from the dashboard."""
        soup = BeautifulSoup(self.dashboard_html, "html.parser")

        dropdown = soup.select_one(
            "select.subheader-consumptionpoint-dropdown"
        )

        if dropdown is None:
            raise UpdateFailed(
                "HTF consumption point dropdown was not found."
            )

        option = dropdown.find("option", selected=True)

        if option is None:
            option = dropdown.find("option")

        if option is None:
            raise UpdateFailed(
                "HTF consumption point option was not found."
            )

        value = option.get("value")

        if not value:
            raise UpdateFailed(
                "HTF consumption point value was empty."
            )

        parts = [part.strip() for part in value.split(";")]

        if len(parts) < 4:
            raise UpdateFailed(
                "HTF returned an unexpected consumption point format."
            )

        consumption_point_id = parts[0]
        consumer_id = parts[1]
        debtor_id = parts[2]
        customer_id = parts[3]

        response = self.session.post(
            f"{BASE}/umbraco/surface/customer2/SetSelectedConsumption",
            data={
                "ConsumptionPointId": consumption_point_id,
                "ConsumerId": consumer_id,
                "DebtorId": debtor_id,
                "CustomerId": customer_id,
            },
            timeout=30,
        )

        response.raise_for_status()

        # HTF has been observed to return something other than literal
        # "OK" even though the following pages work correctly.
        if response.text.strip() != "OK":
            _LOGGER.debug(
                "HTF SetSelectedConsumption response was %r; "
                "continuing with the selected consumption point.",
                response.text[:200],
            )

    def _get_page(self, path: str) -> str:
        """Get an authenticated HTF page."""
        response = self.session.get(
            f"{BASE}{path}",
            timeout=30,
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _extract_json_block(
        html: str,
        element_id: str,
    ) -> dict[str, Any] | None:
        """Extract JSON from a hidden HTML element."""
        soup = BeautifulSoup(html, "html.parser")
        element = soup.select_one(f"#{element_id}")

        if element is None:
            return None

        text = element.get_text(strip=True)

        if not text:
            return None

        try:
            result = json.loads(text)

            if isinstance(result, dict):
                return result

        except json.JSONDecodeError:
            _LOGGER.debug(
                "Could not decode JSON from #%s",
                element_id,
            )

        return None

    @staticmethod
    def _extract_js_variable(
        html: str,
        variable_name: str,
    ) -> Any:
        """Extract a JSON-like JavaScript variable."""
        patterns = [
            rf"\b{re.escape(variable_name)}\s*=\s*(\[[\s\S]*?\])\s*;",
            rf"\b{re.escape(variable_name)}\s*=\s*(\{{[\s\S]*?\}})\s*;",
        ]

        for pattern in patterns:
            match = re.search(pattern, html)

            if not match:
                continue

            raw = match.group(1).strip()

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # Try JavaScript single quotes.
                try:
                    converted = raw.replace("'", '"')
                    return json.loads(converted)
                except json.JSONDecodeError:
                    _LOGGER.debug(
                        "Could not parse JS variable %s",
                        variable_name,
                    )

        return None

    @staticmethod
    def _extract_return_temperature(html: str) -> dict[str, Any]:
        """Extract return-temperature information."""
        data: dict[str, Any] = {}

        for name in (
            "returnTemperatureData",
            "goodReturnTemperatureData",
        ):
            value = HTFClient._extract_js_variable(html, name)

            if value is not None:
                data[name] = value

        return data

    @staticmethod
    def _extract_bill_data(html: str) -> dict[str, Any]:
        """Extract available account/bill information."""
        soup = BeautifulSoup(html, "html.parser")

        tables: list[list[list[str]]] = []

        for table in soup.find_all("table"):
            rows: list[list[str]] = []

            for row in table.find_all("tr"):
                cells = [
                    cell.get_text(" ", strip=True)
                    for cell in row.find_all(["th", "td"])
                ]

                if cells:
                    rows.append(cells)

            if rows:
                tables.append(rows)

        text = soup.get_text(" ", strip=True)

        balance: float | None = None
        latest_bill: float | None = None
        due_date: str | None = None

        # Search table rows first.
        for table in tables:
            for row in table:
                row_text = " ".join(row).lower()

                if (
                    balance is None
                    and (
                        "saldo" in row_text
                        or "balance" in row_text
                        or "indestående" in row_text
                    )
                ):
                    for cell in reversed(row):
                        number = _clean_number(cell)
                        if number is not None:
                            balance = number
                            break

                if (
                    latest_bill is None
                    and (
                        "faktura" in row_text
                        or "beløb" in row_text
                        or "amount" in row_text
                    )
                ):
                    for cell in reversed(row):
                        number = _clean_number(cell)
                        if number is not None:
                            latest_bill = number
                            break

                if (
                    due_date is None
                    and (
                        "forfald" in row_text
                        or "due date" in row_text
                    )
                ):
                    date_match = re.search(
                        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
                        " ".join(row),
                    )

                    if date_match:
                        due_date = date_match.group(0)

        return {
            "balance": balance,
            "latest_bill": latest_bill,
            "due_date": due_date,
            "tables": tables,
            "text_length": len(text),
        }

    def fetch(self) -> dict[str, Any]:
        """Fetch all HTF data."""
        self._login()
        self._select_consumption_point()

        consumption_html = self._get_page("/forbrug/")
        consumption = self._extract_json_block(
            consumption_html,
            "consumption-data-json",
        )

        if not consumption:
            raise UpdateFailed(
                "HTF consumption data was not found."
            )

        return_temperature_html = self._get_page(
            "/forbrugsalarm/"
        )

        return_temperature = self._extract_return_temperature(
            return_temperature_html
        )

        bills_html = self._get_page(
            "/kundeoplysninger/kontoudtog/"
        )

        bills = self._extract_bill_data(bills_html)

        return {
            "consumption": consumption,
            "return_temperature": return_temperature,
            "bills": bills,
            "last_update": dt_util.utcnow().isoformat(),
        }


class HTFCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate HTF updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        customer: str,
        pin: str,
    ) -> None:
        self.client = HTFClient(customer, pin)

        super().__init__(
            hass,
            _LOGGER,
            name="HTF Selvbetjening",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data without blocking Home Assistant."""
        try:
            return await self.hass.async_add_executor_job(
                self.client.fetch
            )
        except Exception as err:
            raise UpdateFailed(
                f"Unable to fetch HTF data: {err}"
            ) from err


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up HTF sensors."""
    coordinator = HTFCoordinator(
        hass,
        entry.data["customer"],
        entry.data["pin"],
    )

    # Important:
    # Do NOT await the first refresh here.
    #
    # This means Home Assistant can finish setting up the integration
    # immediately instead of waiting for HTF's website.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entities = [
        HTFCurrentMonth(coordinator),
        HTFCurrentMonthDailyAverage(coordinator),
        HTFCurrentYear(coordinator),
        HTFReturnTemperature(coordinator),
        HTFBalance(coordinator),
        HTFLatestBill(coordinator),
        HTFLatestBillDueDate(coordinator),
        HTFBillStatus(coordinator),
    ]

    async_add_entities(entities)

    # Start the first refresh in the background.
    coordinator.async_refresh()


class HTFBaseSensor(CoordinatorEntity[HTFCoordinator], SensorEntity):
    """Base HTF sensor."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, "htf")},
            name="HTF Selvbetjening",
            manufacturer="Høje Taastrup Fjernvarme",
            configuration_url=BASE,
        )

    @property
    def available(self) -> bool:
        """Return whether data is available."""
        return (
            super().available
            and self.coordinator.data is not None
        )

    def _consumption(self) -> dict[str, Any]:
        """Return consumption data."""
        return self.coordinator.data.get("consumption", {})

    def _return_temperature(self) -> dict[str, Any]:
        """Return return-temperature data."""
        return self.coordinator.data.get(
            "return_temperature",
            {},
        )

    def _bills(self) -> dict[str, Any]:
        """Return bill data."""
        return self.coordinator.data.get("bills", {})


def _get_year_data(
    consumption: dict[str, Any],
) -> Any:
    """Return year data."""
    return consumption.get("yearData", [])


def _get_month_data(
    consumption: dict[str, Any],
) -> Any:
    """Return month data."""
    return consumption.get("monthData", [])


def _extract_current_month(
    consumption: dict[str, Any],
) -> float | None:
    """Extract current-month consumption in MWh."""
    month_data = _get_month_data(consumption)

    if not isinstance(month_data, list):
        return None

    now = dt_util.now()
    current_month = now.month
    current_year = now.year

    for item in month_data:
        if not isinstance(item, dict):
            continue

        text = json.dumps(
            item,
            ensure_ascii=False,
        ).lower()

        if (
            str(current_month) in text
            and str(current_year) in text
        ):
            value = _find_first_number(
                item.get("value")
                or item.get("consumption")
                or item.get("amount")
                or item.get("mwh")
            )

            if value is not None:
                return value

    # If HTF doesn't include explicit year/month fields,
    # use the last numeric data point.
    for item in reversed(month_data):
        if isinstance(item, dict):
            value = _find_first_number(
                item.get("value")
                or item.get("consumption")
                or item.get("amount")
                or item.get("mwh")
            )

            if value is not None:
                return value

    return None


def _extract_current_year(
    consumption: dict[str, Any],
) -> float | None:
    """Extract current-year consumption in MWh."""
    year_data = _get_year_data(consumption)

    if isinstance(year_data, list):
        now = dt_util.now()

        for item in year_data:
            if not isinstance(item, dict):
                continue

            text = json.dumps(
                item,
                ensure_ascii=False,
            ).lower()

            if str(now.year) in text:
                value = _find_first_number(
                    item.get("value")
                    or item.get("consumption")
                    or item.get("amount")
                    or item.get("mwh")
                )

                if value is not None:
                    return value

        for item in reversed(year_data):
            if isinstance(item, dict):
                value = _find_first_number(
                    item.get("value")
                    or item.get("consumption")
                    or item.get("amount")
                    or item.get("mwh")
                )

                if value is not None:
                    return value

    return None


class HTFCurrentMonth(HTFBaseSensor):
    """Current-month heating consumption."""

    _attr_name = "HTF Heating Current Month"
    _attr_native_unit_of_measurement = UnitOfEnergy.MEGA_WATT_HOUR
    _attr_device_class = "energy"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_heating_current_month"

    @property
    def native_value(self) -> float | None:
        """Return current-month consumption."""
        return _extract_current_month(self._consumption())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return historical consumption data."""
        consumption = self._consumption()

        return {
            "month_data": _get_month_data(consumption),
            "meter_info": consumption.get("meterInfo", []),
            "last_update": self.coordinator.data.get(
                "last_update"
            ),
        }


class HTFCurrentMonthDailyAverage(HTFBaseSensor):
    """Estimated daily average for the current month."""

    _attr_name = "HTF Heating Daily Average"
    _attr_native_unit_of_measurement = "kWh/day"
    _attr_icon = "mdi:chart-line"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_heating_daily_average"

    @property
    def native_value(self) -> float | None:
        """Return estimated daily average."""
        monthly = _extract_current_month(
            self._consumption()
        )

        if monthly is None:
            return None

        day = dt_util.now().day

        if day <= 0:
            return None

        # MWh -> kWh and divide by elapsed calendar days.
        return round((monthly * 1000) / day, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain the estimate."""
        return {
            "calculation": (
                "Current month total / elapsed calendar days"
            ),
            "is_estimate": True,
            "note": (
                "HTF currently provides monthly consumption, "
                "not actual daily meter readings."
            ),
        }


class HTFCurrentYear(HTFBaseSensor):
    """Current-year heating consumption."""

    _attr_name = "HTF Heating Current Year"
    _attr_native_unit_of_measurement = UnitOfEnergy.MEGA_WATT_HOUR
    _attr_device_class = "energy"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_heating_current_year"

    @property
    def native_value(self) -> float | None:
        """Return current-year consumption."""
        return _extract_current_year(
            self._consumption()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return historical yearly/monthly data."""
        consumption = self._consumption()

        return {
            "year_data": _get_year_data(consumption),
            "month_data": _get_month_data(consumption),
            "meter_info": consumption.get("meterInfo", []),
            "last_update": self.coordinator.data.get(
                "last_update"
            ),
        }


class HTFReturnTemperature(HTFBaseSensor):
    """Return temperature."""

    _attr_name = "HTF Return Temperature"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_return_temperature"

    @property
    def native_value(self) -> float | None:
        """Return latest return temperature."""
        data = self._return_temperature()

        good = data.get(
            "goodReturnTemperatureData"
        )

        if isinstance(good, list) and good:
            latest = good[-1]
            value = _find_first_number(latest)

            if value is not None:
                return round(value, 2)

        raw = data.get("returnTemperatureData")

        if isinstance(raw, list) and raw:
            value = _find_first_number(raw[-1])

            if value is not None:
                return round(value, 2)

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return historical return-temperature data."""
        data = self._return_temperature()

        return {
            "good_return_temperature": data.get(
                "goodReturnTemperatureData",
                [],
            ),
            "return_temperature_history": data.get(
                "returnTemperatureData",
                [],
            ),
        }


class HTFBalance(HTFBaseSensor):
    """Account balance."""

    _attr_name = "HTF Balance"
    _attr_native_unit_of_measurement = "DKK"
    _attr_icon = "mdi:bank"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_balance"

    @property
    def native_value(self) -> float | None:
        """Return account balance."""
        return self._bills().get("balance")


class HTFLatestBill(HTFBaseSensor):
    """Latest bill amount."""

    _attr_name = "HTF Latest Bill"
    _attr_native_unit_of_measurement = "DKK"
    _attr_icon = "mdi:receipt"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_latest_bill"

    @property
    def native_value(self) -> float | None:
        """Return latest bill."""
        return self._bills().get("latest_bill")


class HTFLatestBillDueDate(HTFBaseSensor):
    """Latest bill due date."""

    _attr_name = "HTF Latest Bill Due Date"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_latest_bill_due_date"

    @property
    def native_value(self) -> str | None:
        """Return due date."""
        return self._bills().get("due_date")


class HTFBillStatus(HTFBaseSensor):
    """Bill/account status."""

    _attr_name = "HTF Bill Status"
    _attr_icon = "mdi:cash-check"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = "htf_bill_status"

    @property
    def native_value(self) -> str:
        """Return bill status."""
        balance = self._bills().get("balance")

        if balance is None:
            return "Unknown"

        if balance > 0.01:
            return "Amount Due"

        if balance < -0.01:
            return "Credit"

        return "Settled"