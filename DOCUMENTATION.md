# tranchepay Documentation

`tranchepay` is a small Python library that adds three payment modes to a payment
gateway you already have: charge the exact amount, gross the amount up so the
customer covers the gateway fee, or collect one large amount as a sequence of
tranches of at most ₹1,999. Razorpay, Paytm and PhonePe ship as adapters today.

It is a **composition** library, not a client. You build and configure the provider
client yourself (or let the factory do it) and hand the adapter to `tranchepay`:

- **Composition over inheritance.** `tranchepay` never subclasses, monkey-patches,
  or forks a provider SDK. An adapter holds a reference to your instance and calls
  only its public surface.
- **You own the credentials.** Your keys, timeouts, proxies, retry policy, logging,
  and version pinning are unchanged. Swapping `tranchepay` in or out does not change
  how your application talks to the provider.
- **One narrow interface.** Everything above the adapters is written against the
  `PaymentGateway` protocol, so the composer, the split flow and the recovery code
  never name a provider.
- **Integer paise everywhere.** Amounts are `int` paise and fee rates are
  `decimal.Decimal`. A `float` is rejected rather than silently rounded.
- **No hidden arithmetic.** Gross-up rounding is an explicit configuration value.

Because the composer only needs a `PaymentGateway`, `tranchepay` is also easy to
test: a stub gateway satisfies it structurally, so the default test suite makes no
network calls.

## Contents

