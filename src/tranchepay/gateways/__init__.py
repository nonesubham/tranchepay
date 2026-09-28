"""Gateway adapters: one per supported payment provider.

Every adapter implements :class:`~tranchepay.protocol.PaymentGateway`, so the
composer treats them interchangeably. They are normally obtained from
:class:`tranchepay.GatewayFactory` rather than imported directly.
"""

from __future__ import annotations

from .base import GatewayRequest, HttpGatewayAdapter
from .paytm_adapter import PaytmAdapter
from .phonepe_adapter import PhonePeAdapter
from .razorpay_adapter import RazorpayAdapter, RazorpayClientProtocol

__all__ = [
    "GatewayRequest",
    "HttpGatewayAdapter",
    "PaytmAdapter",
    "PhonePeAdapter",
    "RazorpayAdapter",
    "RazorpayClientProtocol",
]
