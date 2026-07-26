select
    date_trunc('month', checkout_ts_utc) as month_start,
    round(sum(gross_revenue_usd), 2) as total_revenue
from {{ ref('fct_cart_checkouts') }}
group by 1
order by month_start
