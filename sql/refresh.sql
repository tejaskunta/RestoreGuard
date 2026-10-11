-- Simulate recent application activity on the "high write" table.
--
-- The demo fixture in schema.sql is STATIC: it stamps occurred_at at seed
-- time, so the newest row ages and the freshness check eventually (correctly)
-- fails. Real sources keep writing; this stands in for that.

UPDATE order_events
SET occurred_at = now() - (id % 60) * interval '1 minute';