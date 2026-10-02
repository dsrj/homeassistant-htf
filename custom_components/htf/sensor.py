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
from homeassistant.const import UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from . import DOMAIN
from .return_temperature import ReturnTemperatureClient

BASE = "https://selvbetjening.htf.dk"
SCAN_INTERVAL = timedelta(hours=24)

_LOGGER = logging.getLogger(__name__)


def _number(value: Any) -> float | None:
    """Convert a value to a number."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    text = text.replace("\xa0", "")
    text = text.replace(" ", "")

    if not text:
        return None

    if "," in text:
        text = text.replace(".", "")
        text = text.replace(",", ".")

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        text,
    )

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def _get_meter(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    """Get the main heating meter."""
    meters = consumption.get("meters")

    if not isinstance(
        meters,
        list,
    ):
        return {}

    for meter in meters:
        if not isinstance(
            meter,
            dict,
        ):
            continue

        info = meter.get("meterInfo")

        if not isinstance(
            info,
            dict,
        ):
            continue

        unit = str(
            info.get(
                "unit",
                "",
            )
        ).strip().upper()

        meter_type = str(
            info.get(
                "meterType",
                "",
            )
        ).strip().lower()

        if (
            unit == "MWH"
            or meter_type == "varmemåler"
        ):
            return meter

    for meter in meters:
        if isinstance(
            meter,
            dict,
        ):
            return meter

    return {}


def _get_month_data(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    """Get monthly heating data."""
    data = _get_meter(
        consumption
    ).get(
        "monthData"
    )

    if isinstance(
        data,
        dict,
    ):
        return data

    return {}


def _get_year_data(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    """Get yearly heating data."""
    data = _get_meter(
        consumption
    ).get(
        "yearData"
    )

    if isinstance(
        data,
        dict,
    ):
        return data

    return {}


def _get_meter_info(
    consumption: dict[str, Any],
) -> dict[str, Any]:
    """Get main meter information."""
    data = _get_meter(
        consumption
    ).get(
        "meterInfo"
    )

    if isinstance(
        data,
        dict,
    ):
        return data

    return {}


def _current_month(
    consumption: dict[str, Any],
) -> float | None:
    """Get current month consumption."""
    data = _get_month_data(
        consumption
    )

    now = dt_util.now()

    year_data = data.get(
        f"year_{now.year}"
    )

    if not isinstance(
        year_data,
        dict,
    ):
        return None

    values = year_data.get(
        "values",
        [],
    )

    months = year_data.get(
        "monthNumbers",
        [],
    )

    if not isinstance(
        values,
        list,
    ):
        return None

    if isinstance(
        months,
        list,
    ):
        for index, month in enumerate(
            months
        ):
            try:
                month_number = int(
                    month
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            if (
                month_number == now.month
                and index < len(values)
            ):
                return _number(
                    values[index]
                )

    return None


def _current_year(
    consumption: dict[str, Any],
) -> float | None:
    """Get current year consumption."""
    data = _get_year_data(
        consumption
    )

    now = dt_util.now()

    years = data.get(
        "yearNumbers",
        [],
    )

    values = data.get(
        "values",
        [],
    )

    if not isinstance(
        years,
        list,
    ):
        return None

    if not isinstance(
        values,
        list,
    ):
        return None

    for index, year in enumerate(
        years
    ):
        try:
            year_number = int(
                year
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if (
            year_number == now.year
            and index < len(values)
        ):
            return _number(
                values[index]
            )

    return None


def _log_json(
    label: str,
    value: Any,
) -> None:
    """Write diagnostic JSON."""
    try:
        output = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except (
        TypeError,
        ValueError,
    ):
        output = repr(value)

    _LOGGER.debug(
        "HTF diagnostic %s:\n%s",
        label,
        output,
    )


class HTFClient:
    """HTF web client."""

    def __init__(
        self,
        customer: str,
        pin: str,
    ) -> None:
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
        """Log in to HTF."""
        _LOGGER.debug(
            "HTF: opening login page"
        )

        response = self.session.get(
            f"{BASE}/login",
            timeout=30,
        )
        response.raise_for_status()

        _LOGGER.debug(
            "HTF: submitting login"
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

        response = self.session.get(
            f"{BASE}/dashboard/",
            timeout=30,
        )
        response.raise_for_status()

        if (
            "/login" in response.url.lower()
            or (
                "subheader-consumptionpoint-dropdown"
                not in response.text
            )
        ):
            raise UpdateFailed(
                "HTF login was not accepted"
            )

        self.dashboard_html = response.text

        _LOGGER.debug(
            "HTF: login successful"
        )

    def _select_consumption_point(
        self,
    ) -> None:
        """Select the active consumption point."""
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
            option = dropdown.find(
                "option"
            )

        if option is None:
            raise UpdateFailed(
                "HTF consumption point not found"
            )

        value = option.get(
            "value"
        )

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

    def _page(
        self,
        path: str,
    ) -> str:
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

        if (
            "/login" in response.url.lower()
            and path != "/login"
        ):
            raise UpdateFailed(
                "HTF session expired"
            )

        return response.text

    @staticmethod
    def _json_block(
        html: str,
        element_id: str,
    ) -> dict[str, Any] | None:
        """Extract JSON from an HTML element."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        element = soup.select_one(
            f"#{element_id}"
        )

        if element is None:
            return None

        raw = element.get_text(
            strip=True
        )

        if not raw:
            return None

        try:
            value = json.loads(
                raw
            )
        except json.JSONDecodeError:
            return None

        if isinstance(
            value,
            dict,
        ):
            return value

        return None

    @staticmethod
    def _bills(
        html: str,
    ) -> dict[str, Any]:
        """Extract account statement."""
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        tables: list[list[list[str]]] = []

        for table in soup.find_all(
            "table"
        ):
            rows: list[list[str]] = []

            for row in table.find_all(
                "tr"
            ):
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
                    rows.append(
                        cells
                    )

            if rows:
                tables.append(
                    rows
                )

        amounts: list[float] = []

        bills: list[
            tuple[
                str,
                str | None,
                float,
            ]
        ] = []

        for table in tables:
            for row in table:
                if len(row) < 6:
                    continue

                description = row[3].strip()
                due_date = row[4].strip()

                amount = _number(
                    row[5]
                )

                if amount is None:
                    continue

                amounts.append(
                    amount
                )

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
            round(
                sum(amounts),
                2,
            )
            if amounts
            else None
        )

        latest_bill = None
        due_date = None

        if bills:
            (
                _,
                due_date,
                latest_bill,
            ) = bills[0]

        return {
            "balance": balance,
            "latest_bill": latest_bill,
            "due_date": due_date,
            "tables": tables,
        }

    def fetch(
        self,
    ) -> dict[str, Any]:
        """Fetch HTF consumption and billing data."""
        _LOGGER.debug(
            "HTF: starting data update"
        )

        self._login()
        self._select_consumption_point()

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

        meters = consumption.get(
            "meters"
        )

        if (
            not isinstance(
                meters,
                list,
            )
            or not meters
        ):
            raise UpdateFailed(
                "HTF returned no meters"
            )

        _LOGGER.debug(
            "HTF: consumption data received"
        )

        _log_json(
            "meterInfo",
            _get_meter_info(
                consumption
            ),
        )

        _LOGGER.debug(
            "HTF: current month=%s MWh",
            _current_month(
                consumption
            ),
        )

        _LOGGER.debug(
            "HTF: current year=%s MWh",
            _current_year(
                consumption
            ),
        )

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

        # Return temperature is fetched separately so that
        # the already-working consumption/billing path stays isolated.
        return_temperature = (
            ReturnTemperatureClient(
                self.customer,
                self.pin,
            ).fetch()
        )

        _log_json(
            "return temperature",
            return_temperature,
        )

        _LOGGER.info(
            "HTF: consumption, billing and "
            "return-temperature update successful"
        )

        return {
            "consumption": consumption,
            "bills": bills,
            "return_temperature": return_temperature,
            "last_update": (
                dt_util.utcnow().isoformat()
            ),
        }


