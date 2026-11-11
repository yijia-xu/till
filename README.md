# Till

Till is the entry for the PayPal AI Hackathon 2026.

Till is a payment-safety layer between a shopping agent and PayPal. A shopping agent can propose an order, but Till checks it against the shopper’s saved intent, catalog data, spending limits, duplicate-payment rules, and PayPal order details. It also detects tampering and other malicious payment attempts before a payment can proceed.

## Requirements

- Python 3.13 (also pinned in `.python-version` for Render builds).
- A Gemini API key to enable AI matching and needs checks.
- PayPal Sandbox REST app credentials to create PayPal Sandbox orders. `PAYPAL_MODE=simulate` can be used without PayPal credentials.

## Run locally

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with your Gemini key and, if using PayPal Sandbox, your PayPal Sandbox app credentials. Then start Till:

```bash
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in with the demo account:

- Email: `demo@till.local`
- Password: `demo`

The local app uses SQLite at `till.db` by default. `/health` reports whether the app is up, the PayPal mode, and the configured AI model.

## Use Till

1. Open **Shopping** and describe the item and constraints in natural language.
2. Set a hard cap per order, per day, and per category, plus the amount above which Till should ask for review. Shipping and add-ons count toward the limits.
3. Enter the delivery address as `street, city, state/province, postal code, 2-letter country code`.
4. Choose **Find a match with AI**. Till matches the request against the products on shopping websites, then submits the selected item as an order proposal for policy checks.
5. Open **Orders** to see Till's decision, checked amount, payment status, and any risk flags. If a proposal needs review, approve or decline it in Till.
6. For PayPal Sandbox checkout, follow the PayPal approval link and authorize the order with a Sandbox personal test account. Till captures only after the required approval steps. A verified PayPal webhook confirms completion.

## Environment variables

Copy `.env.example` to `.env`. Keep `.env` private and never commit API keys, PayPal secrets, or production tokens.

| Variable                                    | Purpose                                                                                                                                         |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `PAYPAL_MODE`                               | `simulate` or `sandbox`. Simulation never contacts PayPal.                                                                                      |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | Credentials for your PayPal Sandbox REST app. Required in `sandbox` mode.                                                                       |
| `PAYPAL_BASE`                               | Sandbox API base, normally `https://api-m.sandbox.paypal.com`.                                                                                  |
| `PAYPAL_WEBHOOK_ID`                         | ID of the webhook configured in the PayPal Developer Dashboard. Needed to verify incoming PayPal webhook events.                                |
| `TILL_PUBLIC_BASE_URL`                      | Public origin used for PayPal return/cancel links and webhook URL. Use `http://127.0.0.1:8000` locally and your HTTPS Render URL when deployed. |
| `TILL_DB`                                   | SQLite database path. Defaults to `till.db`.                                                                                                    |
| `TILL_AGENT_TOKEN`                          | Bearer token for the external agent API and MCP server. Change the demo value for any shared deployment.                                        |
| `TILL_MCP_API_URL`                          | Base URL used by the MCP server to reach Till. Defaults to `http://127.0.0.1:8000`.                                                             |
| `TILL_SIMULATOR_TOKEN`                      | Token for the local simulator webhook endpoint. Do not use it as a PayPal webhook credential.                                                   |
| `TILL_DUPLICATE_WINDOW_SECONDS`             | Time window used to block repeat payments for the same item.                                                                                    |
| `TILL_APPROVAL_TTL_SECONDS`                 | Lifetime of a shopper review approval.                                                                                                          |
| `TILL_INTENT_TTL_SECONDS`                   | Lifetime of the saved shopping intent.                                                                                                          |
| `NEEDS_MODEL_API_KEY`                       | Gemini API key used by AI shopping matching and needs checks.                                                                                   |
| `NEEDS_MODEL_URL`                           | Gemini API base URL. The default is the Google Generative Language API.                                                                         |
| `NEEDS_MODEL_NAME`                          | Gemini model name. Defaults to `gemini-3.5-flash-lite`.                                                                                         |

## Connect an MCP agent

Till's MCP server uses the stdio transport and calls Till's authenticated HTTP API. Install the app dependencies, then register a server in your MCP host configuration:

```json
{
  "mcpServers": {
    "till": {
      "command": "/absolute/path/to/till/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/absolute/path/to/till",
      "env": {
        "TILL_MCP_API_URL": "http://127.0.0.1:8000",
        "TILL_AGENT_TOKEN": "your-agent-token"
      }
    }
  }
}
```

Start Till before launching the MCP host. Available tools:

- `submit_order_proposal`: submits an order proposal for Till to evaluate. Money fields use minor units (for example, `7400` means USD 74.00).
- `get_order_decision`: reads the current decision and status for a submitted order.

The MCP server does not expose PayPal credentials or payment controls. For a deployed Till service, set `TILL_MCP_API_URL` to its HTTPS URL and provide the matching `TILL_AGENT_TOKEN` to the MCP process.

## PayPal Sandbox and webhooks

1. In the PayPal Developer Dashboard, create a REST app in **Sandbox** and copy its client ID and secret to the environment where Till runs.
2. Set `PAYPAL_MODE=sandbox`, `PAYPAL_BASE=https://api-m.sandbox.paypal.com`, and `TILL_PUBLIC_BASE_URL` to the URL where the buyer can return to Till.
3. Add a PayPal webhook pointing to `https://<your-domain>/webhooks/paypal`. Subscribe to `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, and `PAYMENT.CAPTURE.DENIED`.
4. Copy the webhook ID into `PAYPAL_WEBHOOK_ID` and restart or redeploy Till.

PayPal sends webhook events with `POST`. Opening `/webhooks/paypal` in a browser uses `GET` and returns endpoint configuration information; it does not simulate a payment or webhook event. Till verifies PayPal webhook signatures before applying events. For local development without a public webhook URL, the simulator endpoint is `POST /webhooks/simulator` and requires `X-Till-Simulator-Token`.

## Deploy to Render

1. Push the project to GitHub and connect the repository to a Render **Web Service**.
2. Use these commands:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

3. Add the required values from `.env.example` in the Render service's **Environment** settings. Set `TILL_PUBLIC_BASE_URL` to the Render HTTPS URL. Do not upload `.env` to GitHub.
4. Deploy. Render will redeploy from the linked branch when new commits are pushed, if automatic deploys are enabled.

The `.python-version` file pins the build to Python 3.13 to keep the MCP SDK and the app's FastAPI/Starlette dependencies compatible. A `PYTHON_VERSION` value set in Render's Environment settings takes precedence over this file.

The default database is a local SQLite file. Render's local filesystem is not a durable database across all service lifecycle events; use a persistent disk for a demo that must retain data, or move to a managed database for a longer-lived deployment.

## Agent HTTP API

External agents can use the same authenticated API as the MCP tools:

- `POST /api/orders` submits a proposal with `Authorization: Bearer <TILL_AGENT_TOKEN>`.
- `GET /api/orders/{order_id}` reads its decision and status with the same authorization header.

The agent API does not accept caller claims such as `approved`, `user_id`, or `amount` as authority. Till calculates the policy decision itself.

## Development

```bash
pytest
```

`python -m app.demo` runs the local demo with a fake PayPal client; it does not call PayPal.
