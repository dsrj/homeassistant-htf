"""HTF Selvbetjening Home Assistant sensors."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PIN,
    CONF_USERNAME,
    UnitOfEnergy,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
)
from homeassistant.util import dt as dt_util

from . import DOMAIN
from .consumption import (
    ConsumptionCoordinator,
    current_month,
    current_year,
)
from .bills import BillsCoordinator
from .return_temperature import (
    ReturnTemperatureCoordinator,
)

_LOGGER = logging.getLogger(__name__)


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

    consumption = ConsumptionCoordinator(
        hass,
        customer,
        pin,
    )

    bills = BillsCoordinator(
        hass,
        customer,
        pin,
    )

    return_temperature = (
        ReturnTemperatureCoordinator(
            hass,
            customer,
            pin,
        )
    )

    hass.async_create_task(
        _first_refresh(
            "consumption",
            consumption,
        )
    )

    hass.async_create_task(
        _first_refresh(
            "bills",
            bills,
        )
    )

    hass.async_create_task(
        _first_refresh(
            "return temperature",
            return_temperature,
        )
    )

    async_add_entities(
        [
            HTFCurrentMonthSensor(
                consumption
            ),
            HTFDailyAverageSensor(
                consumption
            ),
            HTFCurrentYearSensor(
                consumption
            ),
            HTFReturnTemperatureSensor(
                return_temperature
            ),
            HTFBalanceSensor(
                bills
            ),
            HTFLatestBillSensor(
                bills
            ),
            HTFLatestBillDueDateSensor(
                bills
            ),
            HTFBillStatusSensor(
                bills
            ),
        ]
    )


async def _first_refresh(
    name: str,
    coordinator,
) -> None:
    """Refresh one data source independently."""

    try:
        _LOGGER.debug(
            "HTF: starting %s background refresh",
            name,
        )

        await coordinator.async_config_entry_first_refresh()

        _LOGGER.info(
            "HTF: %s refresh completed successfully",
            name,
        )

    except Exception:
        # Failure of one data source must not
        # prevent the other data sources.
        _LOGGER.exception(
            "HTF: %s first refresh failed",
            name,
        )


class HTFEntity(
    CoordinatorEntity,
):
    """Base HTF entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
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
            "configuration_url": (
                "https://selvbetjening.htf.dk"
            ),
        }


class HTFCurrentMonthSensor(
    HTFEntity,
    SensorEntity,
):
    """Current month consumption."""

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
        return current_month(
            self.coordinator.data
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
        value = current_month(
            self.coordinator.data
        )

        if value is None:
            return None

        days = dt_util.now().day

        if days <= 0:
            return None

        return round(
            value * 1000 / days,
            1,
        )


class HTFCurrentYearSensor(
    HTFEntity,
    SensorEntity,
):
    """Current year consumption."""

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
        return current_year(
            self.coordinator.data
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
            "balance"
        )


class HTFLatestBillSensor(
    HTFEntity,
    SensorEntity,
):
    """Latest bill."""

    _attr_name = "Latest Bill"

    _attr_native_unit_of_measurement = (
        "DKK"
    )

    @property
    def native_value(
        self,
    ) -> float | None:
        return self.coordinator.data.get(
            "latest_bill"
        )


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
            "due_date"
        )


class HTFBillStatusSensor(
    HTFEntity,
    SensorEntity,
):
    """Current bill status."""

    _attr_name = "Bill Status"

    @property
    def native_value(
        self,
    ) -> str:
        balance = self.coordinator.data.get(
            "balance"
        )

        if balance is None:
            return "Unknown"

        if balance > 0.01:
            return "Amount Due"

        if balance < -0.01:
            return "Credit"

        return "Settled"