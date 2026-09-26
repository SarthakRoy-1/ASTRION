"""Evidence, outside an order's own row, that its shipment may already be collected.

An order reads BOOKED until the carrier's pickup confirmation arrives, and the
product documentation warns that some carriers' confirmations run late. So a
BOOKED order is not proof the parcel is still there. Cancelling a parcel that was
in fact collected is the mistake the SOP guards against ("PICKED_UP: Do not
cancel"), and the SOP says that where data conflicts the conflict is named and
verified before anything changes.

The conflict is usually not in the order at all. It is in an open ticket from the
same customer, written by a person, saying the driver has been. This module finds
those tickets. It reads only the tickets the caller's scope already reaches, only
open ones, and only ones that plausibly concern *this* order:

- they name this order, or name no order and name this order's carrier;
- they were opened after the order was booked;
- they say a pickup or collection happened, and do not say it did not.

It decides nothing. A hit is a reason to stop and check with the carrier, reported
with the ticket's id and its own subject so the person can read it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from app.backend.models.records import Order, Ticket
from app.backend.services.records import get_account_tickets
from app.backend.tenancy import Scope

_ORDER_ID = re.compile(r"\bORD-\d+\b", re.IGNORECASE)

# "the driver collected the parcel", "picked up", "after driver pickup".
_PICKUP_HAPPENED = re.compile(
    r"\b(?:driver|courier|carrier|rider)\b[^.!?]{0,60}\b(?:collected|picked[\s-]?up|arrived)\b"
    r"|\b(?:collected|picked[\s-]?up)\b[^.!?]{0,40}\b(?:parcel|shipment|package|order|consignment)\b"
    r"|\b(?:parcel|shipment|package|order|consignment)\b[^.!?]{0,40}\b(?:was|has\s+been|got)\s+(?:collected|picked[\s-]?up)\b"
    r"|\bafter\s+(?:the\s+)?(?:driver\s+)?pick-?up\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"\b(?:not|never|hasn'?t|hasn't|didn'?t|wasn'?t|isn'?t|no\s+one|nobody|yet\s+to|failed\s+to)\b"
    r"[^.!?]{0,30}\b(?:collect(?:ed)?|pick(?:ed)?[\s-]?up|arriv(?:e|ed)|show(?:ed)?\s+up)\b",
    re.IGNORECASE,
)

_CLOSED = frozenset({"closed", "resolved", "solved", "done", "cancelled", "canceled"})


@dataclass(frozen=True)
class PickupReport:
    ticket_id: str
    status: str | None
    opened_at: str | None
    subject: str | None


def _mentions(ticket: Ticket, order: Order) -> bool:
    text = " ".join(filter(None, (ticket.subject, ticket.description)))
    named = {match.group(0).upper() for match in _ORDER_ID.finditer(text)}
    if named:
        return order.order_id.upper() in named
    carrier = (order.carrier or "").strip().lower()
    return bool(carrier) and carrier in text.lower()


def _reports_pickup(ticket: Ticket) -> bool:
    text = " ".join(filter(None, (ticket.subject, ticket.description)))
    return bool(_PICKUP_HAPPENED.search(text)) and not _NEGATED.search(text)


def find_pickup_reports(
    conn: sqlite3.Connection, order: Order, *, scope: Scope
) -> list[PickupReport]:
    """Open tickets from this order's account saying its pickup already happened."""
    reports: list[PickupReport] = []
    for ticket in get_account_tickets(conn, order.account_id, scope=scope):
        if (ticket.status or "").strip().lower() in _CLOSED:
            continue
        if (
            ticket.created_at is not None
            and order.booked_at is not None
            and ticket.created_at < order.booked_at
        ):
            continue
        if _mentions(ticket, order) and _reports_pickup(ticket):
            reports.append(
                PickupReport(
                    ticket_id=ticket.ticket_id,
                    status=ticket.status,
                    opened_at=ticket.created_at.isoformat() if ticket.created_at else None,
                    subject=ticket.subject,
                )
            )
    return reports
