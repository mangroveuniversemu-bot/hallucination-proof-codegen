select
    risk_segment_cd,
    count(distinct customer_id) as customer_count,
    round(coalesce(sum(revenue_usd), 0), 2) as total_revenue_usd
from {{ ref('fct_customer_value_secure') }}
group by risk_segment_cd
order by risk_segment_cd;
