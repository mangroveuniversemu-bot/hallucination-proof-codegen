WITH percentiles AS (
    SELECT
        customer_id,
        customer_lifetime_value,
        NTILE(5) OVER (ORDER BY customer_lifetime_value) AS clv_band
    FROM {{ ref('customers') }}
)

SELECT
    clv_band,
    COUNT(*) AS customer_count,
    MIN(customer_lifetime_value) AS min_clv,
    AVG(customer_lifetime_value) AS avg_clv,
    MAX(customer_lifetime_value) AS max_clv
FROM percentiles
GROUP BY clv_band
ORDER BY clv_band
