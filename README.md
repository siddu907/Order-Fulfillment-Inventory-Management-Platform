# Order-Fulfillment-Inventory-Management-Platform

A role-aware REST API for customer orders, warehouse inventory, fulfillment, returns, and audit history. It uses Python, FastAPI, SQLAlchemy, Alembic, Pydantic, PostgreSQL, and Pytest.

## Requirements implemented

- JWT access and refresh tokens with rotation, revocation, password hashing, and inactive-account protection.
- Admin, warehouse manager, fulfillment agent, and customer roles, with warehouse-scoped authorization.
- Product, category, warehouse, and warehouse-membership management.
- Per-warehouse inventory balances and an inventory transaction ledger.
- Order placement with automatic warehouse selection, atomic stock reservation, immutable price/tax snapshots, lifecycle history, cancellation, and fulfillment.
- Agent assignment/reassignment, generated expected delivery estimates, and staff-managed delivery-date changes.
- Return requests, approval, receipt/restocking, and demo refund-status handling.
- In-app notifications, audit logs, dashboards, filtering, sorting, pagination, and idempotency support.

## Technology and prerequisites

- Python 3.11 or newer
- PostgreSQL 14 or newer
- Packages listed in `requirements.txt`

SQLite is used for fast API tests by default. PostgreSQL is required to verify row-locking concurrency and PostgreSQL append-only ledger behavior.

## Local setup

### Create and configure the PostgreSQL database

Make sure PostgreSQL is installed and running, then use `psql` (or pgAdmin) to
create a dedicated login and database. In PowerShell, replace the example
password with a strong password:

```powershell
psql -U postgres
```

At the `psql` prompt:

```sql
CREATE USER fulfillment_user WITH PASSWORD 'replace-with-a-strong-password';
CREATE DATABASE fulfillment OWNER fulfillment_user;
\q
```

Configure the connection and application secrets in the same PowerShell window
you will use for migrations and to run the API:

```powershell
$env:DATABASE_URL = "postgresql+psycopg2://fulfillment_user:replace-with-a-strong-password@localhost:5432/fulfillment"
$env:SECRET_KEY = "replace-with-a-long-random-secret"
$env:ACCESS_TOKEN_MINUTES = "1440"
$env:REFRESH_TOKEN_DAYS = "14"
```

`app.config` reads these process environment variables (which override values in
`.env.example`). These PowerShell values last only for that window. Set them in
your deployment environment for production; do not commit database passwords
or signing secrets. If the database password contains URL-reserved characters,
URL-encode those characters in `DATABASE_URL`.

### Install, migrate, and run

1. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

   On macOS/Linux, use `python3 -m venv .venv` and `source .venv/bin/activate`.
2. Apply migrations:

   ```powershell
   alembic upgrade head
   ```
3. Create the initial administrator:

   ```powershell
   python -m app.seed_admin
   ```

   The command prompts for administrator name, email, and password. It also accepts `ADMIN_NAME`, `ADMIN_EMAIL`, and `ADMIN_PASSWORD` environment variables. Only one administrator account is permitted; public registration creates customers.
4. Run the development server:

   ```powershell
   uvicorn app.main:app --reload
   ```
5. Open Swagger at <http://127.0.0.1:8000/docs>. Import [Postman Collection.json](<./Postman Collection.json>) for example requests.

## Configuration

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLAlchemy URL, e.g. `postgresql+psycopg2://user:password@localhost:5432/orders_db` |
| `SECRET_KEY` | Shared JWT signing secret; set a strong secret outside source control |
| `ACCESS_TOKEN_MINUTES` | Access-token lifetime |
| `REFRESH_TOKEN_DAYS` | Refresh-token lifetime |
| `ADMIN_NAME` | Optional initial-admin provisioning input |
| `ADMIN_EMAIL` | Optional initial-admin provisioning input |
| `ADMIN_PASSWORD` | Optional initial-admin provisioning input |

The tax percentage is database configuration, not an environment variable. It starts at 0%. Authenticated users can read it with `GET /settings/tax-rate`; only admins can change it with `PUT /settings/tax-rate`, for example `{"rate_percent":18}`.

## Roles and authorization

