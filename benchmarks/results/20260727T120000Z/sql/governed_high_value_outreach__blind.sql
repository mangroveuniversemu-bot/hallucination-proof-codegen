SELECT
    customer_name,
    customer_email,
    revenue,
    orders_90_day_count
FROM {{ ref('fct_customer_value_secure') }}
WHERE revenue >= 1000
ORDER BY revenue DESC;
