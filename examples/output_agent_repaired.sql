WITH segmented_customers AS (
    SELECT
        customer_id,
        customer_lifetime_value,
        NTILE(5) OVER (ORDER BY customer_lifetime_value) AS clv_band
    FROM {{ ref('customers') }}
)

SELECT
    customer_id,
    customer_lifetime_value,
    clv_band
FROM segmented_customers
ORDER BY clv_band, customer_id
