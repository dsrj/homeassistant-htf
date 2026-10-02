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
SCAN_INTERVAL = timedelta(hours=24)

_LOGGER = logging.getLogger(__name__)


def _clean_number(value: Any) -> float | None:
    """Convert a value to a float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("Â ", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _find_first_number(value: Any) -> float | None:
    """Find the first numeric value recursively."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, dict):
        preferred = (
            "value",
            "consumption",
            "amount",
            "mwh",
            "kwh",
            "energy",
            "usage",
            "forbrug",
            "temperature",
            "returnTemperature",
            "return_temperature",
        )
        for key in preferred:
            if key in value:
                result = _find_first_number(value[key])
                if result is not None:
                    return result

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


def _get_meter(consumption: dict[str, Any]) -> dict[str, Any]:
    """Return the first HTF meter."""
    meters = consumption.get("meters")
    if isinstance(meters, list) and meters:
        first_meter = meters[0]
        if isinstance(first_meter, dict):
            return first_meter
    return {}


def _get_year_data(consumption: dict[str, Any]) -> Any:
    """Return yearly consumption data."""
    return _get_meter(consumption).get("yearData", [])


def _get_month_data(consumption: dict[str, Any]) -> Any:
    """Return monthly consumption data."""
    return _get_meter(consumption).get("monthData", [])


def _get_meter_info(consumption: dict[str, Any]) -> Any:
    """Return meter information."""
    return _get_meter(consumption).get("meterInfo", [])


def _item_matches_current_month(item: Any) -> bool:
    """Check whether an item represents the current month."""
    now = dt_util.now()
    text = json.dumps(item, ensure_ascii=False).lower()

    month_names = (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    )
    danish_month_names = (
        "januar", "februar", "marts", "april", "maj", "juni",
        "juli", "august", "september", "oktober", "november", "december",
    )

    if str(now.year) in text and (
        f'"month": {now.month}' in text
        or f'"month":{now.month}' in text
        or f'"monthnumber": {now.month}' in text
        or f'"monthnumber":{now.month}' in text
        or f'"monthindex": {now.month}' in text
        or f'"monthindex":{now.month}' in text
        or f'"month": "{now.month}"' in text
        or f'"month":"{now.month}"' in text
    ):
        return True

    return (
        month_names[now.month - 1] in text
        or danish_month_names[now.month - 1] in text
    ) and str(now.year) in text


def _extract_value_from_record(record: Any) -> float | None:
    """Extract a likely consumption value from one record."""
    if not isinstance(record, dict):
        return _clean_number(record)

    preferred_keys = (
        "value", "consumption", "amount", "mwh", "MWh",
        "kwh", "kWh", "energy", "usage", "forbrug",
    )

    for key in preferred_keys:
        if key in record:
            value = _find_first_number(record[key])
            if value is not None:
                return value

    for key, value in record.items():
        key_text = str(key).lower()
        if any(
            ignored in key_text
            for ignored in ("year", "month", "date", "day", "index", "id")
        ):
            continue

        number = _find_first_number(value)
        if number is not None:
            return number

    return None


def _extract_current_month(
    consumption: dict[str, Any],
) -> float | None:
    """Extract current-month consumption in MWh."""
    month_data = _get_month_data(consumption)

    _LOGGER.debug(
        "HTF parser: monthData type=%s",
        type(month_data).__name__,
    )

    if isinstance(month_data, list):
        for item in month_data:
            if _item_matches_current_month(item):
                value = _extract_value_from_record(item)
                if value is not None:
                    return value

        for item in reversed(month_data):
            value = _extract_value_from_record(item)
            if value is not None:
                return value

    elif isinstance(month_data, dict):
        now = dt_util.now()
        candidates = (
            str(now.month),
            f"{now.year}-{now.month:02d}",
            f"{now.year}-{now.month}",
            now.strftime("%Y-%m"),
            now.strftime("%B").lower(),
        )

        for key in candidates:
            if key in month_data:
                value = _extract_value_from_record(month_data[key])
                if value is not None:
                    return value

        value = _extract_value_from_record(month_data)
        if value is not None:
            return value

    return None


