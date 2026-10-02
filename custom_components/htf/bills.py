"""HTF billing data."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import requests
from bs4 import BeautifulSoup

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

_LOGGER = logging.getLogger(__name__)

BASE = "https://selvbetjening.htf.dk"

LOGIN_PAGE = f"{BASE}/login"

LOGIN_POST = (
    f"{BASE}/umbraco/surface/login2/PostLogin"
)

ACCOUNT_PAGE = (
    f"{BASE}/kundeoplysninger/kontoudtog/"
)

SCAN_INTERVAL = timedelta(hours=24)


def _parse_amount(
    value: str,
) -> float | None:
    if not value:
        return None

    text = re.sub(
        r"[^\d,.\-]",
        "",
        value.strip(),
    )

    if not text:
        return None

    if "," in text:
        text = text.replace(
            ".",
            "",
        )
        text = text.replace(
            ",",
            ".",
        )
    else:
        text = text.replace(
            ",",
            "",
        )

    try:
        return float(text)
    except ValueError:
        return None


def parse_bills(
    html: str,
) -> dict[str, Any]:
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    amounts: list[float] = []

    bill_rows: list[
        dict[str, str]
    ] = []

    for table in soup.find_all(
        "table"
    ):
        rows = table.find_all(
            "tr"
        )

        if not rows:
            continue

        headers = [
            cell.get_text(
                " ",
                strip=True,
            )
            for cell in rows[0].find_all(
                ["th", "td"]
            )
        ]

        for row in rows[1:]:
            cells = [
                cell.get_text(
                    " ",
                    strip=True,
                )
                for cell in row.find_all(
                    "td"
                )
            ]

            if (
                not cells
                or len(cells)
                != len(headers)
            ):
                continue

            record = dict(
                zip(
                    headers,
                    cells,
                )
            )

            amount_text = ""
            description = ""

            for key, value in record.items():
                normalized = re.sub(
                    r"[^a-z0-9]",
                    "",
                    key.lower(),
                )

                if normalized in {
                    "beløb",
                    "beloeb",
                    "amount",
                }:
                    amount_text = value

                if normalized in {
                    "beskrivelse",
                    "description",
                }:
                    description = value

            amount = _parse_amount(
                amount_text
            )

            if amount is not None:
                amounts.append(amount)

            description_normalized = (
                description.lower()
            )

            if (
                "aconto"
                in description_normalized
                or "opgørelse"
                in description_normalized
                or "opgorelse"
                in description_normalized
            ):
                bill_rows.append(
                    record
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

    if bill_rows:
        latest = bill_rows[0]

        for key, value in latest.items():
            normalized = re.sub(
                r"[^a-z0-9]",
                "",
                key.lower(),
            )

            if normalized in {
                "beløb",
                "beloeb",
                "amount",
            }:
                latest_bill = (
                    _parse_amount(value)
                )

            elif normalized in {
                "forfaldsdato",
                "duedate",
            }:
                due_date = (
                    value or None
                )

    return {
        "balance": balance,
        "latest_bill": latest_bill,
        "due_date": due_date,
    }


class BillsClient:
    """HTF client dedicated to billing."""

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

    def fetch(
        self,
    ) -> dict[str, Any]:
        try:
            _LOGGER.debug(
                "HTF bills: "
                "opening login page"
            )

            self.session.get(
                LOGIN_PAGE,
                timeout=30,
            ).raise_for_status()

            _LOGGER.debug(
                "HTF bills: "
                "submitting login"
            )

            self.session.post(
                LOGIN_POST,
                data={
                    "kundenr": self.customer,
                    "pinkode": self.pin,
                },
                timeout=30,
            ).raise_for_status()

            _LOGGER.debug(
                "HTF bills: "
                "requesting account page"
            )

            response = self.session.get(
                ACCOUNT_PAGE,
                timeout=30,
            )

            response.raise_for_status()

            data = parse_bills(
                response.text
            )

            _LOGGER.info(
                "HTF bills: "
                "data update successful"
            )

            return data

        except requests.RequestException as err:
            raise UpdateFailed(
                f"HTF bills network error: {err}"
            ) from err


async def async_update(
    hass,
    customer: str,
    pin: str,
) -> dict[str, Any]:
    client = BillsClient(
        customer,
        pin,
    )

    return await hass.async_add_executor_job(
        client.fetch
    )


class BillsCoordinator(
    DataUpdateCoordinator[
        dict[str, Any]
    ]
):
    """Coordinator for HTF bills."""

    def __init__(
        self,
        hass,
        customer: str,
        pin: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="HTF Bills",
            update_method=lambda: (
                async_update(
                    hass,
                    customer,
                    pin,
                )
            ),
            update_interval=SCAN_INTERVAL,
        )