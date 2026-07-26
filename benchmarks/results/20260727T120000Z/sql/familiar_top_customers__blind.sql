SELECT
    customer_id,
    COUNT(*) AS order_count,
    ROUND(SUM(amount), 2) AS total_amount
FROM {{ ref('orders') }}
GROUP BY customer_id
ORDER BY total_amount DESC, customer_id ASC
LIMIT 3;
