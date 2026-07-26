SELECT
    customer_name,
    risk_segment,
    gross_revenue
FROM {{ ref('fct_customer_value_secure') }}
WHERE risk_segment = 'HIGH'
ORDER BY gross_revenue DESC;
