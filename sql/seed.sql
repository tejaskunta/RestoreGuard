-- RestoreGuard seed schema: users -> orders -> payments
-- This simulates your "production" source database.
-- All data is fake. No real user data anywhere.
--
-- HOW TO READ THIS FILE (for your viva):
--   1. Three tables linked by FOREIGN KEYs. That is deliberate:
--      it gives the referential-integrity check something to catch.
--   2. created_at defaults to NOW(). The freshness check looks at
--      MAX(created_at) in orders — so a fresh seed always passes.
--   3. The payments.amount CHECK ensures realistic data.

DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    status TEXT NOT NULL DEFAULT 'paid',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fresh sample rows (timestamps = NOW() so freshness check passes)
INSERT INTO users (email) VALUES
    ('alice@example.com'),
    ('bob@example.com'),
    ('carol@example.com');

INSERT INTO orders (user_id, total_cents) VALUES
    (1, 1999),
    (1, 4999),
    (2, 2500),
    (3, 9999);

INSERT INTO payments (order_id, amount_cents, status) VALUES
    (1, 1999, 'paid'),
    (2, 4999, 'paid'),
    (3, 2500, 'paid'),
    (4, 9999, 'paid');