class HTFCoordinator(
    DataUpdateCoordinator[
        dict[str, Any]
    ]
):
    """Coordinate HTF updates."""

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
        """Fetch HTF data without blocking HA."""
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
    """Perform the first refresh in background."""
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
    def device_info(
        self,
    ) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    "heating",
                )
            },
            name="HTF Heating",
            manufacturer=(
                "Høje Taastrup Fjernvarme"
            ),
            configuration_url=BASE,
        )

    @property
    def available(
        self,
    ) -> bool:
        """Return availability."""
        return (
            super().available
            and self.coordinator.data
            is not None
        )

    def consumption(
        self,
    ) -> dict[str, Any]:
        """Return consumption data."""
        if self.coordinator.data is None:
            return {}

        return self.coordinator.data.get(
            "consumption",
            {},
        )

    def bills(
        self,
    ) -> dict[str, Any]:
        """Return billing data."""
        if self.coordinator.data is None:
            return {}

        return self.coordinator.data.get(
            "bills",
            {},
        )

    def return_temperature(
        self,
    ) -> dict[str, Any]:
        """Return return-temperature data."""
        if self.coordinator.data is None:
            return {}

        return self.coordinator.data.get(
            "return_temperature",
            {},
        )


class HTFCurrentMonth(
    HTFBaseSensor
):
    """Current month consumption."""

    _attr_name = (
        "HTF Heating Current Month"
    )

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
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_heating_current_month"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
        """Return current month."""
        return _current_month(
            self.consumption()
        )

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
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

    _attr_name = (
        "HTF Heating Daily Average"
    )

    _attr_native_unit_of_measurement = (
        "kWh/day"
    )

    _attr_icon = "mdi:chart-line"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_heating_daily_average"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
        """Return estimated daily average."""
        value = _current_month(
            self.consumption()
        )

        if value is None:
            return None

        day = dt_util.now().day

        if day <= 0:
            return None

        return round(
            value * 1000 / day,
            2,
        )

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
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
    """Current year consumption."""

    _attr_name = (
        "HTF Heating Current Year"
    )

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
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_heating_current_year"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
        """Return current year."""
        return _current_year(
            self.consumption()
        )

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
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
    HTFBaseSensor
):
    """HTF district-heating return temperature."""

    _attr_name = (
        "HTF Return Temperature"
    )

    _attr_native_unit_of_measurement = (
        UnitOfTemperature.CELSIUS
    )

    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer-water"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_return_temperature"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
        """Return calculated return temperature."""
        value = self.return_temperature().get(
            "temperature"
        )

        if value is None:
            return None

        return float(value)

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return return-temperature information."""
        data = self.return_temperature()

        return {
            "source_date": data.get(
                "date"
            ),
            "good_temperature_c": data.get(
                "target"
            ),
            "good_return_temperature_data": data.get(
                "good_return_temperature_data"
            ),
            "calculation": (
                "HTF /forbrug/: FV-RT / FV-M3"
            ),
        }


class HTFGoodReturnTemperature(
    HTFBaseSensor
):
    """HTF yearly good return-temperature target."""

    _attr_name = (
        "HTF Good Return Temperature"
    )

    _attr_native_unit_of_measurement = (
        UnitOfTemperature.CELSIUS
    )

    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:thermometer-check"

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_good_return_temperature"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
        """Return the HTF target for the current data year."""
        value = self.return_temperature().get(
            "target"
        )

        if value is None:
            return None

        return float(value)

    @property
    def extra_state_attributes(
        self,
    ) -> dict[str, Any]:
        """Return yearly target information."""
        data = self.return_temperature()
        date_key = data.get(
            "date"
        )

        return {
            "year": (
                str(date_key)[:4]
                if date_key
                else None
            ),
            "all_year_targets": data.get(
                "good_return_temperature_data"
            ),
            "source": (
                "HTF goodReturnTemperatureData"
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
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_balance"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
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
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_latest_bill"
        )

    @property
    def native_value(
        self,
    ) -> float | None:
        """Return latest bill."""
        return self.bills().get(
            "latest_bill"
        )


class HTFLatestBillDueDate(
    HTFBaseSensor
):
    """Latest bill due date."""

    _attr_name = (
        "HTF Latest Bill Due Date"
    )

    _attr_icon = (
        "mdi:calendar-clock"
    )

    def __init__(
        self,
        coordinator: HTFCoordinator,
    ) -> None:
        """Initialize."""
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_latest_bill_due_date"
        )

    @property
    def native_value(
        self,
    ) -> str | None:
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
        super().__init__(
            coordinator
        )

        self._attr_unique_id = (
            "htf_bill_status"
        )

    @property
    def native_value(
        self,
    ) -> str:
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
    """Set up HTF sensors."""
    coordinator = HTFCoordinator(
        hass,
        entry.data["customer"],
        entry.data["pin"],
    )

    hass.data.setdefault(
        DOMAIN,
        {},
    )[entry.entry_id] = {
        "coordinator": coordinator,
    }

    async_add_entities(
        [
            HTFCurrentMonth(
                coordinator
            ),
            HTFDailyAverage(
                coordinator
            ),
            HTFCurrentYear(
                coordinator
            ),
            HTFReturnTemperature(
                coordinator
            ),
            HTFGoodReturnTemperature(
                coordinator
            ),
            HTFBalance(
                coordinator
            ),
            HTFLatestBill(
                coordinator
            ),
            HTFLatestBillDueDate(
                coordinator
            ),
            HTFBillStatus(
                coordinator
            ),
        ]
    )

    hass.async_create_task(
        _first_refresh(
            coordinator
        )
    )