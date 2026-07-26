SELECT
    risk_segment_cd,
    COUNT(*) AS customer_count,
    ROUND(SUM(gross_revenue_usd), 2) AS total_revenue_usd
FROM {{ ref('fct_customer_value_secure') }}
GROUP BY risk_segment_cd
ORDER BY risk_segment_cd
