"""Flight MCP server (Duffel TEST MODE for search).

A local Model Context Protocol (MCP) server that exposes flight-search
tools. Search is backed by the Duffel Flights API in TEST MODE. This server
does NOT perform any real booking or payment; it only searches for offers
and looks up the current price of a specific offer.

Built with the current MCP Python SDK v2 (the `mcp` package) using the
`MCPServer` class. Tools are defined as plain functions with type hints and
docstrings and registered via the `@mcp.tool()` decorator, so an AI agent
can read each tool's schema and description to decide when to call it.

Credentials:
    The Duffel access token is read from the DUFFEL_ACCESS_TOKEN environment
    variable. It is never hard-coded. Use a Duffel *test* token so no real
    orders can be created.
"""

from __future__ import annotations

import os
from typing import Any, Optional, TypedDict

import httpx

from mcp.server import MCPServer

# The named MCP server. The name "flight-demo" is what clients/agents see.
mcp = MCPServer("flight-demo")

# --- Duffel API configuration -------------------------------------------------

DUFFEL_BASE_URL = "https://api.duffel.com"
DUFFEL_OFFER_REQUESTS_URL = f"{DUFFEL_BASE_URL}/air/offer_requests"
DUFFEL_OFFERS_URL = f"{DUFFEL_BASE_URL}/air/offers"
DUFFEL_ORDERS_URL = f"{DUFFEL_BASE_URL}/air/orders"
DUFFEL_PAYMENTS_URL = f"{DUFFEL_BASE_URL}/air/payments"

# The exact phrase a caller must pass to create_test_booking. This is a
# deliberate speed-bump so an order is never created by accident.
_TEST_BOOKING_CONFIRMATION = "CONFIRM_TEST_BOOKING"

# The exact phrase a caller must pass to create_test_hold_booking. Distinct
# from the instant-booking phrase so the two flows can never be confused.
_TEST_HOLD_BOOKING_CONFIRMATION = "CONFIRM_TEST_HOLD_BOOKING"

# The exact phrase a caller must pass to pay_test_order. Distinct from the
# booking phrases so paying can never be confused with creating a hold.
_TEST_PAYMENT_CONFIRMATION = "CONFIRM_TEST_PAYMENT"

# Prefix that every Duffel TEST access token starts with. create_test_booking
# refuses to run unless the configured token begins with this, so it can
# never hit a live account.
_DUFFEL_TEST_TOKEN_PREFIX = "duffel_test_"

# Only return the cheapest N offers to keep the MCP payload small.
MAX_OFFERS = 10

# Reasonable network timeout for the Duffel calls (seconds).
_HTTP_TIMEOUT = 30.0


class DuffelError(RuntimeError):
    """Raised when the Duffel API cannot be reached or returns an error.

    The message is safe to surface to an agent/user and never contains the
    access token.
    """


class SimplifiedOffer(TypedDict):
    """A simplified flight offer extracted from a Duffel offer.

    Fields:
        offer_id: Duffel offer id (use with get_flight_price).
        airline: Owner/operating carrier name for the offer.
        origin: Origin airport IATA code of the first slice.
        destination: Destination airport IATA code of the first slice.
        departure_time: ISO 8601 departure time of the first segment.
        arrival_time: ISO 8601 arrival time of the last segment of the
            first slice.
        price: Total amount as a string (Duffel returns decimal strings).
        currency: ISO currency code for the total amount.
        direct: True only if every slice has exactly one segment.
        expires_at: ISO 8601 timestamp after which the offer is no longer
            guaranteed by Duffel.
    """

    offer_id: str
    airline: str
    origin: str
    destination: str
    departure_time: str
    arrival_time: str
    price: str
    currency: str
    direct: bool
    expires_at: str


def _get_token() -> str:
    """Return the Duffel access token or raise a clear error.

    Reads DUFFEL_ACCESS_TOKEN from the environment. The token is never
    hard-coded and never echoed back in error messages.
    """
    token = os.environ.get("DUFFEL_ACCESS_TOKEN")
    if not token:
        raise DuffelError(
            "Missing DUFFEL_ACCESS_TOKEN environment variable. Set it to a "
            "Duffel TEST access token before searching for flights."
        )
    return token


def _duffel_headers() -> dict[str, str]:
    """Build the required Duffel request headers, including auth."""
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Duffel-Version": "v2",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _format_duffel_errors(payload: dict[str, Any]) -> str:
    """Turn a Duffel error payload into a single readable message."""
    errors = payload.get("errors")
    if not errors:
        return "Unknown Duffel API error."
    parts = []
    for err in errors:
        title = err.get("title") or err.get("code") or "error"
        detail = err.get("message") or err.get("detail") or ""
        parts.append(f"{title}: {detail}".strip().rstrip(":"))
    return "; ".join(parts)


def _build_slices(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
) -> list[dict[str, str]]:
    """Build the Duffel slices for a one-way or round-trip search.

    Always includes an outbound slice origin -> destination. If return_date
    is provided, appends a return slice destination -> origin.
    """
    slices: list[dict[str, str]] = [
        {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
        }
    ]
    if return_date:
        slices.append(
            {
                "origin": destination,
                "destination": origin,
                "departure_date": return_date,
            }
        )
    return slices


def _is_direct(offer: dict[str, Any]) -> bool:
    """Return True only if every slice in the offer has exactly one segment."""
    slices = offer.get("slices") or []
    if not slices:
        return False
    return all(len(s.get("segments") or []) == 1 for s in slices)


