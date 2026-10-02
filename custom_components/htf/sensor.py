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

from .return_temperature import ReturnTemperatureCoordinator

DOMAIN = "htf"
BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=24)

_LOGGER = logging.getLogger(__name__)


def _number(value: Any) -> float | None:
    """Convert a value to a number."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace("\xa0", "").replace(" ", "")

    if not text:
        return None

    if "," in text:
        text = text.replace(".", "").replace(",", ".")

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _get_meter(consumption: dict[str, Any]) -> dict[str, Any]:
    """Get the first meter."""
    meters = consumption.get("meters")

    if isinstance(meters, list) and meters:
        if isinstance(meters[0], dict):
            return meters[0]

    return {}


def _get_month_data(consumption: dict[str, Any]) -> dict[str, Any]:
    """Get HTF month data."""
    data = _get_meter(consumption).get("monthData")
    return data if isinstance(data, dict) else {}


def _get_year_data(consumption: dict[str, Any]) -> dict[str, Any]:
    """Get HTF year data."""
    data = _get_meter(consumption).get("yearData")
    return data if isinstance(data, dict) else {}


def _get_meter_info(consumption: dict[str, Any]) -> dict[str, Any]:
    """Get meter information."""
    data = _get_meter(consumption).get("meterInfo")
    return data if isinstance(data, dict) else {}


def _current_month(consumption: dict[str, Any]) -> float | None:
    """Get current month consumption in MWh."""
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

    if values:
        return _number(values[-1])

    return None


def _current_year(consumption: dict[str, Any]) -> float | None:
    """Get current year consumption in MWh."""
    data = _get_year_data(consumption)
    now = dt_util.now()

    years = data.get("yearNumbers", [])
    values = data.get("values", [])

    if not isinstance(years, list):
        return None

    if not isinstance(values, list):
        return None

    for index, year in enumerate(years):
        if year == now.year and index < len(values):
            return _number(values[index])

    return None


def _find_number(value: Any) -> float | None:
    """Find a numeric value recursively."""
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, dict):
        preferred = (
            "value",
            "temperature",
            "returnTemperature",
            "return_temperature",
            "temp",
        )

        for key in preferred:
            if key in value:
                result = _find_number(value[key])
                if result is not None:
                    return result

        for item in value.values():
            result = _find_number(item)
            if result is not None:
                return result

    if isinstance(value, list):
        for item in reversed(value):
            result = _find_number(item)
            if result is not None:
                return result

    return _number(value)


def _log_json(label: str, value: Any) -> None:
    """Log JSON for diagnostics."""
    try:
        output = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except (TypeError, ValueError):
        output = repr(value)

    _LOGGER.warning(
        "HTF DIAGNOSTIC JSON BEGIN: %s\n%s\n"
        "HTF DIAGNOSTIC JSON END: %s",
        label,
        output,
        label,
    )


class HTFClient:
    """HTF web client."""

    def __init__(self, customer: str, pin: str) -> None:
        """Initialize."""
        self.customer = customer
        self.pin = pin
        self.session = requests.Session()
        self.dashboard_html = ""

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Home Assistant HTF integration)"
                )
            }
        )

    def _login(self) -> None:
        """Log in."""
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
                )
            },
            timeout=30,
        )
        response.raise_for_status()

        _LOGGER.debug("HTF: opening dashboard")

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

        _LOGGER.debug("HTF: login successful")

    def _select_consumption_point(self) -> None:
        """Select the consumption point."""
        _LOGGER.debug(
            "HTF: finding consumption point"
        )

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

        _LOGGER.debug(
            "HTF: consumption point found"
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
            "HTF: SetSelectedConsumption returned %r",
            response.text[:100],
        )

    def _page(self, path: str) -> str:
        """Get an authenticated page."""
        _LOGGER.debug(
            "HTF: requesting %s",
            path,
        )

        response = self.session.get(
            f"{BASE}{path}",
            timeout=30,
        )
        response.raise_for_status()

        return response.text

    @staticmethod
    def _json_block(
        html: str,
        element_id: str,
    ) -> dict[str, Any] | None:
        """Extract JSON from a page element."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        element = soup.select_one(
            f"#{element_id}"
        )

        if element is None:
            return None

        text = element.get_text(
            strip=True
        )

        if not text:
            return None

        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None

        return (
            value
            if isinstance(value, dict)
            else None
        )

    @staticmethod
    def _bills(html: str) -> dict[str, Any]:
        """Extract account statement."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        tables: list[list[list[str]]] = []

        for table in soup.find_all("table"):
            rows: list[list[str]] = []

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

                if cells:
                    rows.append(cells)

            if rows:
                tables.append(rows)

        amounts: list[float] = []
        bills: list[
            tuple[str, str | None, float]
        ] = []

        for table in tables:
            for row in table:
                if len(row) < 6:
                    continue

                description = row[3].strip()
                due_date = row[4].strip()
                amount = _number(row[5])

                if amount is None:
                    continue

                amounts.append(amount)

                description_lower = (
                    description.lower()
                )

                if (
                    "aconto-regning"
                    in description_lower
                    or "opgørelse"
                    in description_lower
                    or "faktura"
                    in description_lower
                ):
                    bills.append(
                        (
                            description,
                            due_date or None,
                            amount,
                        )
                    )

        balance = (
            round(sum(amounts), 2)
            if amounts
            else None
        )

        latest_bill = None
        due_date = None

        if bills:
            _, due_date, latest_bill = bills[0]

        return {
            "balance": balance,
            "latest_bill": latest_bill,
            "due_date": due_date,
            "tables": tables,
        }

    def fetch(self) -> dict[str, Any]:
        """Fetch HTF consumption and billing data."""
        _LOGGER.debug(
            "HTF: starting data update"
        )

        self._login()
        self._select_consumption_point()

        # IMPORTANT:
        # The main client only handles consumption and bills.
        # Return temperature is handled by return_temperature.py.
        consumption_html = self._page(
            "/forbrug/"
        )

        consumption = self._json_block(
            consumption_html,
            "consumption-data-json",
        )

        if not consumption:
            raise UpdateFailed(
                "HTF consumption JSON not found"
            )

        meters = consumption.get("meters")

        if not isinstance(
            meters,
            list,
        ) or not meters:
            raise UpdateFailed(
                "HTF returned no meters"
            )

        _LOGGER.debug(
            "HTF: consumption data received"
        )

        # Keep the existing consumption diagnostics.
        _log_json(
            "monthData",
            _get_month_data(
                consumption
            ),
        )

        _log_json(
            "yearData",
            _get_year_data(
                consumption
            ),
        )

        _log_json(
            "meterInfo",
            _get_meter_info(
                consumption
            ),
        )

        _LOGGER.warning(
            "HTF: current month=%s MWh",
            _current_month(consumption),
        )

        _LOGGER.warning(
            "HTF: current year=%s MWh",
            _current_year(consumption),
        )

        # Bills/account information.
        bills_html = self._page(
            "/kundeoplysninger/kontoudtog/"
        )

        bills = self._bills(
            bills_html
        )

        _log_json(
            "bills",
            bills,
        )

        _LOGGER.info(
            "HTF: data update successful"
        )

        return {
            "consumption": consumption,
            "bills": bills,
            "last_update": (
                dt_util.utcnow().isoformat()
            ),
        }


class HTFCoordinator(
    DataUpdateCoordinator[dict[str, Any]]
):
    """Coordinate HTF consumption and billing updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        customer: str,
        pin: str,
    ) -> None:
        """Initialize."""
        self.client = HTFClient(
            customer,
            pin,
        )

        super().__init__(
            hass,
            _LOGGER,
            name="HTF Selvbetjening",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(
        self,
    ) -> dict[str, Any]:
        """Fetch data without blocking HA."""
        try:
            return await self.hass.async_add_executor_job(
                self.client.fetch
            )
        except UpdateFailed:
            raise
        except Exception as err:
            _LOGGER.exception(
                "HTF: unexpected fetch error"
            )

            raise UpdateFailed(
                f"Unable to fetch HTF data: {err}"
            ) from err


async def _first_refresh(
    coordinator: HTFCoordinator,
) -> None:
    """Perform first refresh in background."""
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


async def _return_temperature_first_refresh(
    coordinator: ReturnTemperatureCoordinator,
) -> None:
    """Perform return-temperature refresh in background."""
    try:
        _LOGGER.debug(
            "HTF: starting return-temperature background refresh"
        )

        await coordinator.async_config_entry_first_refresh()

        _LOGGER.info(
            "HTF: return-temperature first refresh completed"
        )
    except Exception:
        _LOGGER.exception(
            "HTF: return-temperature first refresh failed"
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
        """Initialize."""
        super().__init__(
            coordinator
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    "heating",
                )
            },
            name="HTF Heating",
            manufacturer="Høje Taastrup Fjernvarme",
            configuration_url=BASE,
        )

    @property
    def available(self) -> bool:
        """Return availability."""
        return (
            super().available
            and self.coordinator.data is not None
        )

    def consumption(
        self,
    ) -> dict[str, Any]:
        """Return consumption."""
        if self.coordinator.data is None:
            return {}

        return self.coordinator.data.get(
            "consumption",
            {},
        )

    def bills(
        self,
    ) -> dict[str, Any]:
        """Return bills."""
        if self.coordinator.data is None:
            return {}

        return self.coordinator.data.get(
            "bills",
            {},
        )