| Role | Access |
| --- | --- |
| **Admin** | Manage users, products, categories, warehouses, warehouse memberships, and tax; view/manage all orders and warehouses; assign agents; access admin dashboard and audit logs. |
| **Warehouse manager** | Manage inventory and warehouse details within assigned warehouses; view/manage scoped orders and returns; assign agents authorized for the same warehouse; access that warehouse's dashboard. |
| **Fulfillment agent** | View inventory and orders within assigned warehouses; process orders currently assigned to them. |
| **Customer** | Register/login; place orders; view own orders and notifications; cancel eligible orders; confirm demo payment; request eligible returns. |

Protected routes require `Authorization: Bearer <access_token>`. Admins authorize warehouse managers and fulfillment agents through:

- `GET /warehouses/{warehouse_id}/users`
- `POST /warehouses/{warehouse_id}/users` with `{"user_id":4}`
- `DELETE /warehouses/{warehouse_id}/users/{user_id}`

Only active users with the warehouse-manager or fulfillment-agent role may be assigned to a warehouse. Agents must be authorized for an order's warehouse before assignment.

## API overview

See the full [API reference](./docs/api.md) for endpoint permissions, request parameters and bodies, response shapes, state transitions, and error behavior. The live Swagger/OpenAPI docs are at `/docs`. Import [Postman Collection.json](<./Postman Collection.json>) into Postman for request examples.

See the [entity-relationship diagram](./docs/er-diagram.md) for the database entities, foreign-key relationships, and key integrity constraints.

See the [system architecture](./docs/architecture.md) for application components, request flow, transaction boundaries, and integration scope.

### Authentication

`POST /auth/register`, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, and `POST /auth/change-password`.

Registration creates a customer. Login returns access and refresh tokens. Refresh rotates the refresh token; logout revokes refresh tokens and invalidates existing access tokens.

### Catalog, users, and warehouses

- Users: `POST /users`, `GET /users`, `GET /users/{user_id}`, `PUT /users/{user_id}` (admin).
- Products: `POST /products`, `GET /products`, `GET /products/{product_id}`, `PUT /products/{product_id}`. Admin-only writes; updates include activation/deactivation.
- Categories: `POST /categories`, `GET /categories`, `GET /categories/{category_id}`, `PUT /categories/{category_id}`. Admin-only writes.
- Warehouses: `POST /warehouses`, `GET /warehouses`, `GET /warehouses/{warehouse_id}`, `PUT /warehouses/{warehouse_id}`.
- Warehouse members: the three admin-only routes listed above.

### Inventory

- `GET /inventory` — List/filter balances.
- `GET /inventory/{product_id}` — Get a product's balance(s), optionally scoped by `warehouse_id`.
- `GET /inventory/low-stock` and `GET /inventory/out-of-stock` — Query low or zero available stock.
- `POST /inventory/adjust` — Add initial stock or adjust an existing balance.
- `POST /inventory/transfer` — Transfer stock between authorized warehouses.
- `GET /inventory/transactions` and `GET /inventory/transactions/{transaction_id}` — Read the inventory ledger.

Inventory records track available, reserved, damaged, and reorder-level quantities. An adjustment can create the product/warehouse balance if it does not exist. Each stock change records a transaction. Warehouse managers receive in-app `low_stock` and `out_of_stock` notifications when available stock crosses the corresponding thresholds; cancellation/failure releases reserved stock and sends `stock_restored`.

### Orders and fulfillment

- `POST /orders` — Customer creates an order.
- `GET /orders` — List visible orders (customer-owned or warehouse-scoped for staff).
- `GET /orders/{order_id}`, `GET /orders/{order_id}/items`, `GET /orders/{order_id}/history`.
- `POST /orders/{order_id}/cancel` — Customer cancels an eligible order.
- `PUT /orders/{order_id}` — Admin or authorized warehouse staff changes status and/or expected delivery date.
- `POST /orders/{order_id}/assign` and `/orders/{order_id}/reassign` — Admin or authorized warehouse manager assigns an active, warehouse-authorized agent.
- `GET /fulfillment/orders/pending` — Pending orders visible to warehouse staff.
- `GET /fulfillment/orders/assigned-to-me` — Agent's current assignments.
- `POST /fulfillment/orders/{order_id}/accept`, `/start-processing`, `/pack`, `/ship`, `/deliver`, and `/fail` — Fulfillment transitions.

