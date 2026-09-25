# tranchepay

Compose payment modes around the Razorpay client you already own: charge the exact
amount, gross the amount up so the customer covers the gateway fee, or collect one
large amount as sequential tranches of at most ₹1,999.

[![CI](https://github.com/tranchepay/tranchepay/actions/workflows/ci.yml/badge.svg)](https://github.com/tranchepay/tranchepay/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

- **Composition only.** tranchepay never subclasses, monkey-patches, or forks
  `razorpay.Client`. You build and configure the client; tranchepay calls its public
  `order`, `payment`, and `utility` resources.
- **Integer paise everywhere.** Amounts are `int` paise and fees are
  `decimal.Decimal`; a float is rejected rather than rounded.
- **Explicit rounding.** Gross-up rounding is a documented config value, not an
  implementation detail.
- **Resumable splits.** Sessions are plain pydantic models in a pluggable store, so a
  half-finished split survives a deploy.

## Install

```bash
pip install tranchepay
```

Requires Python 3.10+ and `razorpay>=1.4` (installed automatically). Add the dev
extras for tests and type checks: `pip install "tranchepay[dev]"`.

## Quickstart

```python
from decimal import Decimal

import razorpay
from tranchepay import ChargesConfig, PaymentComposer, PaymentMode, SplitConfig

client = razorpay.Client(auth=("rzp_test_xxxxxxxx", "your_key_secret"))
composer = PaymentComposer(
    client,
    charges=ChargesConfig(fee_rate=Decimal("0.0236")),  # 2.36% - your rate, as a Decimal
    split=SplitConfig(),  # tranche ceiling, default 199_900 paise
)
```

### 1. EXACT — the merchant absorbs the charges

```python
order = composer.create_order(50_000, mode=PaymentMode.EXACT)

order.order_id  # "order_XXXXXXXXXXXXXX"
order.amount_paise  # 50_000 - the customer is charged exactly this
order.raw  # untouched Razorpay response, ready for Checkout
```

### 2. WITH_CHARGES — the customer covers the gateway fee

```python
order = composer.create_order(50_000, mode=PaymentMode.WITH_CHARGES)

order.amount_paise  # 51_209 - charged to the customer (round(50_000 / (1 - 0.0236)))
order.net_paise  # 50_000 - what the merchant keeps
order.fee_paise  #  1_209 - the fee the customer paid instead
```

### 3. SPLIT — sequential tranches, one customer at a time

```python
first = composer.create_order(450_000, mode=PaymentMode.SPLIT)  # ₹4,500 -> ₹1,999 + ₹1,999 + ₹502
session_id = first.session_id
# Send first.raw to Razorpay Checkout.

# In your payment callback / webhook handler, after Checkout returns:
next_order = composer.verify_and_advance(
    session_id=session_id,
    order_id=payload["razorpay_order_id"],
    payment_id=payload["razorpay_payment_id"],
    signature=payload["razorpay_signature"],
)
# next_order is the next tranche's order to check out, or None when the split is complete.
```

`verify_and_advance` verifies the signature with Razorpay's own
`client.utility.verify_payment_signature`, fetches the payment, requires
`status == "captured"` and an amount equal to the tranche, marks the tranche paid, and
creates the next order. Each tranche is verified independently, and the whole call is
idempotent.

### Recovering a split

```python
order = composer.resume(session_id)  # same order if still payable, fresh one if expired
session = composer.abort_and_refund(session_id)  # refund every captured tranche, then ABORTED
session.status  # SessionStatus.ABORTED
```

### Webhooks

```python
from tranchepay import VerificationError, verify_webhook

try:
    verify_webhook(client, raw_body, signature_header, webhook_secret)
except VerificationError:
    ...  # return 400
```

### Inspecting a session

```python
session = composer.store.get(session_id)

session.status  # PENDING | IN_PROGRESS | COMPLETE | ABORTED
session.tranches[0].status  # PENDING | PAID | FAILED | REFUNDED
session.tranches[0].payment_id
session.total_paid_paise()
session.model_dump_json()  # persist it wherever you like
```

## Architecture

```
                         ┌───────────────────────────────────────┐
    your application ───▶│            PaymentComposer            │
                         │  EXACT │ WITH_CHARGES │    SPLIT     │
                         └────┬───────────┬───────────────┬──────┘
                              │           │               │
                money.gross_up│           │               │ split.plan_tranches
                              ▼           ▼               ▼
                         ┌───────────────────────────────────────────┐
                         │ orders: create order, wrap OrderResult    │
                         └───────────────────┬───────────────────────┘
                                             │
                         SplitFlow ──────────┤  (split_flow:     start, verify_and_advance)
                         SplitRecovery ──────┤  (split_recovery: abort_and_refund, resume)
                         verification ───────┘  (signature + captured-payment checks)
                                             │
       razorpay.Client (your instance, used as-is, never wrapped)
       ├── order.create / order.fetch
       ├── payment.fetch / payment.capture / payment.refund
       └── utility.verify_payment_signature / verify_webhook_signature
                                             │
                         SessionStore ◀──────┘  (InMemorySessionStore, or your own)
```

| Module | Responsibility |
| --- | --- |
| `models.py` | Pydantic v2 config, `SplitSession`/`Tranche`, `OrderResult` |
| `money.py` | Exact paise arithmetic, `gross_up`, fee-rate validation |
| `split.py` | Pure tranche planning (`plan_tranches`, `build_session`) |
| `orders.py` | Order payloads and per-tranche bookkeeping |
| `split_flow.py` | Collection state machine, `verify_and_advance` |
| `split_recovery.py` | `abort_and_refund`, `resume` |
| `store.py` | `SessionStore` protocol + `InMemorySessionStore` |
| `verification.py` | Signature and captured-payment checks |
| `session_locks.py` | Process-global per-session transition locks |
| `protocol.py` | Structural typing for the Razorpay client |

## Money rules

- Every amount is an `int` number of paise. Passing a float raises `TypeError`
  instead of quietly rounding.
- `fee_rate` is a `Decimal` between `0` and `1` (exclusive). Floats are rejected.
- `gross_up(net, fee_rate, rounding)` computes `round(net / (1 - fee_rate))` as an
  exact rational and rounds once, at the end, using the configured policy:

  | Policy | Effect on the merchant's recovery |
  | --- | --- |
  | `ROUND_UP`, `ROUND_CEILING` | Never under-recovers; may net up to one paisa extra |
  | `ROUND_HALF_UP` (default) | Closest paisa; may under-recover by less than half a paisa |
  | `ROUND_HALF_DOWN`, `ROUND_HALF_EVEN` | Same, with the tie broken toward zero/even |
  | `ROUND_DOWN`, `ROUND_FLOOR` | Customer never overcharged; merchant absorbs up to one paisa |

  Whatever the policy, `OrderResult` always reports the gross charged, the net, and
  the implied fee, so nothing is hidden.

## Splits, idempotency, and concurrency

- `plan_tranches(amount_paise, tranche_paise)` is a pure `divmod`: *n* full tranches
  plus a remainder tranche, which is omitted when it is zero. An amount at or below
  the ceiling yields a single tranche and `plan.requires_session is False`; in that
  case `create_order(mode=SPLIT)` skips the session entirely and places one ordinary
  order.
- Transitions are keyed on `(session_id, tranche_index)`. Replaying a verification for
  an already-paid tranche verifies the signature again, changes nothing, and returns the
  same next order (or `None` when the session is already complete).
- In one process, transitions for a session are serialised by a global per-session
  lock, so concurrent callbacks cannot advance the same tranche twice. Across
  processes, atomicity is the store adapter's job: make `update` a compare-and-swap
  (Redis `WATCH`/`MULTI` or a Lua script, SQL `SELECT ... FOR UPDATE`).
- Only the *current pending* tranche can be advanced, and a replacement order is never
  created for an order that is already paid, so a replayed callback cannot cause a
  double charge.
- `abort_and_refund` refunds only `PAID` tranches and persists each one as `REFUNDED`
  before touching the next, so no tranche is ever refunded twice - even when an earlier
  attempt failed halfway and raised `PartialPaymentError`.

## Session stores

`InMemorySessionStore` (the default) is thread-safe and copy-on-access, but it is
process-local: sessions vanish on restart and are invisible to other workers. For
production, pass a durable adapter implementing three methods:

```python
class SessionStore(Protocol):
    def save(self, session: SplitSession) -> None: ...
    def get(self, session_id: str) -> SplitSession | None: ...
    def update(self, session: SplitSession) -> None: ...
```

`SplitSession` is a pydantic model, so an adapter is usually
`session.model_dump_json()` and `SplitSession.model_validate_json(...)`. `get` should
return a copy, and `update` should be atomic. Any object with these three methods
works - tranchepay validates it structurally and never imports your code.

## Exceptions

All errors derive from `PaymentComposeError`.

| Exception | Raised when |
| --- | --- |
| `VerificationError` | Signature rejected, payment not `captured`, or payment unfetchable |
| `AmountMismatchError` | Captured amount is not the tranche amount (subclass of `VerificationError`) |
| `SessionNotFoundError` | The session id is unknown to the store |
| `SessionStateError` | Order not in the session, wrong tranche, or session already closed |
| `PartialPaymentError` | A split could not complete or unwind; call again to finish |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest --cov=tranchepay
ruff check . && ruff format --check .
mypy
```

The test suite makes **zero network calls** and never touches a real Razorpay
account: `tests/fakes.py` implements the client's resources in-process, including real
HMAC-SHA256 signature verification, and `tests/test_composer_modes.py` asserts that the
official `razorpay.Client` satisfies the protocol tranchepay expects.

## COMPLIANCE

**Not legal advice. Read this before you ship.**

- **Why the default tranche ceiling is ₹1,999.** NPCI's MDR rules for merchant UPI
  transactions apply above ₹2,000, so splitting a large collection into tranches of at
  most ₹1,999 keeps each tranche at or below that threshold. That threshold, and the
  rules around it, are set by NPCI, RBI, and the networks, are **subject to change**,
  and differ by instrument, merchant category, and date. tranchepay ships ₹1,999 as a
  *default constant only*: it does not track regulation, and you must confirm the
  current limit yourself. Configure `SplitConfig(tranche_paise=...)` to whatever your
  compliance team requires.
- **Merchants are responsible for their own compliance.** Using this library does not
  make a transaction compliant. You are responsible for the correctness of your fee
  rate, for how you charge customers, for MDR treatment, for GST and invoicing, for
  Razorpay's own terms, and for any reporting obligation that follows. tranchepay does
  not provide a fee rate, does not know your pricing, and does not calculate tax.
- **Surcharging is prohibited on UPI and debit.** Charging customers an extra amount to
  cover your payment costs is not permitted on UPI and debit card transactions. The
  `WITH_CHARGES` mode exists for instruments and jurisdictions where recovering a
  gateway fee is lawful; **do not use it to surcharge UPI or debit transactions**, and
  confirm before use that it is permitted for the instrument you are collecting on.
- **No warranty.** tranchepay is MIT-licensed and provided "as is", without warranty of
  any kind. See [`LICENSE`](LICENSE). This project is not affiliated with or endorsed
  by Razorpay.

## Future ideas

Deliberately out of scope for now, kept here so they are not lost:

- A Redis store adapter (and a SQLAlchemy one) shipped in-tree rather than documented.
- Per-tranche receipts or notes so every tranche order is trivially reconcilable.
- Async variants of the composer, flow, and store for `asyncio` frameworks.
- Pluggable order-status strategies for `resume` beyond `created`/`attempted`.
- Partial-capture support on the final tranche for amounts Razorpay rejects.
- A `PaymentComposer.fee_summary()` helper that reports realised fees per session.

## License

MIT - see [`LICENSE`](LICENSE).