class HTFCurrentMonth(
    HTFBaseSensor
):
    """Current month."""

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
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_heating_current_month"
        )

    @property
    def native_value(self) -> float | None:
        """Return current month."""
        return _current_month(
            self.consumption()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return historical data."""
        return {
            "month_data": _get_month_data(
                self.consumption()
            ),
            "meter_info": _get_meter_info(
                self.consumption()
            ),
            "last_update": (
                self.coordinator.data.get(
                    "last_update"
                )
                if self.coordinator.data
                else None
            ),
        }


class HTFDailyAverage(
    HTFBaseSensor
):
    """Estimated daily average."""

    _attr_name = "HTF Heating Daily Average"
    _attr_native_unit_of_measurement = "kWh/day"
    _attr_icon = "mdi:chart-line"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_heating_daily_average"
        )

    @property
    def native_value(self) -> float | None:
        """Return estimated daily average."""
        value = _current_month(
            self.consumption()
        )

        if value is None:
            return None

        return round(
            value * 1000 / dt_util.now().day,
            2,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return calculation information."""
        return {
            "calculation": (
                "Current month total / elapsed "
                "calendar days"
            ),
            "is_estimate": True,
            "note": (
                "HTF provides monthly consumption, "
                "not actual daily readings."
            ),
        }


class HTFCurrentYear(
    HTFBaseSensor
):
    """Current year."""

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
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_heating_current_year"
        )

    @property
    def native_value(self) -> float | None:
        """Return current year."""
        return _current_year(
            self.consumption()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return historical data."""
        return {
            "year_data": _get_year_data(
                self.consumption()
            ),
            "month_data": _get_month_data(
                self.consumption()
            ),
            "meter_info": _get_meter_info(
                self.consumption()
            ),
        }


class HTFReturnTemperature(
    CoordinatorEntity[ReturnTemperatureCoordinator],
    SensorEntity,
):
    """Return temperature."""

    _attr_has_entity_name = False
    _attr_name = "HTF Return Temperature"
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer"

    def __init__(
        self,
        coordinator: ReturnTemperatureCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_return_temperature"
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    "heating",
                )
            },
            name="HTF Heating",
            manufacturer="Høje Taastrup Fjernvarme",
            configuration_url=BASE,
        )

    @property
    def available(self) -> bool:
        """Return availability."""
        return (
            super().available
            and self.coordinator.data is not None
        )

    @property
    def native_value(self) -> float | None:
        """Return latest return temperature."""
        if not self.coordinator.data:
            return None

        value = self.coordinator.data.get(
            "temperature"
        )

        if isinstance(value, (int, float)):
            return round(
                float(value),
                2,
            )

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic information."""
        data = self.coordinator.data or {}

        return {
            "diagnostic_variables": data.get(
                "diagnostic_variables",
                [],
            ),
            "diagnostic_elements": data.get(
                "diagnostic_elements",
                [],
            ),
            "diagnostic_script_count": data.get(
                "diagnostic_script_count",
                0,
            ),
        }


class HTFBalance(
    HTFBaseSensor
):
    """Account balance."""

    _attr_name = "HTF Balance"
    _attr_native_unit_of_measurement = "DKK"
    _attr_icon = "mdi:bank"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = "htf_balance"

    @property
    def native_value(self) -> float | None:
        """Return balance."""
        return self.bills().get(
            "balance"
        )


class HTFLatestBill(
    HTFBaseSensor
):
    """Latest bill."""

    _attr_name = "HTF Latest Bill"
    _attr_native_unit_of_measurement = "DKK"
    _attr_icon = "mdi:receipt"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_latest_bill"
        )

    @property
    def native_value(self) -> float | None:
        """Return latest bill."""
        return self.bills().get(
            "latest_bill"
        )


