SELECT
    DATE_TRUNC('month', checkout_ts) AS month_start,
    ROUND(SUM(revenue), 2) AS total_revenue
FROM {{ ref('fct_cart_checkouts') }}
GROUP BY DATE_TRUNC('month', checkout_ts)
ORDER BY month_start;