def _extract_current_year(
    consumption: dict[str, Any],
) -> float | None:
    """Extract current-year consumption in MWh."""
    year_data = _get_year_data(consumption)

    _LOGGER.debug(
        "HTF parser: yearData type=%s",
        type(year_data).__name__,
    )

    now = dt_util.now()

    if isinstance(year_data, list):
        for item in year_data:
            text = json.dumps(item, ensure_ascii=False).lower()
            if str(now.year) in text:
                value = _extract_value_from_record(item)
                if value is not None:
                    return value

        for item in reversed(year_data):
            value = _extract_value_from_record(item)
            if value is not None:
                return value

    elif isinstance(year_data, dict):
        for key in (str(now.year), now.strftime("%Y")):
            if key in year_data:
                value = _extract_value_from_record(year_data[key])
                if value is not None:
                    return value

        value = _extract_value_from_record(year_data)
        if value is not None:
            return value

    return None


def _log_json(label: str, value: Any) -> None:
    """Write JSON to the debug log for diagnostics."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except (TypeError, ValueError):
        encoded = repr(value)

    _LOGGER.debug(
        "HTF DIAGNOSTIC JSON BEGIN: %s\n%s\n"
        "HTF DIAGNOSTIC JSON END: %s",
        label,
        encoded,
        label,
    )


class HTFClient:
    """Blocking HTTP client for HTF."""

    def __init__(self, customer: str, pin: str) -> None:
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
                ),
            }
        )

    def _login(self) -> None:
        """Log in to HTF."""
        _LOGGER.debug("HTF: opening login page")

        response = self.session.get(
            f"{BASE}/login",
            timeout=30,
        )
        response.raise_for_status()

        _LOGGER.debug("HTF: submitting login")

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

        _LOGGER.debug("HTF: opening dashboard")

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
        _LOGGER.debug("HTF: login successful")

    def _select_consumption_point(self) -> None:
        """Select the consumption point dynamically."""
        _LOGGER.debug("HTF: finding consumption point")

        soup = BeautifulSoup(
            self.dashboard_html,
            "html.parser",
        )
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

        _LOGGER.debug("HTF: consumption point found")

        response = self.session.post(
            f"{BASE}/umbraco/surface/customer2/SetSelectedConsumption",
            data={
                "ConsumptionPointId": parts[0],
                "ConsumerId": parts[1],
                "DebtorId": parts[2],
                "CustomerId": parts[3],
            },
            timeout=30,
        )
        response.raise_for_status()

        if response.text.strip() != "OK":
            _LOGGER.debug(
                "HTF: SetSelectedConsumption returned %r; continuing",
                response.text[:200],
            )
        else:
            _LOGGER.debug(
                "HTF: consumption point selected"
            )

    def _get_page(self, path: str) -> str:
        """Get an authenticated HTF page."""
        _LOGGER.debug("HTF: requesting %s", path)

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
        """Extract JSON from an HTML element."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )
        element = soup.select_one(f"#{element_id}")

        if element is None:
            return None

        text = element.get_text(strip=True)
        if not text:
            return None

        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            _LOGGER.debug(
                "HTF: invalid JSON in #%s",
                element_id,
            )
            return None

        return result if isinstance(result, dict) else None

    @staticmethod
    def _extract_js_variable(
        html: str,
        variable_name: str,
    ) -> Any:
        """Extract a JavaScript variable."""
        patterns = [
            rf"{re.escape(variable_name)}\s*=\s*(\[[\s\S]*?\])\s*;",
            rf"{re.escape(variable_name)}\s*=\s*(\{{[\s\S]*?\}})\s*;",
        ]

        for pattern in patterns:
            match = re.search(pattern, html)
            if not match:
                continue

            raw = match.group(1).strip()

            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                try:
                    return json.loads(raw.replace("'", '"'))
                except json.JSONDecodeError:
                    _LOGGER.debug(
                        "HTF: could not parse JavaScript variable %s",
                        variable_name,
                    )

        return None

    @staticmethod
    def _extract_return_temperature(
        html: str,
    ) -> dict[str, Any]:
        """Extract return temperature data."""
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
        """Extract bill information."""
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

        for table in tables:
            for row in table:
                row_text = " ".join(row).lower()

                if (
                    balance is None
                    and (
                        "saldo" in row_text
                        or "balance" in row_text
                        or "indestÃ¥ende" in row_text
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
                        or "belÃ¸b" in row_text
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
                        r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}",
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
        """Fetch all HTF information."""
        _LOGGER.debug("HTF: starting data update")

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

        meters = consumption.get("meters")
        if not isinstance(meters, list) or not meters:
            raise UpdateFailed(
                "HTF returned consumption data without any meters."
            )

        _LOGGER.debug("HTF: consumption data received")

        # DIAGNOSTIC MODE:
        # Logs the exact HTF JSON so we can see its real structure.
        # It does not log the login PIN or session cookies.
        # The JSON may still contain account/meter identifiers, so do not
        # publish the resulting HA log publicly.
        _log_json(
            "consumption-data-json",
            consumption,
        )

        meter = _get_meter(consumption)
        _LOGGER.debug(
            "HTF DIAGNOSTIC: meter keys=%s",
            sorted(meter.keys()),
        )

        _log_json("monthData", meter.get("monthData"))
        _log_json("yearData", meter.get("yearData"))
        _log_json("meterInfo", meter.get("meterInfo"))

        current_month = _extract_current_month(consumption)
        current_year = _extract_current_year(consumption)

        _LOGGER.debug(
            "HTF DIAGNOSTIC: extracted current month=%r MWh",
            current_month,
        )
        _LOGGER.debug(
            "HTF DIAGNOSTIC: extracted current year=%r MWh",
            current_year,
        )

        return_temperature_html = self._get_page(
            "/forbrugsalarm/"
        )
        return_temperature = self._extract_return_temperature(
            return_temperature_html
        )

        _LOGGER.debug(
            "HTF: return temperature page received"
        )
        _log_json(
            "return-temperature",
            return_temperature,
        )

        bills_html = self._get_page(
            "/kundeoplysninger/kontoudtog/"
        )
        bills = self._extract_bill_data(bills_html)

        _LOGGER.debug("HTF: account page received")
        _log_json("bills", bills)

        _LOGGER.info("HTF: data update successful")

        return {
            "consumption": consumption,
            "return_temperature": return_temperature,
            "bills": bills,
            "last_update": dt_util.utcnow().isoformat(),
        }


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
        """Initialize coordinator."""
        self.client = HTFClient(customer, pin)

        super().__init__(
            hass,
            _LOGGER,
            name="HTF Selvbetjening",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(
        self,
    ) -> dict[str, Any]:
        """Fetch HTF data in an executor."""
        try:
            return await self.hass.async_add_executor_job(
                self.client.fetch
            )
        except UpdateFailed:
            raise
        except Exception as err:
            _LOGGER.exception(
                "HTF: unexpected error while fetching data"
            )
            raise UpdateFailed(
                f"Unable to fetch HTF data: {err}"
            ) from err


async def _async_first_refresh(
    coordinator: HTFCoordinator,
) -> None:
    """Run first refresh in the background."""
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

    hass.data.setdefault(
        DOMAIN,
        {},
    )[entry.entry_id] = coordinator

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

    hass.async_create_task(
        _async_first_refresh(coordinator)
    )


class HTFBaseSensor(
    CoordinatorEntity[HTFCoordinator],
    SensorEntity,
):
    """Base HTF sensor."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, "heating")},
            name="HTF Heating",
            manufacturer="HÃ¸je Taastrup Fjernvarme",
            configuration_url=BASE,
        )

    @property
    def available(self) -> bool:
        """Return sensor availability."""
        return (
            super().available
            and self.coordinator.data is not None
        )

    def _consumption(self) -> dict[str, Any]:
        """Return consumption data."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get(
            "consumption",
            {},
        )

    def _return_temperature(self) -> dict[str, Any]:
        """Return return temperature data."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get(
            "return_temperature",
            {},
        )

    def _bills(self) -> dict[str, Any]:
        """Return bill data."""
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get(
            "bills",
            {},
        )


class HTFCurrentMonth(HTFBaseSensor):
    """Current month heating consumption."""

    _attr_name = "HTF Heating Current Month"
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_device_class = "energy"
    _attr_state_class = "total"
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_heating_current_month"
        )

    @property
    def native_value(self) -> float | None:
        """Return current month consumption."""
        return _extract_current_month(
            self._consumption()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return monthly history."""
        consumption = self._consumption()
        return {
            "month_data": _get_month_data(consumption),
            "meter_info": _get_meter_info(consumption),
            "last_update": (
                self.coordinator.data.get("last_update")
                if self.coordinator.data
                else None
            ),
        }


class HTFCurrentMonthDailyAverage(HTFBaseSensor):
    """Estimated daily average."""

    _attr_name = "HTF Heating Daily Average"
    _attr_native_unit_of_measurement = "kWh/day"
    _attr_icon = "mdi:chart-line"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_heating_daily_average"
        )

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

        return round(
            (monthly * 1000) / day,
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain the estimate."""
        return {
            "calculation": (
                "Current month total / elapsed calendar days"
            ),
            "is_estimate": True,
            "note": (
                "HTF provides monthly consumption, "
                "not actual daily meter readings."
            ),
        }


class HTFCurrentYear(HTFBaseSensor):
    """Current year heating consumption."""

    _attr_name = "HTF Heating Current Year"
    _attr_native_unit_of_measurement = (
        UnitOfEnergy.MEGA_WATT_HOUR
    )
    _attr_device_class = "energy"
    _attr_state_class = "total"
    _attr_icon = "mdi:fire"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_heating_current_year"
        )

    @property
    def native_value(self) -> float | None:
        """Return current year consumption."""
        return _extract_current_year(
            self._consumption()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return yearly and monthly history."""
        consumption = self._consumption()
        return {
            "year_data": _get_year_data(consumption),
            "month_data": _get_month_data(consumption),
            "meter_info": _get_meter_info(consumption),
            "last_update": (
                self.coordinator.data.get("last_update")
                if self.coordinator.data
                else None
            ),
        }


class HTFReturnTemperature(HTFBaseSensor):
    """Return temperature."""

    _attr_name = "HTF Return Temperature"
    _attr_native_unit_of_measurement = "Â°C"
    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_return_temperature"
        )

    @property
    def native_value(self) -> float | None:
        """Return latest return temperature."""
        data = self._return_temperature()

        good = data.get(
            "goodReturnTemperatureData"
        )
        if isinstance(good, list) and good:
            value = _find_first_number(good[-1])
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
        """Return temperature history."""
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
        """Initialize sensor."""
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
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = "htf_latest_bill"

    @property
    def native_value(self) -> float | None:
        """Return latest bill amount."""
        return self._bills().get("latest_bill")


class HTFLatestBillDueDate(HTFBaseSensor):
    """Latest bill due date."""

    _attr_name = "HTF Latest Bill Due Date"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_latest_bill_due_date"
        )

    @property
    def native_value(self) -> str | None:
        """Return latest bill due date."""
        return self._bills().get("due_date")


class HTFBillStatus(HTFBaseSensor):
    """Bill/account status."""

    _attr_name = "HTF Bill Status"
    _attr_icon = "mdi:cash-check"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = "htf_bill_status"

    @property
    def native_value(self) -> str:
        """Return account status."""
        balance = self._bills().get("balance")

        if balance is None:
            return "Unknown"
        if balance > 0.01:
            return "Amount Due"
        if balance < -0.01:
            return "Credit"
        return "Settled"