class HTFLatestBillDueDate(
    HTFBaseSensor
):
    """Latest bill due date."""

    _attr_name = "HTF Latest Bill Due Date"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_latest_bill_due_date"
        )

    @property
    def native_value(self) -> str | None:
        """Return due date."""
        return self.bills().get(
            "due_date"
        )


class HTFBillStatus(
    HTFBaseSensor
):
    """Bill status."""

    _attr_name = "HTF Bill Status"
    _attr_icon = "mdi:cash-check"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = (
            "htf_bill_status"
        )

    @property
    def native_value(self) -> str:
        """Return bill status."""
        balance = self.bills().get(
            "balance"
        )

        if balance is None:
            return "Unknown"

        if balance > 0.01:
            return "Amount Due"

        if balance < -0.01:
            return "Credit"

        return "Settled"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up HTF."""
    coordinator = HTFCoordinator(
        hass,
        entry.data["customer"],
        entry.data["pin"],
    )

    return_temperature_coordinator = (
        ReturnTemperatureCoordinator(
            hass,
            entry.data["customer"],
            entry.data["pin"],
        )
    )

    hass.data.setdefault(
        DOMAIN,
        {}
    )[entry.entry_id] = {
        "coordinator": coordinator,
        "return_temperature_coordinator": (
            return_temperature_coordinator
        ),
    }

    async_add_entities(
        [
            HTFCurrentMonth(coordinator),
            HTFDailyAverage(coordinator),
            HTFCurrentYear(coordinator),
            HTFReturnTemperature(
                return_temperature_coordinator
            ),
            HTFBalance(coordinator),
            HTFLatestBill(coordinator),
            HTFLatestBillDueDate(coordinator),
            HTFBillStatus(coordinator),
        ]
    )

    hass.async_create_task(
        _first_refresh(coordinator)
    )

    hass.async_create_task(
        _return_temperature_first_refresh(
            return_temperature_coordinator
        )
    )