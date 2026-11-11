# Till

Till sits between a shopping agent and PayPal. The agent may propose an item. Only Till creates, reads, captures, or voids a PayPal order. A person opens Till to see each order: whether it was approved, whether it succeeded, where it failed, and whether that failure was malicious.

Decisions are `ALLOW`, `BLOCK`, or `NEED_APPROVAL`. Amounts are integer minor units. Quantity times unit price, plus tax, shipping, and add-ons, is the full total Till compares to the caps.

## Run the demo

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.demo
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and sign in as `demo@till.local` / `demo`.

`python -m app.demo` uses a fake PayPal client. It does not call PayPal. Needs judgment does call an LLM: set `NEEDS_MODEL_API_KEY` in `.env`. The model says whether the listing fits the shopper's sentence. It cannot turn a hard block into an allow. If the key is missing or the model is down, that check waits for a person. The honest order is a $74 pair of running shoes, size 9, under the $80 cap, with no subscription. Till would capture it, then a local verified webhook marks it complete. Changed price, swapped item, changed quantity, and a second charge for the same shoes are blocked.

The agent token in `.env.example` is `demo-agent-token`. The agent sends `POST /api/orders` and reads `GET /api/orders/{id}`. It has no PayPal credentials. Fields named `approved`, `user_id`, and `amount` grant nothing.

```bash
pytest
```

## PayPal sandbox

Leave `PAYPAL_MODE=simulate` to run the demo and the tests. Till uses the real sandbox client only when `PAYPAL_MODE=sandbox` and both `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` are set.

1. Create a REST app in the PayPal Developer Dashboard and copy the sandbox client id and secret into `.env`. Do not commit `.env`.
2. Set `PAYPAL_MODE=sandbox` and `PAYPAL_BASE=https://api-m.sandbox.paypal.com`.
3. Set `TILL_PUBLIC_BASE_URL` to the origin where the buyer can return to Till (for local use, `http://127.0.0.1:8000`; for deployment, your public HTTPS origin). Restart Till. Create still puts the decision id in `custom_id`. Capture sends `PayPal-Request-Id`.
4. For webhooks, subscribe `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, and `PAYMENT.CAPTURE.DENIED` to a public `POST /webhooks/paypal`. Set `PAYPAL_WEBHOOK_ID`. Till checks the signature and accepts the certificate URL only when its host is PayPal. Each event id is applied once. The order is complete only when the captured currency and amount match the decision.
5. Without a public URL, post a simulator event to `POST /webhooks/simulator` with header `X-Till-Simulator-Token`. That path is for the local demo. It does not replace signature verification on `/webhooks/paypal`.

After Till allows an order, the buyer must follow the PayPal approval link before Till captures it. A shopper approval in Till is a separate policy gate and does not substitute for PayPal buyer authorization. Till does not treat an order id as completion. A mismatch, or a capture with no approved decision, is an incident. Till voids the PayPal order if it is still open.

## Deferred

These stay out of v1:

- Refuse a payment that has no shopper intent.
- Separate authorization for a subscription or a protection plan.
- Confirm the first order from a new merchant.
- Add up split orders and rapid small charges before the budget check.
- Ignore product-page text that tries to change the rules.
- An MCP door. When it exists, its tools are submit and get decision. It cannot capture.

Till does not refund, answer disputes, fetch arbitrary merchant URLs, or train a model.
