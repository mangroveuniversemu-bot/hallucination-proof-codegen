select
    customer_name_txt,
    risk_segment_cd,
    gross_revenue_usd
from {{ ref('fct_customer_value_secure') }}
where risk_segment_cd = 'HIGH'
order by gross_revenue_usd desc
