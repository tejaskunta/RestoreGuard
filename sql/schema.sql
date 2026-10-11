-- RestoreGuard sample database.
--
-- Three tables in a chain: customers <- orders <- order_events
-- That chain is what makes the integrity check meaningful: every order
-- points at a real customer, every event points at a real order.

DROP TABLE IF EXISTS order_events, orders, customers CASCADE;

CREATE TABLE customers (
    id         bigserial PRIMARY KEY,
    email      text NOT NULL UNIQUE,
    full_name  text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id          bigserial PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    status      text NOT NULL,
    total_cents integer NOT NULL
);

-- order_events is the "high write" table. The freshness check looks ONLY
-- at this one, because it updates constantly. A table like customers
-- could legitimately be quiet for weeks.
CREATE TABLE order_events (
    id          bigserial PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    event_type  text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

-- Sample data, so the checks have something to look at.
INSERT INTO customers (email, full_name)
SELECT 'user' || g || '@example.com', 'User ' || g
FROM generate_series(1, 50) g;

INSERT INTO orders (customer_id, status, total_cents)
SELECT 1 + (g % 50), 'paid', 1000 + g
FROM generate_series(1, 200) g;

INSERT INTO order_events (order_id, event_type, occurred_at)
SELECT 1 + (g % 200), 'order.paid', now() - (g || ' minutes')::interval
FROM generate_series(1, 600) g;