def _simplify_offer(offer: dict[str, Any]) -> SimplifiedOffer:
    """Extract the simplified fields from a full Duffel offer object."""
    slices = offer.get("slices") or []
    first_slice = slices[0] if slices else {}
    segments = first_slice.get("segments") or []
    first_segment = segments[0] if segments else {}
    last_segment = segments[-1] if segments else {}

    origin = (first_slice.get("origin") or {}).get("iata_code", "")
    destination = (first_slice.get("destination") or {}).get("iata_code", "")

    owner = offer.get("owner") or {}
    airline = owner.get("name") or owner.get("iata_code") or "Unknown"

    return {
        "offer_id": offer.get("id", ""),
        "airline": airline,
        "origin": origin,
        "destination": destination,
        "departure_time": first_segment.get("departing_at", ""),
        "arrival_time": last_segment.get("arriving_at", ""),
        "price": offer.get("total_amount", ""),
        "currency": offer.get("total_currency", ""),
        "direct": _is_direct(offer),
        "expires_at": offer.get("expires_at", ""),
    }


def _sort_key(offer: dict[str, Any]) -> float:
    """Numeric sort key on Duffel's total_amount (a decimal string)."""
    try:
        return float(offer.get("total_amount"))
    except (TypeError, ValueError):
        return float("inf")


