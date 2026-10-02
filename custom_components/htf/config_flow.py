"""Config flow for HTF Selvbetjening."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from . import DOMAIN


class HTFConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an HTF config flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial setup."""
        if user_input is not None:
            return self.async_create_entry(
                title="HTF Selvbetjening",
                data={
                    "customer": user_input["customer"],
                    "pin": user_input["pin"],
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("customer"): str,
                    vol.Required("pin"): str,
                }
            ),
        )