- [Introduction](#introduction)
- [Installation](#installation)
- [Initialization](#initialization)
- [Gateways & the Factory](#gateways--the-factory)
- [PhonePe Integration](#phonepe-integration)
- [Usage Guide](#usage-guide)
  - [Mode 1: EXACT](#mode-1-exact--the-merchant-absorbs-the-fee)
  - [Mode 2: WITH_CHARGES](#mode-2-with_charges--the-customer-covers-the-fee)
  - [Mode 3: SPLIT](#mode-3-split--sequential-tranches)
- [Handling Partial Failures & Aborts](#handling-partial-failures--aborts)
- [Webhook Verification](#webhook-verification)
- [Error Handling](#error-handling)
- [Architecture & Session Store](#architecture--session-store)
- [Compliance & Disclaimers](#compliance--disclaimers)

## Introduction

Three modes, one composer:

| Mode | What the customer is charged | Use it for |
| --- | --- | --- |
| `PaymentMode.EXACT` | Exactly `amount_paise` | The default. The merchant absorbs the gateway fee. |
| `PaymentMode.WITH_CHARGES` | `round(amount_paise / (1 - fee_rate))` | Passing the gateway fee on to the customer, where lawful. |
| `PaymentMode.SPLIT` | Sequential tranches of at most `tranche_paise` | Collecting a large amount in pieces, e.g. to stay under a UPI MDR threshold. |

All three return an `OrderResult` wrapping the untouched provider response, so
anything the SDK gives you is still there under `.raw`.

## Installation

```bash
pip install tranchepay
```

Requirements:

- Python 3.10 or newer.
- `razorpay>=1.4` — installed automatically as a dependency.
- `pydantic>=2.6` — installed automatically.

Razorpay is the reference adapter and is installed as a dependency. The Paytm and
PhonePe adapters build their HTTP requests with `httpx`, which is an optional extra:
install it if you will drive those gateways.

```bash
pip install "tranchepay[http]"
```

The optional development extras add the test and type-checking toolchain
(`pytest`, `pytest-cov`, `ruff`, `mypy`, `python-dotenv`, `httpx`).

Working on tranchepay itself? Clone it and install an editable copy with those
extras:

```bash
git clone https://github.com/nonesubham/tranchepay.git
cd tranchepay
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Or install a specific revision straight from GitHub:

```bash
pip install "tranchepay @ git+https://github.com/nonesubham/tranchepay.git"
```

## Initialization

Ask the factory for a gateway, then hand it to the composer. The factory is
tranchepay's answer to JDBC's `DriverManager`: you name a provider, it returns the
adapter, and nothing downstream knows which one it got.

```python
from decimal import Decimal

from tranchepay import ChargesConfig, GatewayFactory, PaymentComposer, SplitConfig

# 1. Ask for the adapter. Credentials are read from your config, never hardcoded.
gateway = GatewayFactory.get_gateway(
    "razorpay",
    {"key_id": "rzp_test_xxxxxxxxxxxx", "key_secret": "your_key_secret"},
)

# 2. Hand it to the composer. The adapter is stored as-is and never mutated.
composer = PaymentComposer(
    gateway,
    charges=ChargesConfig(fee_rate=Decimal("0.0236")),  # only needed for WITH_CHARGES
    split=SplitConfig(tranche_paise=199_900),  # only needed for SPLIT
)

composer.gateway is gateway  # True - the same object you passed in
```

Constructing an adapter performs no I/O; the first API call happens when the
composer uses it. If your application already owns a configured client, wrap it
directly or pass it to the factory:

```python
import razorpay

from tranchepay import RazorpayAdapter

client = razorpay.Client(auth=("rzp_test_xxxxxxxxxxxx", "your_key_secret"))

gateway = RazorpayAdapter(client)  # the client is stored as-is and never mutated
# ...or, equivalently:
gateway = GatewayFactory.get_gateway("razorpay", {"client": client})
```

`PaymentComposer` signature:

```python
class PaymentComposer:
    def __init__(
        self,
        gateway,  # any PaymentGateway: a built-in adapter or your own
        *,
        charges: ChargesConfig | None = None,
        split: SplitConfig | None = None,
        store: SessionStore | None = None,
        currency: str = "INR",
    ) -> None: ...
```

- `charges` is required only to use `WITH_CHARGES`; using that mode without it
  raises `PaymentComposeError`.
- `split` defaults to `SplitConfig()` (₹1,999). See
  [Compliance & Disclaimers](#compliance--disclaimers).
- `store` defaults to an in-process `InMemorySessionStore`. See
  [Architecture & Session Store](#architecture--session-store) for production.
- `currency` is the default for every order this composer creates; each
  `create_order` call may override it.

The constructor validates the gateway structurally and raises `TypeError` if the
object does not implement the `PaymentGateway` methods. A raw provider client is
*not* a gateway: wrap it in an adapter first. This is a guard against passing a
misconfigured or unrelated object, not a subclass check.

## Gateways & the Factory

Every provider is reached through one protocol, and every adapter is obtained
through one factory. That is what lets the same composer, split flow and recovery
code drive Razorpay, Paytm and PhonePe without a single provider-specific branch.

### The `PaymentGateway` protocol

An adapter is anything that implements these five methods. Amounts are always
integer paise, and adapters return the provider's response payload unchanged so no
field tranchepay does not read is lost.

```python
from typing import Protocol


class PaymentGateway(Protocol):
    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str | None = None,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def fetch_order(self, order_id: str) -> dict[str, Any]: ...

    def fetch_payment(self, payment_id: str) -> dict[str, Any]: ...

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool: ...

    def refund_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]: ...
```

### The shipped adapters

| Provider | Adapter | Credentials | Status |
| --- | --- | --- | --- |
| Razorpay | `RazorpayAdapter` | `key_id`, `key_secret` (or a pre-built `client`) | Reference implementation, fully supported. |
| Paytm | `PaytmAdapter` | `merchant_id`, `merchant_key` (optional `website`, `callback_url`) | Structural: request building is complete; `execute()` must be implemented for your account. |
| PhonePe | `PhonePeAdapter` | `merchant_id`, `salt_key` (optional `salt_index`, `env`, `auth_mode`) | Implemented: OAuth Standard Checkout v2 by default, classic X-VERIFY also supported. See [PhonePe Integration](#phonepe-integration). |

Registering a gateway and using it, with no change to the rest of your code:

```python
from tranchepay import GatewayFactory, PaymentComposer

composer = PaymentComposer(
    GatewayFactory.get_gateway("phonepe", {"merchant_id": "M123", "salt_key": "..."})
)
```

An unknown provider raises `UnsupportedGatewayError`. A missing required credential
raises `GatewayConfigurationError`:

```python
from tranchepay import UnsupportedGatewayError, GatewayConfigurationError

try:
    gateway = GatewayFactory.get_gateway("stripe", {})
except UnsupportedGatewayError as exc:
    ...  # "stripe" is not registered
```

### What "structural" means for Paytm

Paytm does not ship a first-party Python SDK, so its adapter is written directly
against Paytm's REST API. Everything tranchepay needs is implemented and tested
*except* the final HTTP call:

- The class implements the full `PaymentGateway` protocol.
- Credentials are validated on construction.
- Requests are built completely — URL, headers, JSON body, paise-to-rupee
  conversion — and exposed as a `GatewayRequest` you can inspect.
- `execute()` raises `NotImplementedError` until you fill it in, because sending a
  call needs your merchant account's host, signing rules and checksum algorithm.

```python
from tranchepay import PaytmAdapter

paytm = PaytmAdapter({"merchant_id": "M123", "merchant_key": "..."})

request = paytm.refund_request("pay_123", 1_500)
request.method  # "POST"
request.url  # "https://api.paytm.com/v2/refund/apply"
request.json  # {"mid": "M123", "txnId": "pay_123", "refundAmount": {"value": "15.00", ...}}

paytm.create_order(1_500)  # raises NotImplementedError until execute() is written
```

Subclass the adapter and implement `execute()` (or inject an `httpx.Client` and
implement the signing) to go live. The base `HttpGatewayAdapter` handles URL
construction, the lazy `httpx` import, and the `tranchepay[http]` install hint.

PhonePe is different: `PhonePeAdapter` is fully implemented against PhonePe's
REST API and performs live calls. See [PhonePe Integration](#phonepe-integration).

### Registering your own gateway

`register_gateway` is the `DriverManager.registerDriver` analogue. It takes any
callable that accepts the credentials mapping and returns a `PaymentGateway`; an
adapter class whose constructor takes the credentials mapping can be registered
directly.

```python
from tranchepay import GatewayFactory, PaymentComposer


class MyGateway:
    def __init__(self, credentials: dict) -> None:
        self.api_key = credentials["api_key"]

    def create_order(
        self, amount_paise, *, currency=None, receipt=None, notes=None
    ): ...  # call your provider

    def fetch_order(self, order_id): ...
    def fetch_payment(self, payment_id): ...
    def verify_signature(self, order_id, payment_id, signature): ...
    def verify_webhook_signature(self, body, signature, secret): ...
    def refund_payment(self, payment_id, amount_paise): ...


GatewayFactory.register_gateway("my-gateway", MyGateway)
gateway = GatewayFactory.get_gateway("my-gateway", {"api_key": "..."})
composer = PaymentComposer(gateway)
```

Provider names are matched case-insensitively. `GatewayFactory.available_gateways()`
lists everything registered.

## PhonePe Integration

PhonePe does not model "orders" the way Razorpay does, and it ships no first-party
Python SDK. `PhonePeAdapter` talks to PhonePe's REST API directly and maps its flow
onto the `PaymentGateway` protocol, so the composer drives it unchanged.

### The PhonePe flow

1. **Pay request.** Send the amount to PhonePe and get back a `redirectUrl`.
2. **Redirect.** Send the customer to that URL to finish paying.
3. **Status check.** There is no capture call and no checkout signature to verify.
   You learn the outcome by polling the status endpoint with the same
   `merchantTransactionId` you sent.

```text
merchant  ── POST /checkout/v2/pay ───────────▶ PhonePe
merchant  ◀── redirectUrl ───────────────────── PhonePe
customer  ── pays in the browser ─────────────▶ PhonePe
merchant  ── GET /checkout/v2/order/{id}/status ▶ PhonePe   PENDING → COMPLETED
```

Each payment needs a **unique** `merchantTransactionId`. PhonePe rejects a reused
id with `DUPLICATE_TXN_REQUEST`. The split flow generates one per tranche, and
`create_order` generates one when you do not pass `merchant_transaction_id`.

### Configuration

PhonePe has two integration styles and the adapter implements both:

| `auth_mode` | Flow | Endpoints |
| --- | --- | --- |
| `"oauth"` (default) | Standard Checkout v2: the client id and secret are exchanged for a short-lived `O-Bearer` token. | `/checkout/v2/pay`, `/checkout/v2/order/{id}/status` |
| `"x-verify"` | Classic PG: the base64 JSON body is signed with `SHA256(base64 + endpoint + saltKey) + "###" + saltIndex`. | `/pg/v1/pay`, `/pg/v1/status/{merchantId}/{txnId}` |

Credentials come from the environment and are never hardcoded:

```bash
export PHONEPE_MERCHANT_ID=...   # client id
export PHONEPE_SALT_KEY=...      # client secret / salt key
export PHONEPE_SALT_INDEX=1      # optional, defaults to 1
export PHONEPE_ENV=preprod       # optional: preprod (default) or production
```

```python
import os
from tranchepay import GatewayFactory, PaymentComposer, PaymentMode

gateway = GatewayFactory.get_gateway(
    "phonepe",
    {
        "merchant_id": os.environ["PHONEPE_MERCHANT_ID"],
        "salt_key": os.environ["PHONEPE_SALT_KEY"],
        "salt_index": os.environ.get("PHONEPE_SALT_INDEX", "1"),
        "env": os.environ.get("PHONEPE_ENV", "preprod"),
    },
)

composer = PaymentComposer(gateway)

result = composer.create_order(50_000, mode=PaymentMode.EXACT)
redirect_url = result.raw["redirect_url"]  # send the customer here
```

### Webhooks vs. status checks

These are two different things, and PhonePe uses both:

- **Webhook / callback verification.** PhonePe does not sign order/payment pairs;
  it signs payloads. The `X-VERIFY` header is
  `SHA256(payload + saltKey) + "###" + saltIndex`. Verify it with
  `adapter.verify_signature(payload, signature)` (or `verify_webhook_signature`).
- **Payment status.** To confirm a payment actually settled, call
  `adapter.fetch_payment(merchant_transaction_id)` and read `state` / `status`.

```python
# Incoming PhonePe callback:
body = request.get_data(as_text=True)  # the exact bytes PhonePe signed
signature = request.headers["X-VERIFY"]

if gateway.verify_signature(body, signature):
    status = gateway.fetch_payment(merchant_transaction_id)
    if status["state"] == "COMPLETED":
        ...  # settle the order
```

### Refunds

`PhonePeAdapter.refund_payment(...)` raises `NotImplementedError`. PhonePe refunds
go through a separate Refund API with its own signing and state machine, which
this adapter does not model. Refund from the PhonePe dashboard, then reconcile the
session.

> The composer's `abort_and_refund` calls `refund_payment` for every paid tranche,
> so aborting a PhonePe-backed split session raises `NotImplementedError` until
> refunds are supported. Plan for manual reconciliation.

## Usage Guide

All three modes go through one method:

```python
def create_order(
    amount_paise: int,  # int, positive
    *,
    mode: PaymentMode = PaymentMode.EXACT,
    receipt: str | None = None,  # echoed back by Razorpay
    notes: Mapping[str, Any] | None = None,  # echoed back by Razorpay
    currency: str | None = None,  # per-order override
) -> OrderResult: ...
```

Every order is created with `payment_capture=1`, so a successful payment settles
without a second capture call. `amount_paise` must be an `int`; passing a `float`
raises `TypeError`, and passing zero or a negative value raises `ValueError`.

### Mode 1: EXACT — the merchant absorbs the fee

The customer is charged exactly the amount you ask for.

```python
from tranchepay import PaymentMode

order = composer.create_order(50_000, mode=PaymentMode.EXACT)  # 50_000 paise = ₹500.00

order.order_id  # "order_XXXXXXXXXXXXXX"
order.amount_paise  # 50_000 - exactly what the customer pays
order.net_paise  # None - nothing was grossed up
order.raw  # untouched Razorpay response, ready for Checkout
```

Hand `order.raw` to Razorpay Checkout, or read `order.order_id` if your frontend
builds its own payload. If this is your only use case, you arguably do not need a
library — but it keeps the three modes behind one call site.

### Mode 2: WITH_CHARGES — the customer covers the fee

Pass the amount you want to **net**, and `tranchepay` computes the gross amount to
charge:

```text
gross = round(net / (1 - fee_rate), policy)
```

The fee rate is configuration, not a per-call argument, because it is a property of
your Razorpay account and pricing rather than of an individual order:

```python
from decimal import Decimal

from tranchepay import ChargesConfig, PaymentComposer, PaymentMode, RoundingPolicy

composer = PaymentComposer(
    gateway,
    charges=ChargesConfig(
        fee_rate=Decimal("0.0236"),  # 2.36%, as a Decimal - never a float
        rounding=RoundingPolicy.ROUND_HALF_UP,  # the default
    ),
)

order = composer.create_order(50_000, mode=PaymentMode.WITH_CHARGES)

order.amount_paise  # 51_209 - what the customer is charged
order.net_paise  # 50_000 - what the merchant keeps
order.fee_paise  #  1_209 - the fee, paid by the customer instead of the merchant
```

`fee_rate` is the gateway fee as a fraction of the **gross** amount and must satisfy
`0 <= fee_rate < 1`, or a validation error is raised. Always pass a `Decimal` such
as `Decimal("0.0236")`: pydantic will coerce a numeric or string input, but a
`Decimal` states the intent and keeps the arithmetic exact. (The standalone
`gross_up()` helper is stricter and rejects a `float` outright with `TypeError`.)

#### Rounding policy

The gross-up is evaluated as an exact rational and rounded **once**, at the end.
Which way that single rounding goes is explicit configuration, never implicit:

```python
from tranchepay import RoundingPolicy

RoundingPolicy.ROUND_HALF_UP  # default: nearest paisa, halves up
RoundingPolicy.ROUND_HALF_DOWN
RoundingPolicy.ROUND_HALF_EVEN
RoundingPolicy.ROUND_UP  # customer covers the fee in full
RoundingPolicy.ROUND_CEILING  # synonym of ROUND_UP for non-negative amounts
RoundingPolicy.ROUND_DOWN  # customer is never overcharged
RoundingPolicy.ROUND_FLOOR  # synonym of ROUND_DOWN for non-negative amounts
```

Because the *total* is rounded, the merchant may net up to one paisa more or less
than requested. Choose deliberately:

- `ROUND_UP` / `ROUND_CEILING` — the merchant never under-recovers (may net up to
  one paisa extra per order).
- `ROUND_HALF_UP` — the closest paisa; the merchant may under-recover by less than
  half a paisa.
- `ROUND_DOWN` / `ROUND_FLOOR` — the customer is never overcharged; the merchant
  absorbs up to one paisa.

At an exact half-paisa boundary the policies diverge, which is why the choice is
documented rather than hidden. For `gross_up(3, Decimal("0.6"))` (`3 / 0.4 = 7.5`):

```python
from decimal import Decimal

from tranchepay import RoundingPolicy, gross_up

gross_up(3, Decimal("0.6"), RoundingPolicy.ROUND_HALF_UP)  # 8
gross_up(3, Decimal("0.6"), RoundingPolicy.ROUND_HALF_DOWN)  # 7
gross_up(3, Decimal("0.6"), RoundingPolicy.ROUND_DOWN)  # 7
gross_up(3, Decimal("0.6"), RoundingPolicy.ROUND_UP)  # 8
```

Whatever the policy, the result is reported exactly: `order.amount_paise` is what
was sent to Razorpay and `order.fee_paise` is the implied fee, so you never have to
reconstruct the remainder yourself.

### Mode 3: SPLIT — sequential tranches

A split collects one amount as a series of payments, one customer checkout at a
time. Each tranche is verified independently, and the whole flow is idempotent.

**Planning.** The plan is `divmod(amount_paise, tranche_paise)`: *n* full tranches
plus an optional remainder, which is omitted when it is zero.

```python
from tranchepay import plan_tranches

plan_tranches(450_000).amounts_paise  # (199_900, 199_900, 50_200)  -> ₹1,999 + ₹1,999 + ₹502
plan_tranches(399_800).amounts_paise  # (199_900, 199_900)         -> exact multiple, no remainder
plan_tranches(150_000).amounts_paise  # (150_000,)                 -> fits in one tranche
plan_tranches(150_000).requires_session  # False
```

When the amount fits in a single tranche there is nothing to sequence, so **no
session is created** and one ordinary order is returned for the full amount. Check
`order.session_id is None` to detect this case.

#### Estimating the number of tranches

To tell the customer how many payments to expect *before* creating anything, use
`estimate_tranche_count`. It is pure arithmetic — no order, no session, no API
call — and it is the same math the split engine uses, so the promise and the plan
can never disagree:

```python
from tranchepay import estimate_tranche_count

estimate_tranche_count(450_000)  # 3 - the default ceiling (₹1,999)
estimate_tranche_count(450_000, 50_000)  # 9 - a ₹500 ceiling
estimate_tranche_count(150_000)  # 1 - fits in a single tranche
```

Pass the same ceiling your composer uses (`SplitConfig().tranche_paise`) so the
estimate matches what will actually be created.

Once the session exists, `session.total_tranches` carries the same number, so
progress copy lines up with what you promised:

```python
first = composer.create_order(450_000, mode=PaymentMode.SPLIT)
session = composer.store.get(first.session_id)

session.total_tranches  # 3 - derived from the tranches actually stored
f"Tranche {first.tranche_index + 1} of {session.total_tranches}"  # "Tranche 1 of 3"
```

**Starting a split.** Use `create_order` with `mode=PaymentMode.SPLIT`; it opens the
session and returns the first tranche's order:

```python
from tranchepay import PaymentComposer, PaymentMode, SplitConfig

composer = PaymentComposer(gateway, split=SplitConfig(tranche_paise=199_900))

first = composer.create_order(450_000, mode=PaymentMode.SPLIT, receipt="ord-1043")

first.order_id  # tranche 1's order
first.amount_paise  # 199_900
first.session_id  # "3f1c...": save this; every later call needs it
first.tranche_index  # 0
# Send first.raw to Razorpay Checkout.
```

**Advancing the split.** After each tranche is paid, checkout returns
`razorpay_order_id`, `razorpay_payment_id`, and `razorpay_signature`. Pass them to
`verify_and_advance` from your payment callback or webhook handler:

```python
next_order = composer.verify_and_advance(
    session_id=first.session_id,
    order_id=payload["razorpay_order_id"],
    payment_id=payload["razorpay_payment_id"],
    signature=payload["razorpay_signature"],
)
```

What it does, in order:

1. Verifies the signature through the gateway
   (`gateway.verify_signature`), which delegates to the provider's own verifier.
2. Loads the session and checks that the order belongs to it and is its current
   pending tranche. A replay for an already-paid tranche returns the current
   pending order instead of advancing again.
3. Fetches the payment with `gateway.fetch_payment` and requires `status == "captured"`.
4. Requires the captured amount to equal that tranche's amount, else
   `AmountMismatchError`.
5. Marks the tranche `PAID` and persists the session.
6. If tranches remain, creates the next order (`payment_capture=1`) and returns it.
7. If that was the last tranche, marks the session `COMPLETE` and returns `None`.

**Completion vs. the next order.** The `None` return is the signal that the split is
finished; anything else is an order to send to checkout:

```python
if next_order is None:
    mark_order_fulfilled(session_id)  # every tranche captured
else:
    send_to_checkout(next_order.raw)  # next_order.tranche_index == 1, 2, ...
```

Because the flow is keyed on `(session_id, tranche_index)` and re-reads the session
under a lock, a double-submitted callback or two concurrent webhooks advance a
session **at most once**. A replay of an already-verified tranche returns the
current pending order instead of creating another.

**Inspecting a session.** Sessions are plain pydantic models:

```python
session = composer.store.get(session_id)

session.status  # PENDING | IN_PROGRESS | COMPLETE | ABORTED
session.amount_paise  # 450_000 - the total being collected
session.tranche_paise  # 199_900 - the configured ceiling
session.total_tranches  # 3 - how many payments the session was split into
session.tranches[0].status  # PENDING | PAID | FAILED | REFUNDED
session.tranches[0].order_id
session.tranches[0].payment_id
session.total_paid_paise()  # sum of captured tranches
session.is_fully_paid()
session.model_dump_json()  # serialize it wherever you persist state
```

## Handling Partial Failures & Aborts

**Customer abandons the split.** The session stays `PENDING` or `IN_PROGRESS` with
the unpaid tranches `PENDING`; nothing is charged beyond what was already captured.
Two options:

**Give the money back.** `abort_and_refund` refunds every captured tranche for
exactly the amount captured and marks the session `ABORTED`:

```python
session = composer.abort_and_refund(first.session_id)

session.status  # SessionStatus.ABORTED
session.total_paid_paise()  # 0 - paid tranches are now REFUNDED
session.tranches[0].status  # TrancheStatus.REFUNDED
```

- Only tranches in the `PAID` state are refunded, and each is persisted as
  `REFUNDED` before the next refund is attempted, so **a tranche is never refunded
  twice** — including when you call this again after a failure.
- Unpaid tranches are left `PENDING`. `ABORTED` is terminal, so they can never be
  collected afterwards.
- A `COMPLETE` session cannot be aborted (`SessionStateError`); refund those
  payments directly through your gateway instead.
- If a refund call fails midway, `PartialPaymentError` is raised with the
  already-refunded tranches persisted. Calling `abort_and_refund` again resumes
  from the first tranche that still needs refunding — it is safe to retry.

**An order expired before the customer paid.** Razorpay orders expire, which would
otherwise strand a session mid-way. `resume` returns a payable order for the current
pending tranche: the existing one if it is still payable (`created` or `attempted`),
otherwise a fresh replacement for the same amount, recorded on the session.

```python
order = composer.resume(first.session_id)

order.order_id  # the same order if still payable, else a new one
order.amount_paise  # the pending tranche's amount
send_to_checkout(order.raw)
```

- `resume` raises `SessionStateError` if the session is already `COMPLETE` or
  `ABORTED`, has no pending tranche, or if the pending order was already paid
  (verify that payment with `verify_and_advance` instead of paying again — this
  guards against a double charge).

A typical recovery loop is: `resume` to re-offer a checkout link, `verify_and_advance`
when the customer pays, and `abort_and_refund` if the merchant decides to cancel the
collection.

## Webhook Verification

Razorpay signs webhooks with a **webhook secret** configured in the dashboard. That
secret is *not* your API key secret. `tranchepay` delegates the HMAC to the gateway
(`gateway.verify_webhook_signature`, i.e. the provider's own verifier) and
normalizes the outcome into this library's exception hierarchy, so nothing is
re-implemented and the original exception is chained as `__cause__`.

```python
from tranchepay import VerificationError, verify_webhook


@app.post("/razorpay/webhook")
def razorpay_webhook():
    raw_body = request.get_data(as_text=True)  # exactly as received, not re-serialized
    signature = request.headers["X-Razorpay-Signature"]
    secret = settings.RAZORPAY_WEBHOOK_SECRET  # from the dashboard, not the API key secret

    try:
        verify_webhook(gateway, raw_body, signature, secret)
    except VerificationError:
        return {"error": "invalid signature"}, 400

    event = json.loads(raw_body)
    ...  # handle payment.captured, etc.
    return {"ok": True}, 200
```

Signature:

```python
def verify_webhook(gateway, body, signature, secret) -> bool: ...  # True on success
```

Pass the body exactly as received. Re-serializing JSON before verifying changes the
bytes and invalidates the signature. On failure, `VerificationError` is raised — for a
falsey result or for an exception from the SDK — so a single `except` handles both.

A webhook handler is also the natural place to call `verify_and_advance`: for
split payments, treat `payment.captured` for the session's pending order as the
trigger to advance to the next tranche. Because the flow is idempotent, it is safe
for both your callback and your webhook to call it for the same payment.

## Error Handling

Every exception derives from `PaymentComposeError`, so a whole checkout handler can
be wrapped in one `except` when the specific failure does not matter:

```text
PaymentComposeError
├── VerificationError
│   └── AmountMismatchError
├── SessionNotFoundError
├── SessionStateError
├── PartialPaymentError
├── UnsupportedGatewayError
└── GatewayConfigurationError
```

| Exception | Raised when |
| --- | --- |
| `PaymentComposeError` | Base class. Also raised for configuration misuse, e.g. `WITH_CHARGES` without a `ChargesConfig`, or a Razorpay response with no usable order id. |
| `VerificationError` | A signature check failed (`verify_payment_signature` / `verify_webhook_signature`), or a fetched payment is not in the `captured` state, or the payment could not be fetched. The original SDK exception is chained as `__cause__`. |
| `AmountMismatchError` | The captured amount differs from the tranche's recorded `amount_paise`. Subclass of `VerificationError`, so catching `VerificationError` covers both. |
| `SessionNotFoundError` | The session id is unknown to the configured `SessionStore`. |
| `SessionStateError` | The operation is illegal for the current state: the order does not belong to the session, it is not the current pending tranche, the session is already closed, or `resume` found an already-paid pending order. |
| `PartialPaymentError` | A split could not complete or unwind cleanly, e.g. a refund failed midway. Already-applied side effects are persisted; retry the same call to finish the remaining work. |
| `UnsupportedGatewayError` | `GatewayFactory.get_gateway` was asked for a provider name that is neither built in nor registered. |
| `GatewayConfigurationError` | An adapter could not be built: a required credential is missing, or the optional dependency it needs (`razorpay`, `httpx`) is not installed. |

`TypeError` and `ValueError` are raised for programmer error rather than API
failure: a float amount, a non-positive amount, a non-`Decimal` fee rate, or an
out-of-range fee rate.

```python
try:
    next_order = composer.verify_and_advance(session_id, order_id, payment_id, signature)
except AmountMismatchError:
    ...  # do not advance; investigate the payment amount
except VerificationError:
    ...  # signature or payment state rejected: return 400, do not advance
except SessionNotFoundError:
    ...  # unknown session id: return 404
except SessionStateError:
    ...  # stale or out-of-order callback: usually safe to ignore / return 409
except PaymentComposeError:
    ...  # any other tranchepay failure
```

## Architecture & Session Store

A split session is a plain pydantic model, and persistence is behind a protocol
that is three methods wide. `tranchepay` never imports your persistence code; it
validates the object structurally when you pass it in. The composer reaches the
provider the same way - through the `PaymentGateway` protocol, never a concrete
SDK - which is why the same session store works for every adapter.

```text
                      ┌───────────────────────────┐
  your app ─────────► │ GatewayFactory            │
                      │  get_gateway("razorpay")  │
                      └─────────────┬─────────────┘
                                    │ returns
                                    ▼
                      ┌───────────────────────────┐
  your app ─────────► │ PaymentComposer           │
                      │  create_order(...)        │
                      │  verify_and_advance(...)  │
                      │  abort_and_refund(...)    │
                      │  resume(...)              │
                      └─────┬───────────────┬─────┘
                            │               │
         drives as-is       │               │ save/get/update
                            ▼               ▼
             ┌──────────────────────┐  ┌────────────────────┐
             │ PaymentGateway       │  │ SessionStore       │
             │  (Protocol)          │  │  (yours, or the    │
             └───────────┬──────────┘  │   in-memory one)   │
                         │ implements  └────────────────────┘
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
 ┌────────────┐   ┌────────────┐   ┌────────────┐
 │ Razorpay   │   │ Paytm      │   │ PhonePe    │
 │ Adapter    │   │ Adapter    │   │ Adapter    │
 └────────────┘   └────────────┘   └────────────┘
```

The shipped `InMemorySessionStore` is thread-safe and copies sessions on the way in
and out, so a mutation only takes effect once `update` is called. It is
**process-local**: sessions are lost on restart and are invisible to other workers.
Use it for tests, CLIs, and single-process apps. Production deployments with more
than one worker should supply a durable adapter.

The protocol:

```python
class SessionStore(Protocol):
    def save(self, session: SplitSession) -> None:
        """Persist the session, replacing any existing record with its id."""

    def get(self, session_id: str) -> SplitSession | None:
        """Return the stored session, or None if the id is unknown."""

    def update(self, session: SplitSession) -> None:
        """Persist changes to an existing session."""
```

Rules for an adapter:

- `get` must return `None` for an unknown id and **never raise**; return a copy (or
  a freshly loaded object) so mutating the result has no effect until it is written
  back with `update`.
- `save` upserts; `update` persists the whole session.
- Make `update` **atomic** if several processes share the store. `tranchepay`'s
  in-process locking serializes concurrent transitions within one process, but it
  cannot protect cross-process races. Use a Redis `WATCH`/`MULTI` transaction or a
  Lua script, or `SELECT ... FOR UPDATE` in SQL, to serialize compare-and-swap.

A Redis adapter, using the session's JSON round-trip:

```python
import redis

from tranchepay import SplitSession


class RedisSessionStore:
    """A drop-in SessionStore backed by Redis."""

    def __init__(self, client: redis.Redis, namespace: str = "tranchepay:session") -> None:
        self._redis = client
        self._namespace = namespace

    def _key(self, session_id: str) -> str:
        return f"{self._namespace}:{session_id}"

    def save(self, session: SplitSession) -> None:
        self._redis.set(self._key(session.session_id), session.model_dump_json())

    def get(self, session_id: str) -> SplitSession | None:
        raw = self._redis.get(self._key(session_id))
        if raw is None:
            return None
        return SplitSession.model_validate_json(raw)

    def update(self, session: SplitSession) -> None:
        self.save(session)
```

```python
composer = PaymentComposer(gateway, split=SplitConfig(), store=RedisSessionStore(redis_client))
```

A SQLAlchemy adapter follows the same shape: a table keyed by `session_id`, a `TEXT`
or `JSONB` column holding `model_dump_json()`, and a `SELECT ... FOR UPDATE` inside
`update` to keep concurrent webhooks honest.

You can also bypass the store entirely: `session.model_dump_json()` serializes a
session, so a developer may persist it wherever their application already keeps
state.

## Compliance & Disclaimers

**This section is not legal advice. Read it before you ship.**

### NPCI MDR context

`SplitConfig.tranche_paise` defaults to **199,900 paise (₹1,999)**. That default
exists because NPCI's MDR rule for merchant UPI transactions applies above ₹2,000:
keeping each tranche at or below ₹1,999 is a way to stay under that threshold. It is
the only place in the codebase where that number is written down.

```python
SplitConfig()  # 199_900 paise (₹1,999) - the default ceiling
SplitConfig(tranche_paise=50_000)  # ₹500 per tranche - configure to your compliance policy
```

`tranchepay` does not decide the size for you. It splits `amount_paise` with
`divmod` against whatever ceiling you configure, and nothing else.

### Regulatory changes

The ₹2,000 threshold, MDR rates, and the treatment of different instruments are set
by NPCI, the RBI, and the card networks. They are **subject to change**, and they
differ by instrument, merchant category, and date. A threshold that is correct today
may not be correct after the next circular.

`tranchepay` does not track regulation and does not update this default for you.
Confirm the current limit yourself and set `SplitConfig(tranche_paise=...)` to
whatever your compliance team requires. The default is a convenience, not a
representation that ₹1,999 is compliant for your business.

### Surcharging rules

**Surcharging customers on UPI and debit card payments is prohibited** by the RBI
and NPCI. Do not use `PaymentMode.WITH_CHARGES` to pass your gateway cost on to a
customer paying by UPI or debit card.

`WITH_CHARGES` exists only for instruments and jurisdictions where recovering a
gateway fee is lawful and defensible — for example, certain credit card
transactions. Even there:

- Apply it only where it is legally permitted for the instrument you are collecting
  on, and confirm that with your own counsel and your acquirer.
- Disclose the fee to the customer before they pay. An undisclosed "convenience
  fee" is a compliance and chargeback problem.
- Remember that the mode is arithmetic, not advice: `tranchepay` will happily
  gross up an amount on any instrument. Choosing to do so is your decision and your
  responsibility.
- Splitting one purchase into tranches to work around surcharging rules is not a
  permitted use of this library.

### Standard OSS disclaimer

`tranchepay` is released under the **MIT License** and is provided **"as is"**,
without warranty of any kind, express or implied, including but not limited to the
warranties of merchantability, fitness for a particular purpose, and
non-infringement. See [`LICENSE`](LICENSE) for the full text.

- **Merchants are solely responsible for their own compliance**, including
  PCI-DSS, RBI and NPCI rules, MDR treatment, GST and invoicing, customer
  disclosures, and Razorpay's own terms of service.
- Using this library does not make a transaction compliant. You are responsible for
  the correctness of your fee rate, for how you charge customers, and for any
  reporting obligation that follows.
- `tranchepay` provides no fee rate, does not know your pricing, and does not
  calculate tax.
- This project is not affiliated with, endorsed by, or sponsored by Razorpay.

In short: the library does the arithmetic and the state machine. The regulatory
judgment is yours.