def _fetch_offer(offer_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single Duffel offer by id (read-only GET).

    Shared by get_flight_price and prepare_booking. Performs
    GET /air/offers/{offer_id} with the standard headers and error
    handling. This never books, pays, or modifies anything.

    Returns:
        The Duffel offer object (dict) if found, or None if the offer does
        not exist / is no longer available (HTTP 404 or empty body).

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing, the request fails at
            the transport level, Duffel returns a non-404 error, or the
            response is not valid JSON.
    """
    try:
        response = httpx.get(
            f"{DUFFEL_OFFERS_URL}/{offer_id}",
            headers=_duffel_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if response.status_code == 404:
        return None

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise DuffelError(
            f"Duffel API error (HTTP {response.status_code}): "
            f"{_format_duffel_errors(payload)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    offer = payload.get("data") or {}
    return offer or None


@mcp.tool()
def search_flights(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str] = None,
) -> list[SimplifiedOffer]:
    """Search for real flight offers via the Duffel API (TEST MODE).

    Call this tool when a user wants to find flights between two airports on
    given dates. Assumes one adult passenger in economy class. Returns the
    cheapest offers so an agent can present options and, if needed, refresh
    a specific offer's price with get_flight_price.

    This searches Duffel in TEST MODE. It does NOT book or pay for anything.

    Args:
        origin: Departure airport IATA code (e.g. "TPE").
        destination: Arrival airport IATA code (e.g. "NRT").
        departure_date: Outbound date in ISO format "YYYY-MM-DD".
        return_date: Optional return date in ISO format "YYYY-MM-DD". When
            provided, a return slice destination -> origin is added for a
            round-trip search.

    Returns:
        Up to the 10 cheapest SimplifiedOffer dictionaries, sorted by total
        price ascending. Each contains offer_id, airline, origin,
        destination, departure_time, arrival_time, price, currency, direct,
        and expires_at.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing, the HTTP request
            fails, Duffel returns an error, or no offers are found.
    """
    slices = _build_slices(origin, destination, departure_date, return_date)
    request_body = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"}],
            "cabin_class": "economy",
        }
    }

    try:
        response = httpx.post(
            DUFFEL_OFFER_REQUESTS_URL,
            headers=_duffel_headers(),
            json=request_body,
            # Ask Duffel to include the offers in the response so we don't
            # need a second round-trip to list them.
            params={"return_offers": "true"},
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise DuffelError(
            f"Duffel API error (HTTP {response.status_code}): "
            f"{_format_duffel_errors(payload)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    offers = (payload.get("data") or {}).get("offers") or []
    if not offers:
        raise DuffelError(
            "No flight offers were found for the requested route and dates."
        )

    offers.sort(key=_sort_key)
    cheapest = offers[:MAX_OFFERS]
    return [_simplify_offer(offer) for offer in cheapest]


@mcp.tool()
def get_flight_price(offer_id: str) -> dict:
    """Get the current price of a specific Duffel offer (TEST MODE).

    Call this tool after search_flights when the user has picked an offer
    and you want to confirm its latest price before presenting a final
    number. Offers expire, so this may fail if the offer is stale.

    Args:
        offer_id: The offer_id from a SimplifiedOffer returned by
            search_flights (a Duffel offer id such as "off_...").

    Returns:
        A dictionary with:
            offer_id: The requested offer id.
            price: Current total amount as a string, or None if unavailable.
            currency: ISO currency code, or None if unavailable.
            expires_at: ISO 8601 expiry timestamp, or None if unavailable.
            found: True if the offer was returned by Duffel, False otherwise.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing or the HTTP request
            itself fails at the transport level.
    """
    offer = _fetch_offer(offer_id)
    if not offer:
        return {
            "offer_id": offer_id,
            "price": None,
            "currency": None,
            "expires_at": None,
            "found": False,
        }

    return {
        "offer_id": offer.get("id", offer_id),
        "price": offer.get("total_amount"),
        "currency": offer.get("total_currency"),
        "expires_at": offer.get("expires_at"),
        "found": True,
    }


# Constant confirmation message returned by prepare_booking so the wording
# is consistent and unmistakable to any agent reading the result.
_NO_BOOKING_MESSAGE = (
    "No booking has been made. Explicit user confirmation is required "
    "before any booking tool may be called."
)


@mcp.tool()
def prepare_booking(offer_id: str) -> dict:
    """Review a Duffel offer before booking. Does NOT book, pay, or reserve.

    This is a SAFE pre-booking review step only. It fetches the latest state
    of an offer so an agent can show the user a final summary and ask for
    explicit confirmation. It never creates an order, booking, payment,
    reservation, or ticket, and it never cancels anything.

    Call this after the user has chosen an offer from search_flights and you
    want to present a final review. To actually book, a separate, explicitly
    confirmed step would be required, which this server does not implement.

    Args:
        offer_id: The offer_id from a SimplifiedOffer returned by
            search_flights (a Duffel offer id such as "off_...").

    Offer expiry is validated here: still_available is True only if the
    fetch succeeded, expires_at exists and parses as a timezone-aware UTC
    datetime, and the current UTC time is strictly before expires_at.

    Returns:
        On success (offer present and not expired), a dictionary with the
        offer summary plus safety flags:
            offer_id, airline, origin, destination, departure_time,
            arrival_time, price, currency, direct, expires_at,
            current_utc_time (ISO 8601 UTC time the check ran),
            still_available (True), requires_confirmation (always True),
            booking_status (always "NOT_BOOKED"), and a message stating no
            booking was made.

        It also includes fields needed to build a later sandbox order:
            live_mode: Whether the offer is a live (True) or test (False)
                offer.
            passenger_ids: List of Duffel passenger ids from the offer's
                passengers array (needed when creating an order).
            requires_instant_payment: Whether the offer must be paid for
                immediately when ordering.
            payment_required_by: ISO 8601 deadline to pay, if provided.
            passenger_identity_documents_required: Whether passenger
                identity documents must be supplied to book.
            total_amount: The offer's total amount as a string.
            total_currency: ISO currency code for total_amount.

        If the offer exists but has expired (or its expiry is missing/
        unparseable), a safe dictionary with the offer summary plus:
            expires_at, current_utc_time, still_available (False),
            requires_confirmation (True), booking_status ("OFFER_EXPIRED"),
            error (a description), and the no-booking message.

        If the offer cannot be fetched at all, a safe error dictionary with:
            offer_id, still_available (False), requires_confirmation (True),
            booking_status ("NOT_BOOKED"), error (a description), and the
            same no-booking message. No booking is attempted in any case.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing or the HTTP request
            fails at the transport level.
    """
    offer = _fetch_offer(offer_id)
    if not offer:
        # Offer is gone/expired/unknown. Do NOT proceed; return safely.
        return {
            "offer_id": offer_id,
            "still_available": False,
            "requires_confirmation": True,
            "booking_status": "NOT_BOOKED",
            "error": (
                "The offer could not be retrieved. It may have expired or "
                "no longer exists. Please search again for a fresh offer."
            ),
            "message": _NO_BOOKING_MESSAGE,
        }

    summary = _simplify_offer(offer)

    # --- Expiry validation --------------------------------------------------
    # still_available must NOT be true merely because Duffel returned the
    # offer. Verify the offer has not expired by comparing its expires_at
    # against the current time, both as timezone-aware UTC datetimes.
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)
    current_utc_time = now_utc.isoformat()

    raw_expires_at = offer.get("expires_at")
    expires_dt: Optional[datetime] = None
    if raw_expires_at:
        try:
            # Normalize a trailing "Z" to an explicit +00:00 UTC offset so
            # fromisoformat yields a timezone-aware datetime.
            expires_dt = datetime.fromisoformat(
                raw_expires_at.replace("Z", "+00:00")
            )
        except ValueError:
            expires_dt = None

    # Offer is available only if the fetch succeeded (guaranteed here),
    # expires_at exists and parsed, and now is strictly before it.
    still_available = expires_dt is not None and now_utc < expires_dt

    if not still_available:
        # Expired, missing, or unparseable expiry -> do NOT proceed.
        return {
            "offer_id": summary["offer_id"] or offer_id,
            "airline": summary["airline"],
            "origin": summary["origin"],
            "destination": summary["destination"],
            "departure_time": summary["departure_time"],
            "arrival_time": summary["arrival_time"],
            "price": summary["price"],
            "currency": summary["currency"],
            "direct": summary["direct"],
            "expires_at": raw_expires_at,
            "current_utc_time": current_utc_time,
            "still_available": False,
            "requires_confirmation": True,
            "booking_status": "OFFER_EXPIRED",
            "error": (
                "The offer has expired (or its expiry is missing/invalid). "
                "Its expires_at is not after the current UTC time. Please "
                "search again for a fresh offer before booking."
            ),
            "message": _NO_BOOKING_MESSAGE,
        }

    # Collect the extra fields needed later to build a sandbox order.
    # passenger_ids: each entry in the offer's passengers array has an id.
    passengers = offer.get("passengers") or []
    passenger_ids = [p.get("id") for p in passengers if p.get("id")]

    # payment_requirements holds the instant-payment flag and deadline.
    payment = offer.get("payment_requirements") or {}

    return {
        "offer_id": summary["offer_id"] or offer_id,
        "airline": summary["airline"],
        "origin": summary["origin"],
        "destination": summary["destination"],
        "departure_time": summary["departure_time"],
        "arrival_time": summary["arrival_time"],
        "price": summary["price"],
        "currency": summary["currency"],
        "direct": summary["direct"],
        "expires_at": summary["expires_at"],
        "current_utc_time": current_utc_time,
        "still_available": True,
        "requires_confirmation": True,
        "booking_status": "NOT_BOOKED",
        # Extra fields to prepare a later sandbox order creation tool.
        "live_mode": offer.get("live_mode"),
        "passenger_ids": passenger_ids,
        "requires_instant_payment": payment.get("requires_instant_payment"),
        "payment_required_by": payment.get("payment_required_by"),
        "passenger_identity_documents_required": offer.get(
            "passenger_identity_documents_required"
        ),
        "total_amount": offer.get("total_amount"),
        "total_currency": offer.get("total_currency"),
        "message": _NO_BOOKING_MESSAGE,
    }


# =============================================================================
# TEST-MODE BOOKING TOOL
# =============================================================================
# WARNING: create_test_booking below DOES create a real Duffel order via
# POST /air/orders. It is deliberately restricted to Duffel TEST MODE ONLY:
#   - the access token MUST start with "duffel_test_"
#   - the fetched offer MUST have live_mode == false
#   - the caller MUST pass the exact confirmation phrase
# In test mode this creates a sandbox order that costs no real money and
# issues no real ticket. It must NEVER be pointed at a live Duffel token.
# This tool does NOT support live booking, card payment, cancellation,
# changes, or refunds. It handles exactly one adult, economy, no extras.
# =============================================================================


def _booking_error(offer_id: str, message: str) -> dict:
    """Build a safe, uniform refusal/error result (no order was created)."""
    return {
        "order_id": None,
        "booking_reference": None,
        "live_mode": None,
        "offer_id": offer_id,
        "booking_status": "NOT_BOOKED",
        "error": message,
    }


@mcp.tool()
def create_test_booking(
    offer_id: str,
    given_name: str,
    family_name: str,
    born_on: str,
    gender: str,
    title: str,
    email: str,
    phone_number: str,
    confirmation_phrase: str,
) -> dict:
    """Create a Duffel TEST-MODE sandbox order. TEST MODE ONLY.

    !!! TEST MODE ONLY !!!
    This tool creates a real Duffel order via POST /air/orders, but it is
    hard-restricted to Duffel's sandbox so it costs no real money and issues
    no real ticket. It refuses to run unless ALL of these hold:
      * DUFFEL_ACCESS_TOKEN starts with "duffel_test_"
      * the fetched offer has live_mode == false
      * confirmation_phrase exactly equals "CONFIRM_TEST_BOOKING"

    It supports EXACTLY one adult passenger, economy, with no extra services,
    no baggage purchase, and no seat purchase. It does NOT implement live
    booking, card payment, cancellation, changes, or refunds. If the offer
    requires passenger identity documents, it stops safely (passport handling
    is intentionally not implemented).

    Args:
        offer_id: Duffel offer id to book (from search_flights).
        given_name: Passenger's given/first name.
        family_name: Passenger's family/last name.
        born_on: Passenger date of birth, ISO "YYYY-MM-DD".
        gender: Passenger gender as required by Duffel (e.g. "m" or "f").
        title: Passenger title (e.g. "mr", "ms", "mrs", "miss").
        email: Contact email for the booking.
        phone_number: Contact phone in E.164 format (e.g. "+886912345678").
        confirmation_phrase: Must be exactly "CONFIRM_TEST_BOOKING" or the
            tool refuses to create an order.

    Returns:
        On success, a simplified order summary with order_id,
        booking_reference, live_mode, offer_id, airline, origin,
        destination, departure_time, arrival_time, total_amount,
        total_currency, payment_status, and booking_status
        ("TEST_BOOKING_CREATED").

        On any safety refusal or error, a dict with booking_status
        "NOT_BOOKED" and an "error" explaining why. No order is created in
        that case.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing or an HTTP request
            fails at the transport level.
    """
    # --- Guard 1: token must be a TEST token ------------------------------
    # Read the existing env var; _get_token() raises if it is missing.
    token = _get_token()
    if not token.startswith(_DUFFEL_TEST_TOKEN_PREFIX):
        return _booking_error(
            offer_id,
            "Refusing to book: DUFFEL_ACCESS_TOKEN is not a test token "
            "(must start with 'duffel_test_'). This tool is TEST MODE ONLY.",
        )

    # --- Guard 2: exact confirmation phrase -------------------------------
    if confirmation_phrase != _TEST_BOOKING_CONFIRMATION:
        return _booking_error(
            offer_id,
            "Refusing to book: confirmation_phrase must be exactly "
            f"'{_TEST_BOOKING_CONFIRMATION}'.",
        )

    # --- Fetch the latest offer (read-only) -------------------------------
    offer = _fetch_offer(offer_id)
    if not offer:
        return _booking_error(
            offer_id,
            "The offer could not be retrieved. It may have expired or no "
            "longer exists. Search again for a fresh offer.",
        )

    # --- Guard 3: offer must be a test (sandbox) offer --------------------
    if offer.get("live_mode") is not False:
        return _booking_error(
            offer_id,
            "Refusing to book: offer live_mode is not false. This tool "
            "only creates TEST MODE sandbox orders.",
        )

    # --- Guard 4: no passport/identity-document handling yet --------------
    if offer.get("passenger_identity_documents_required") is True:
        return _booking_error(
            offer_id,
            "Refusing to book: this offer requires passenger identity "
            "documents, which are not implemented yet.",
        )

    # --- Guard 5: exactly one passenger id from the offer -----------------
    passengers = offer.get("passengers") or []
    passenger_ids = [p.get("id") for p in passengers if p.get("id")]
    if len(passenger_ids) != 1:
        return _booking_error(
            offer_id,
            "Refusing to book: this tool supports exactly one adult "
            f"passenger, but the offer has {len(passenger_ids)}.",
        )
    passenger_id = passenger_ids[0]

    # --- Re-check the latest price immediately before booking -------------
    # Use the freshly fetched offer's own totals as the payment amount so we
    # pay exactly what Duffel currently quotes.
    total_amount = offer.get("total_amount")
    total_currency = offer.get("total_currency")
    if not total_amount or not total_currency:
        return _booking_error(
            offer_id,
            "Refusing to book: the offer is missing a current price.",
        )

    # --- Build the order payload (TEST MODE) ------------------------------
    # type="instant", single selected offer, and payment from the Duffel
    # test-account "balance" (NOT a real card). Exactly one adult passenger,
    # no extra services / baggage / seats are added.
    order_body = {
        "data": {
            "type": "instant",
            "selected_offers": [offer_id],
            "payments": [
                {
                    "type": "balance",
                    "currency": total_currency,
                    "amount": total_amount,
                }
            ],
            "passengers": [
                {
                    "id": passenger_id,
                    "given_name": given_name,
                    "family_name": family_name,
                    "born_on": born_on,
                    "gender": gender,
                    "title": title,
                    "email": email,
                    "phone_number": phone_number,
                }
            ],
        }
    }

    # --- Create the Duffel TEST order -------------------------------------
    try:
        response = httpx.post(
            DUFFEL_ORDERS_URL,
            headers=_duffel_headers(),
            json=order_body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return _booking_error(
            offer_id,
            f"Duffel API error (HTTP {response.status_code}): "
            f"{_format_duffel_errors(payload)}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    order = payload.get("data") or {}

    # Pull flight details from the created order for a friendly summary.
    summary = _simplify_offer(order)
    owner = order.get("owner") or {}
    airline = owner.get("name") or owner.get("iata_code") or summary["airline"]

    return {
        "order_id": order.get("id"),
        "booking_reference": order.get("booking_reference"),
        "live_mode": order.get("live_mode"),
        "offer_id": offer_id,
        "airline": airline,
        "origin": summary["origin"],
        "destination": summary["destination"],
        "departure_time": summary["departure_time"],
        "arrival_time": summary["arrival_time"],
        "total_amount": order.get("total_amount", total_amount),
        "total_currency": order.get("total_currency", total_currency),
        "payment_status": order.get("payment_status"),
        "booking_status": "TEST_BOOKING_CREATED",
    }


# =============================================================================
# TEST-MODE HOLD BOOKING TOOL
# =============================================================================
# WARNING: create_test_hold_booking below DOES create a real Duffel order via
# POST /air/orders, but as a HOLD order with NO payment attached. It is
# deliberately restricted to Duffel TEST MODE ONLY:
#   - the access token MUST start with "duffel_test_"
#   - the fetched offer MUST have live_mode == false
#   - the offer MUST NOT require instant payment
#   - the caller MUST pass the exact hold-confirmation phrase
# A hold order reserves the fare without paying; in test mode this costs no
# real money and issues no real ticket. It must NEVER be pointed at a live
# Duffel token. No payment, card, services, baggage, seats, or live booking.
# =============================================================================


def _offer_is_expired(offer: dict[str, Any]) -> bool:
    """Return True if the offer's expires_at is in the past.

    Parses Duffel's ISO 8601 expires_at (which ends in "Z"). If the value is
    missing or unparseable, we conservatively treat the offer as NOT expired
    here and let Duffel be the final authority at order-creation time.
    """
    from datetime import datetime, timezone

    raw = offer.get("expires_at")
    if not raw:
        return False
    try:
        # Normalize a trailing "Z" to an explicit UTC offset for fromisoformat.
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return expires <= datetime.now(timezone.utc)


@mcp.tool()
def create_test_hold_booking(
    offer_id: str,
    given_name: str,
    family_name: str,
    born_on: str,
    gender: str,
    title: str,
    email: str,
    phone_number: str,
    confirmation_phrase: str,
) -> dict:
    """Create a Duffel TEST-MODE HOLD order (no payment). TEST MODE ONLY.

    !!! TEST MODE ONLY !!!
    This creates a real Duffel order via POST /air/orders with type "hold"
    and NO payments field, so the fare is reserved without paying. It is
    hard-restricted to Duffel's sandbox and refuses to run unless ALL hold:
      * DUFFEL_ACCESS_TOKEN starts with "duffel_test_"
      * the fetched offer has live_mode == false
      * the offer does NOT require instant payment
      * the offer has not expired
      * confirmation_phrase exactly equals "CONFIRM_TEST_HOLD_BOOKING"

    It supports EXACTLY one adult passenger. It never adds payment, card
    information, services, baggage, seats, or live-mode booking. If the offer
    requires passenger identity documents, it stops safely (passport handling
    is intentionally not implemented).

    Args:
        offer_id: Duffel offer id to hold (from search_flights).
        given_name: Passenger's given/first name.
        family_name: Passenger's family/last name.
        born_on: Passenger date of birth, ISO "YYYY-MM-DD".
        gender: Passenger gender as required by Duffel (e.g. "m" or "f").
        title: Passenger title (e.g. "mr", "ms", "mrs", "miss").
        email: Contact email for the booking.
        phone_number: Contact phone in E.164 format (e.g. "+886912345678").
        confirmation_phrase: Must be exactly "CONFIRM_TEST_HOLD_BOOKING" or
            the tool refuses to create an order.

    Returns:
        On success, a simplified hold-order summary with order_id,
        booking_reference, live_mode, offer_id, airline, origin,
        destination, departure_time, arrival_time, total_amount,
        total_currency, payment_required_by, payment_status, booking_status
        ("TEST_HOLD_BOOKING_CREATED"), and a note that no real flight was
        booked and no real payment was made.

        On any safety refusal or error, a dict with booking_status
        "NOT_BOOKED" and an "error" explaining why. No order is created in
        that case.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing or an HTTP request
            fails at the transport level.
    """
    # --- Guard 1: token must be a TEST token ------------------------------
    token = _get_token()
    if not token.startswith(_DUFFEL_TEST_TOKEN_PREFIX):
        return _booking_error(
            offer_id,
            "Refusing to hold: DUFFEL_ACCESS_TOKEN is not a test token "
            "(must start with 'duffel_test_'). This tool is TEST MODE ONLY.",
        )

    # --- Guard 2: exact hold confirmation phrase --------------------------
    if confirmation_phrase != _TEST_HOLD_BOOKING_CONFIRMATION:
        return _booking_error(
            offer_id,
            "Refusing to hold: confirmation_phrase must be exactly "
            f"'{_TEST_HOLD_BOOKING_CONFIRMATION}'.",
        )

    # --- Fetch the latest offer (read-only) -------------------------------
    offer = _fetch_offer(offer_id)
    if not offer:
        return _booking_error(
            offer_id,
            "The offer is unavailable. It may have expired or no longer "
            "exists. Search again for a fresh offer.",
        )

    # --- Guard 3: offer must be a test (sandbox) offer --------------------
    if offer.get("live_mode") is not False:
        return _booking_error(
            offer_id,
            "Refusing to hold: offer live_mode is not false. This tool only "
            "creates TEST MODE hold orders.",
        )

    # --- Guard 4: hold requires that instant payment is NOT required ------
    payment_reqs = offer.get("payment_requirements") or {}
    if payment_reqs.get("requires_instant_payment") is not False:
        return _booking_error(
            offer_id,
            "Refusing to hold: this offer requires instant payment, so it "
            "cannot be held without paying.",
        )

    # --- Guard 5: offer must not be expired -------------------------------
    if _offer_is_expired(offer):
        return _booking_error(
            offer_id,
            "Refusing to hold: the offer has expired. Search again for a "
            "fresh offer.",
        )

    # --- Guard 6: no passport/identity-document handling yet --------------
    if offer.get("passenger_identity_documents_required") is True:
        return _booking_error(
            offer_id,
            "Refusing to hold: this offer requires passenger identity "
            "documents, which are not implemented yet.",
        )

    # --- Guard 7: exactly one passenger id from the offer -----------------
    passengers = offer.get("passengers") or []
    passenger_ids = [p.get("id") for p in passengers if p.get("id")]
    if len(passenger_ids) != 1:
        return _booking_error(
            offer_id,
            "Refusing to hold: this tool supports exactly one adult "
            f"passenger, but the offer has {len(passenger_ids)}.",
        )
    passenger_id = passenger_ids[0]

    # --- Basic passenger-data validation ----------------------------------
    required_fields = {
        "given_name": given_name,
        "family_name": family_name,
        "born_on": born_on,
        "gender": gender,
        "title": title,
        "email": email,
        "phone_number": phone_number,
    }
    missing = [name for name, value in required_fields.items() if not str(value).strip()]
    if missing:
        return _booking_error(
            offer_id,
            f"Refusing to hold: missing passenger data: {', '.join(missing)}.",
        )

    # --- Build the HOLD order payload (TEST MODE, NO payment) -------------
    # type="hold" reserves the fare without paying. There is deliberately NO
    # "payments" key, and no services / baggage / seats are added.
    order_body = {
        "data": {
            "type": "hold",
            "selected_offers": [offer_id],
            "passengers": [
                {
                    "id": passenger_id,
                    "given_name": given_name,
                    "family_name": family_name,
                    "born_on": born_on,
                    "gender": gender,
                    "title": title,
                    "email": email,
                    "phone_number": phone_number,
                }
            ],
        }
    }

    # --- Create the Duffel TEST hold order --------------------------------
    try:
        response = httpx.post(
            DUFFEL_ORDERS_URL,
            headers=_duffel_headers(),
            json=order_body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return _booking_error(
            offer_id,
            f"Hold order creation failed (HTTP {response.status_code}): "
            f"{_format_duffel_errors(payload)}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    order = payload.get("data") or {}
    if not order.get("id"):
        return _booking_error(
            offer_id,
            "Hold order creation failed: Duffel did not return an order id.",
        )

    # Pull flight details from the created order for a friendly summary.
    summary = _simplify_offer(order)
    owner = order.get("owner") or {}
    airline = owner.get("name") or owner.get("iata_code") or summary["airline"]

    return {
        "order_id": order.get("id"),
        "booking_reference": order.get("booking_reference"),
        "live_mode": order.get("live_mode"),
        "offer_id": offer_id,
        "airline": airline,
        "origin": summary["origin"],
        "destination": summary["destination"],
        "departure_time": summary["departure_time"],
        "arrival_time": summary["arrival_time"],
        "total_amount": order.get("total_amount"),
        "total_currency": order.get("total_currency"),
        "payment_required_by": (
            (order.get("payment_status") or {}).get("payment_required_by")
        ),
        "payment_status": order.get("payment_status"),
        "booking_status": "TEST_HOLD_BOOKING_CREATED",
        "message": (
            "This is a Duffel test-mode hold booking. No real flight was "
            "booked and no real payment was made."
        ),
    }


# =============================================================================
# READ-ONLY ORDER STATUS TOOL
# =============================================================================
# get_test_order below is strictly READ-ONLY. It performs a single
# GET /air/orders/{order_id} and never creates a payment, modifies an order,
# cancels an order, or creates a booking. It is restricted to TEST MODE:
# the token must start with "duffel_test_" and the order must have
# live_mode == false.
# =============================================================================


def _is_past(iso_timestamp: Optional[str]) -> bool:
    """Return True if an ISO 8601 timestamp (may end in 'Z') is in the past."""
    if not iso_timestamp:
        return False
    from datetime import datetime, timezone

    try:
        when = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return when <= datetime.now(timezone.utc)


def _derive_order_status(order: dict[str, Any]) -> str:
    """Map a Duffel order into a clear, human-readable test status.

    Precedence: cancelled > paid > expired (hold past deadline) > awaiting.
    """
    if order.get("cancelled_at"):
        return "TEST_ORDER_CANCELLED"

    payment_status = order.get("payment_status") or {}
    if payment_status.get("paid_at"):
        return "TEST_ORDER_PAID"

    payment_required_by = payment_status.get("payment_required_by")
    awaiting = payment_status.get("awaiting_payment")
    if awaiting and _is_past(payment_required_by):
        return "TEST_ORDER_EXPIRED"

    if awaiting:
        return "TEST_HOLD_AWAITING_PAYMENT"

    # Fallback when Duffel gives an unexpected combination.
    return "TEST_ORDER_UNKNOWN"


@mcp.tool()
def get_test_order(order_id: str) -> dict:
    """Retrieve the latest state of a Duffel TEST-MODE order (READ-ONLY).

    Call this to check on an order previously created by
    create_test_hold_booking or create_test_booking, for example to see
    whether a hold is still awaiting payment, has been paid, cancelled, or
    has expired. It performs a single GET /air/orders/{order_id}.

    This tool NEVER creates a payment, modifies an order, cancels an order,
    or creates a booking. It is restricted to TEST MODE: it refuses unless
    DUFFEL_ACCESS_TOKEN starts with "duffel_test_" and the order has
    live_mode == false.

    Args:
        order_id: The Duffel order id (e.g. "ord_...") from a prior test
            booking.

    Returns:
        On success, a simplified dictionary with: order_id,
        booking_reference, live_mode, total_amount, total_currency,
        payment_required_by, price_guaranteed_expires_at, awaiting_payment,
        paid_at, cancelled_at, airline, origin, destination,
        departure_time, arrival_time, passenger_name, and order_status
        (one of TEST_HOLD_AWAITING_PAYMENT, TEST_ORDER_PAID,
        TEST_ORDER_CANCELLED, TEST_ORDER_EXPIRED).

        On any refusal or error, a dict with order_status "ERROR", the
        order_id, and an "error" explaining why. No order is modified.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing or the HTTP request
            fails at the transport level.
    """

    def _error(message: str) -> dict:
        return {"order_id": order_id, "order_status": "ERROR", "error": message}

    # --- Guard 1: token must be a TEST token ------------------------------
    token = _get_token()
    if not token.startswith(_DUFFEL_TEST_TOKEN_PREFIX):
        return _error(
            "Refusing: DUFFEL_ACCESS_TOKEN is not a test token (must start "
            "with 'duffel_test_'). This tool is TEST MODE ONLY."
        )

    # --- Read-only GET of the order ---------------------------------------
    try:
        response = httpx.get(
            f"{DUFFEL_ORDERS_URL}/{order_id}",
            headers=_duffel_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if response.status_code == 404:
        return _error(
            "Order not found. The order id may be invalid or unavailable."
        )

    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return _error(
            f"Duffel API error (HTTP {response.status_code}): "
            f"{_format_duffel_errors(payload)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    order = payload.get("data") or {}
    if not order.get("id"):
        return _error("Order not found or returned empty by Duffel.")

    # --- Guard 2: must be a TEST order ------------------------------------
    if order.get("live_mode") is not False:
        return _error(
            "Refusing: order live_mode is not false. This tool only reads "
            "TEST MODE orders."
        )

    # --- Build the simplified, read-only summary --------------------------
    summary = _simplify_offer(order)
    owner = order.get("owner") or {}
    airline = owner.get("name") or owner.get("iata_code") or summary["airline"]

    payment_status = order.get("payment_status") or {}

    passengers = order.get("passengers") or []
    if passengers:
        p = passengers[0]
        passenger_name = (
            f"{p.get('given_name', '')} {p.get('family_name', '')}".strip()
        )
    else:
        passenger_name = ""

    return {
        "order_id": order.get("id", order_id),
        "booking_reference": order.get("booking_reference"),
        "live_mode": order.get("live_mode"),
        "total_amount": order.get("total_amount"),
        "total_currency": order.get("total_currency"),
        "payment_required_by": payment_status.get("payment_required_by"),
        "price_guaranteed_expires_at": payment_status.get(
            "price_guarantee_expires_at"
        ),
        "awaiting_payment": payment_status.get("awaiting_payment"),
        "paid_at": payment_status.get("paid_at"),
        "cancelled_at": order.get("cancelled_at"),
        "airline": airline,
        "origin": summary["origin"],
        "destination": summary["destination"],
        "departure_time": summary["departure_time"],
        "arrival_time": summary["arrival_time"],
        "passenger_name": passenger_name,
        "order_status": _derive_order_status(order),
    }


# =============================================================================
# TEST-MODE PAYMENT TOOL
# =============================================================================
# WARNING: pay_test_order below pays an existing HOLD order via
# POST /air/payments using the Duffel test-account BALANCE (never a card).
# It is hard-restricted to Duffel TEST MODE ONLY:
#   - the access token MUST start with "duffel_test_"
#   - the fetched order MUST have live_mode == false
#   - the caller MUST pass the exact confirmation phrase
#   - amount/currency are taken ONLY from the fetched order, never the caller
# In test mode this charges NO real money. It must NEVER be pointed at a live
# token. This tool does NOT implement live payment, card payment,
# cancellation, refunds, or changes.
# =============================================================================


@mcp.tool()
def pay_test_order(order_id: str, confirmation_phrase: str) -> dict:
    """Pay a Duffel TEST-MODE hold order from test balance. TEST MODE ONLY.

    !!! TEST MODE ONLY !!!
    This settles an existing hold order via POST /air/payments using the
    Duffel test-account balance (type "balance", never a card). It refuses
    unless ALL of these hold:
      * DUFFEL_ACCESS_TOKEN starts with "duffel_test_"
      * the fetched order has live_mode == false
      * confirmation_phrase exactly equals "CONFIRM_TEST_PAYMENT"
      * the order is still awaiting payment, not paid, not cancelled, and
        its payment_required_by has not passed

    The payment amount and currency are read ONLY from the freshly fetched
    order, never from the caller. It does NOT implement live payment, card
    payment, cancellation, refunds, or changes.

    Args:
        order_id: The Duffel order id to pay (e.g. "ord_...").
        confirmation_phrase: Must be exactly "CONFIRM_TEST_PAYMENT" or the
            tool refuses to pay.

    Returns:
        On success, a dict with payment_id, payment_status, payment_live_mode,
        order_id, booking_reference, total_amount, total_currency,
        awaiting_payment, paid_at, live_mode, order_status
        ("TEST_ORDER_PAID" only if the refreshed order confirms payment), and
        a message stating no real money was charged.

        On any refusal or error, a dict with order_status other than
        TEST_ORDER_PAID and an "error" explaining why. No payment is made in
        that case.

    Raises:
        DuffelError: If DUFFEL_ACCESS_TOKEN is missing or an HTTP request
            fails at the transport level.
    """

    def _error(message: str, status: str = "ERROR") -> dict:
        return {"order_id": order_id, "order_status": status, "error": message}

    # --- Guard 1: exact confirmation phrase -------------------------------
    if confirmation_phrase != _TEST_PAYMENT_CONFIRMATION:
        return _error(
            "Refusing to pay: confirmation_phrase must be exactly "
            f"'{_TEST_PAYMENT_CONFIRMATION}'."
        )

    # --- Guard 2: token must be a TEST token ------------------------------
    token = _get_token()
    if not token.startswith(_DUFFEL_TEST_TOKEN_PREFIX):
        return _error(
            "Refusing to pay: DUFFEL_ACCESS_TOKEN is not a test token (must "
            "start with 'duffel_test_'). This tool is TEST MODE ONLY."
        )

    # --- Fetch the latest order (read-only) -------------------------------
    try:
        response = httpx.get(
            f"{DUFFEL_ORDERS_URL}/{order_id}",
            headers=_duffel_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if response.status_code == 404:
        return _error("Order not found. The order id may be invalid.")
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return _error(
            f"Duffel API error (HTTP {response.status_code}): "
            f"{_format_duffel_errors(payload)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    order = payload.get("data") or {}
    if not order.get("id"):
        return _error("Order not found or returned empty by Duffel.")

    # --- Guard 3: must be a TEST order ------------------------------------
    if order.get("live_mode") is not False:
        return _error(
            "Refusing to pay: order live_mode is not false. This tool only "
            "pays TEST MODE orders."
        )

    # --- Guard 4: order must be payable -----------------------------------
    payment_status = order.get("payment_status") or {}
    if order.get("cancelled_at"):
        return _error("Refusing to pay: order is cancelled.", "TEST_ORDER_CANCELLED")
    if payment_status.get("paid_at"):
        return _error("Refusing to pay: order is already paid.", "TEST_ORDER_PAID")
    if payment_status.get("awaiting_payment") is False:
        return _error("Refusing to pay: order is not awaiting payment.")
    if _is_past(payment_status.get("payment_required_by")):
        return _error(
            "Refusing to pay: payment_required_by has passed; the hold has "
            "expired.",
            "TEST_ORDER_EXPIRED",
        )

    # --- Read amount/currency from the ORDER only (never the caller) ------
    total_amount = order.get("total_amount")
    total_currency = order.get("total_currency")
    if not total_amount or not total_currency:
        return _error("Refusing to pay: order is missing a current price.")

    # --- Create the TEST payment (balance, not a card) --------------------
    payment_body = {
        "data": {
            "order_id": order_id,
            "payment": {
                "type": "balance",
                "amount": total_amount,
                "currency": total_currency,
            },
        }
    }

    try:
        pay_response = httpx.post(
            DUFFEL_PAYMENTS_URL,
            headers=_duffel_headers(),
            json=payment_body,
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DuffelError(f"Failed to reach Duffel API: {exc}") from exc

    if pay_response.status_code >= 400:
        try:
            err_payload = pay_response.json()
        except ValueError:
            err_payload = {}
        # Surface Duffel's specific payment error codes clearly (e.g.
        # already_paid, already_cancelled, past_payment_required_by_date,
        # schedule_changed, price_changed,
        # payment_amount_does_not_match_order_amount,
        # payment_currency_does_not_match_order_currency).
        return _error(
            f"Payment failed (HTTP {pay_response.status_code}): "
            f"{_format_duffel_errors(err_payload)}"
        )

    try:
        pay_payload = pay_response.json()
    except ValueError as exc:
        raise DuffelError("Duffel API returned a non-JSON response.") from exc

    payment = pay_payload.get("data") or {}

    # --- Refresh the order to confirm payment landed ----------------------
    refreshed = _fetch_order_raw(order_id)
    refreshed_payment_status = (refreshed.get("payment_status") or {})
    paid_at = refreshed_payment_status.get("paid_at")
    awaiting_payment = refreshed_payment_status.get("awaiting_payment")

    # Only mark PAID if the refreshed order actually confirms it.
    order_status = "TEST_ORDER_PAID" if paid_at else _derive_order_status(refreshed)

    return {
        "payment_id": payment.get("id"),
        "payment_status": payment.get("status") or refreshed_payment_status,
        "payment_live_mode": payment.get("live_mode"),
        "order_id": refreshed.get("id", order_id),
        "booking_reference": refreshed.get("booking_reference"),
        "total_amount": refreshed.get("total_amount", total_amount),
        "total_currency": refreshed.get("total_currency", total_currency),
        "awaiting_payment": awaiting_payment,
        "paid_at": paid_at,
        "live_mode": refreshed.get("live_mode"),
        "order_status": order_status,
        "message": (
            "This was a Duffel test-mode payment. No real money was charged."
        ),
    }


def _fetch_order_raw(order_id: str) -> dict[str, Any]:
    """Fetch a single order object (read-only). Returns {} on any failure.

    Used to refresh order state after a payment. Never raises for HTTP-level
    errors here so the caller can still report the payment it just made.
    """
    try:
        response = httpx.get(
            f"{DUFFEL_ORDERS_URL}/{order_id}",
            headers=_duffel_headers(),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        return {}
    if response.status_code >= 400:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload.get("data") or {}


if __name__ == "__main__":
    # Run the server over stdio (the default transport), the standard way a
    # local MCP client such as an IDE or desktop agent launches this server.
    mcp.run()
