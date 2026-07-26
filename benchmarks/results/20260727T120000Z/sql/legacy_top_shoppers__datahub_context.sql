WITH shopper_revenue AS (
    SELECT
        shopper_fk AS shopper_id,
        COUNT(*) AS checkout_count,
        ROUND(SUM(gross_revenue_usd), 2) AS total_revenue
    FROM {{ ref('fct_cart_checkouts') }}
    GROUP BY shopper_fk
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            ORDER BY total_revenue DESC, shopper_id ASC
        ) AS rn
    FROM shopper_revenue
)
SELECT
    shopper_id,
    checkout_count,
    total_revenue
FROM ranked
WHERE rn <= 2
ORDER BY total_revenue DESC, shopper_id ASC