Customer requests contain product IDs and quantities (plus optional discount); customers do not select the warehouse, expected delivery date, or tax. The service selects an active warehouse with enough stock for the entire basket. If several warehouses qualify, the current implementation selects the eligible warehouse with the lowest ID. Expected delivery is estimated at five calendar days after placement; authorized staff can update the future date, notifying the customer.

The normal status flow is `pending -> confirmed -> processing -> packed -> shipped -> delivered`. Cancellation, failure, and return transitions are constrained by the service state machine. Cancellation releases reservations; shipment consumes reserved stock.

### Returns, payments, notifications, dashboards, tax, and audit

- Returns: `POST /returns`, `GET /returns`, `GET /returns/{return_id}`, `GET /returns/{return_id}/items`, and admin/warehouse-manager actions `POST /returns/{return_id}/approve`, `/reject`, `/receive`, `/refund`.
- Payments: `POST /payments/{order_id}/confirm`, `GET /payments/{order_id}`.
- Notifications: `GET /notifications`, `GET /notifications/{notification_id}`, `PATCH /notifications/{notification_id}/read`, `PATCH /notifications/read-all`.
- Dashboards: `GET /dashboard/admin`, `GET /dashboard/warehouse?warehouse_id={id}`, `GET /dashboard/customer`.
- Tax: `GET /settings/tax-rate`, `PUT /settings/tax-rate` (admin-only update).
- Audit: `GET /audit-logs`, `GET /audit-logs/{audit_id}` (admin).

Return workflow: `requested -> approved -> received -> refunded`, or `requested -> rejected`. Returned stock is put back into inventory at receipt. Payment and refund handling is demo-only and does not send or verify real money.

## Order pricing and tax

```text
subtotal   = product price × quantity
taxable    = subtotal − discount
tax        = taxable × configured tax rate / 100 (rounded to 2 decimal places)
line total = taxable + tax
```

The tax rate is 0–100 percent, to two decimal places, and initially 0%. The order item stores snapshots of unit price, discount, applied tax rate, tax amount, and total, preserving order history when the product price or configured tax rate changes.

## Example: place an order

Login as the customer:

```http
POST /auth/login
Content-Type: application/json

{"email":"customer@example.com","password":"your-password"}
```

Then use the returned access token:

```http
POST /orders
Authorization: Bearer <access_token>
Content-Type: application/json

{"items":[{"product_id":1,"quantity":2}]}
```

`Idempotency-Key` is optional for order creation, inventory adjustment/transfer, and payment confirmation. Clients that send a key should reuse it only to retry the same operation. Payment confirmation remains retry-safe when its optional key is omitted; the server derives a stable key for that order.

## Pagination and filtering

Paged list routes use one-based `page` and `page_size` parameters; maximum page size is 100. Orders support status, customer, warehouse, payment status, date range, total range, and sorting by creation date, total, or status. Inventory supports product/warehouse and low/out-of-stock filters. Notifications support `unread_only`.

## Data integrity and concurrency

- Order creation reserves available inventory using conditional database updates in one transaction. Competing PostgreSQL requests serialize on inventory rows; a request cannot reserve units no longer available.
- A failed basket reservation rolls back the order, line items, reservation ledger, audit, and notification writes.
- Inventory adjustments and transfers lock rows; order status changes lock the order. Cancellation, shipping, and receipt of returned goods update balances and ledger entries transactionally.
- Inventory transactions and audit logs are append-only in PostgreSQL.
- An idempotency key is scoped to user and endpoint. Reusing a key with a different request returns HTTP 409.

## Migrations and tests

```powershell
alembic upgrade head
pytest
```

Tests use in-memory SQLite unless `TEST_POSTGRESQL_URL` is set. PostgreSQL-specific concurrency and immutable-ledger tests are skipped otherwise. When setting `TEST_POSTGRESQL_URL`, use a disposable database or isolated schema: tests create and drop mapped tables in that target. 


