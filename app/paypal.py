"""PayPal sandbox client, plus an in-process simulator for the demo.

Only Till constructs this client. The shopping agent has no credentials and
cannot create, read, capture, or void an order.
"""

from __future__ import annotations

import base64
import copy
import uuid
import zlib
from urllib.parse import urlparse

import httpx

from .money import from_minor, to_minor

PAYPAL_CERT_HOSTS = {
    "api.paypal.com",
    "api.sandbox.paypal.com",
    "api-m.paypal.com",
    "api-m.sandbox.paypal.com",
}


def certificate_url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        return False
    if host not in PAYPAL_CERT_HOSTS:
        return False
    return parsed.path.startswith("/") and parsed.path != "/"


class PayPalError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class PayPalClient:
    def __init__(self, mode: str, base: str, client_id: str, secret: str, webhook_id: str):
        self.mode = mode
        self.base = base.rstrip("/")
        self.client_id = client_id
        self.secret = secret
        self.webhook_id = webhook_id
        self._orders: dict[str, dict] = {}
        self._by_request: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.tamper_read_value: str | None = None
        self.tamper_sku: str | None = None
        self.tamper_quantity: str | None = None
        self.tamper_currency: str | None = None

    def create_order(
        self,
        *,
        custom_id: str,
        currency: str,
        payable_minor: int,
        item_minor: int,
        tax_minor: int,
        shipping_minor: int,
        addons_minor: int = 0,
        title: str,
        sku: str,
        quantity: int,
        request_id: str,
    ) -> dict:
        self.calls.append(("create", custom_id, request_id))
        if self.mode == "simulate" and request_id in self._by_request:
            return copy.deepcopy(self._orders[self._by_request[request_id]])
        value = from_minor(payable_minor)
        unit = from_minor(item_minor)
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "custom_id": custom_id[:127],
                    "invoice_id": request_id[:127],
                    "amount": {
                        "currency_code": currency,
                        "value": value,
                        "breakdown": {
                            "item_total": {
                                "currency_code": currency,
                                "value": from_minor(item_minor * quantity),
                            },
                            "tax_total": {"currency_code": currency, "value": from_minor(tax_minor)},
                            "shipping": {"currency_code": currency, "value": from_minor(shipping_minor)},
                            "handling": {"currency_code": currency, "value": from_minor(addons_minor)},
                        },
                    },
                    "items": [
                        {
                            "name": title[:127],
                            "sku": sku[:127],
                            "quantity": str(quantity),
                            "unit_amount": {"currency_code": currency, "value": unit},
                            "category": "PHYSICAL_GOODS",
                        }
                    ],
                }
            ],
        }
        if self.mode == "simulate":
            order_id = "SIM" + uuid.uuid4().hex[:14].upper()
            order = {"id": order_id, "status": "APPROVED", **body}
            self._orders[order_id] = order
            self._by_request[request_id] = order_id
            return copy.deepcopy(order)
        return self._request("POST", "/v2/checkout/orders", body, request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        self.calls.append(("read", order_id))
        if self.mode == "simulate":
            if order_id not in self._orders:
                raise PayPalError("PayPal order was not found.")
            order = copy.deepcopy(self._orders[order_id])
            unit = order["purchase_units"][0]
            if self.tamper_read_value is not None:
                unit["amount"]["value"] = self.tamper_read_value
            if self.tamper_currency is not None:
                unit["amount"]["currency_code"] = self.tamper_currency
            if self.tamper_sku is not None:
                unit["items"][0]["sku"] = self.tamper_sku
            if self.tamper_quantity is not None:
                unit["items"][0]["quantity"] = str(self.tamper_quantity)
            return order
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture(self, order_id: str, request_id: str) -> dict:
        self.calls.append(("capture", order_id, request_id))
        if self.mode == "simulate":
            order = self._orders[order_id]
            if order.get("status") == "COMPLETED":
                raise PayPalError("This PayPal order was already captured.")
            if order.get("status") == "VOIDED":
                raise PayPalError("This PayPal order is void.")
            capture_id = "CAP" + uuid.uuid4().hex[:14].upper()
            unit = order["purchase_units"][0]
            capture = {
                "id": capture_id,
                "status": "COMPLETED",
                "amount": unit["amount"],
                "custom_id": unit.get("custom_id"),
                "supplementary_data": {"related_ids": {"order_id": order_id}},
            }
            order["status"] = "COMPLETED"
            order["capture"] = capture
            return copy.deepcopy(capture)
        return self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            {},
            request_id=request_id,
        )

    def void(self, order_id: str) -> dict:
        self.calls.append(("void", order_id))
        if self.mode == "simulate":
            if order_id in self._orders:
                self._orders[order_id]["status"] = "VOIDED"
            return {"voided": True}
        try:
            return self._request("POST", f"/v2/checkout/orders/{order_id}/void", {})
        except PayPalError:
            order = self.get_order(order_id)
            voided = False
            for unit in order.get("purchase_units") or []:
                payments = unit.get("payments") or {}
                for auth in payments.get("authorizations") or []:
                    auth_id = auth.get("id")
                    if auth_id and auth.get("status") not in {"VOIDED", "CAPTURED"}:
                        self._request("POST", f"/v2/payments/authorizations/{auth_id}/void", {})
                        voided = True
            return {"voided": voided, "status": order.get("status")}

    def verify_webhook(self, headers: dict, event: dict) -> bool:
        """PayPal's verify-webhook-signature API. Certificate URL must be PayPal's.

        The HTTP route uses local signature verification instead, so the demo
        does not need a public callback. This method remains the sandbox API path.
        """
        lowered = {str(key).lower(): value for key, value in headers.items()}
        if self.mode == "simulate":
            return lowered.get("x-till-simulate") == "1"
        cert_url = lowered.get("paypal-cert-url") or ""
        if not certificate_url_allowed(cert_url):
            return False
        if not self.webhook_id or not self.client_id or not self.secret:
            return False
        body = {
            "transmission_id": lowered.get("paypal-transmission-id"),
            "transmission_time": lowered.get("paypal-transmission-time"),
            "cert_url": cert_url,
            "auth_algo": lowered.get("paypal-auth-algo"),
            "transmission_sig": lowered.get("paypal-transmission-sig"),
            "webhook_id": self.webhook_id,
            "webhook_event": event,
        }
        result = self._request("POST", "/v1/notifications/verify-webhook-signature", body)
        return result.get("verification_status") == "SUCCESS"

    def _token(self) -> str:
        response = httpx.post(
            f"{self.base}/v1/oauth2/token",
            auth=(self.client_id, self.secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=20.0,
        )
        if response.status_code >= 400:
            raise PayPalError("PayPal refused the sandbox credentials.")
        return response.json()["access_token"]

    def _request(self, method: str, path: str, body: dict | None = None, request_id: str | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id[:38]
        response = httpx.request(method, f"{self.base}{path}", headers=headers, json=body, timeout=30.0)
        if response.status_code >= 400:
            raise PayPalError(f"PayPal {method} {path} failed ({response.status_code}).")
        if not response.content:
            return {}
        return response.json()


def build_paypal(mode: str, base: str, client_id: str, secret: str, webhook_id: str) -> PayPalClient:
    if client_id and secret and mode == "sandbox":
        return PayPalClient("sandbox", base, client_id, secret, webhook_id)
    return PayPalClient("simulate", base, client_id, secret, webhook_id)


def fetch_paypal_cert(url: str) -> bytes:
    if not certificate_url_allowed(url):
        raise PayPalError("Certificate URL is not PayPal's.")
    response = httpx.get(url, follow_redirects=False, timeout=10.0)
    if response.status_code != 200:
        raise PayPalError("Certificate download failed.")
    return response.content


def verify_webhook_signature(headers: dict, raw_body: bytes, webhook_id: str, fetch_cert) -> bool:
    """Real signature check. The certificate URL must be an official PayPal host."""
    lowered = {str(key).lower(): value for key, value in headers.items()}
    cert_url = lowered.get("paypal-cert-url") or ""
    if not certificate_url_allowed(cert_url):
        return False
    transmission_id = lowered.get("paypal-transmission-id") or ""
    transmission_time = lowered.get("paypal-transmission-time") or ""
    signature = lowered.get("paypal-transmission-sig") or ""
    algo = (lowered.get("paypal-auth-algo") or "").upper()
    if not transmission_id or not transmission_time or not signature or not webhook_id:
        return False
    if algo and "SHA256" not in algo:
        return False
    try:
        cert_pem = fetch_cert(cert_url)
        if isinstance(cert_pem, str):
            cert_pem = cert_pem.encode()
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.x509 import load_pem_x509_certificate

        certificate = load_pem_x509_certificate(cert_pem)
        crc = zlib.crc32(raw_body) & 0xFFFFFFFF
        message = f"{transmission_id}|{transmission_time}|{webhook_id}|{crc}".encode()
        certificate.public_key().verify(
            base64.b64decode(signature),
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def amount_minor(order_or_capture: dict) -> tuple[str, int] | None:
    if "amount" in order_or_capture and isinstance(order_or_capture["amount"], dict) and "value" in order_or_capture["amount"]:
        amount = order_or_capture["amount"]
        return amount["currency_code"], to_minor(str(amount["value"]))
    units = order_or_capture.get("purchase_units") or []
    if not units:
        return None
    amount = units[0].get("amount") or {}
    if "value" not in amount:
        return None
    return amount["currency_code"], to_minor(str(amount["value"]))


def custom_id_of(payload: dict) -> str | None:
    if payload.get("custom_id"):
        return payload["custom_id"]
    units = payload.get("purchase_units") or []
    if units and units[0].get("custom_id"):
        return units[0]["custom_id"]
    return None


def paypal_order_id_of(payload: dict) -> str | None:
    related = (payload.get("supplementary_data") or {}).get("related_ids") or {}
    if related.get("order_id"):
        return related["order_id"]
    if payload.get("purchase_units"):
        return payload.get("id")
    return None


def paypal_line(payload: dict) -> dict | None:
    """SKU, quantity, unit, currency, and total read back from an order or capture."""
    units = payload.get("purchase_units") or []
    if units:
        unit = units[0]
        item = (unit.get("items") or [{}])[0]
        parsed = amount_minor(payload)
        if parsed is None or "unit_amount" not in item:
            return None
        currency, total = parsed
        unit_currency = item["unit_amount"]["currency_code"]
        return {
            "sku": item.get("sku"),
            "quantity": int(item.get("quantity") or 0),
            "unit_price_minor": to_minor(str(item["unit_amount"]["value"])),
            "currency": currency,
            "unit_currency": unit_currency,
            "total_minor": total,
            "custom_id": unit.get("custom_id"),
        }
    parsed = amount_minor(payload)
    if parsed is None:
        return None
    currency, total = parsed
    return {
        "sku": payload.get("sku"),
        "quantity": None,
        "unit_price_minor": None,
        "currency": currency,
        "unit_currency": currency,
        "total_minor": total,
        "custom_id": payload.get("custom_id"),
    }
