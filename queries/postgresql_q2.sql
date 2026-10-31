SET work_mem = '4MB';

SELECT data->>'order_id' AS order_id,
      data->'customer'->>'id' AS customer_id,
      (data->'payment'->'totals'->>'total')::numeric AS final_total
FROM orders
WHERE data->'payment'->>'status' = 'completed'
  AND (data->>'timestamp')::timestamptz >= '2026-01-01 00:00:00Z'
ORDER BY (data->'payment'->'totals'->>'total')::numeric DESC
LIMIT 1000;
