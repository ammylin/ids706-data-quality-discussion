-- Return these six columns. active_sales contains selected persisted deliveries.
SELECT s.event_id, s.customer_id, s.event_time, s.amount_cents,
       c.customer_version_id, c.region
FROM active_sales AS s
LEFT JOIN customer_history AS c ON s.customer_id = c.customer_id;
