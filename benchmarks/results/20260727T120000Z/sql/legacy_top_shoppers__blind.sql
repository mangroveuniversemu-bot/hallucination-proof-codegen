select
    shopper_id,
    count(*) as checkout_count,
    round(sum(gross_revenue), 2) as total_revenue
from {{ ref('fct_cart_checkouts') }}
group by shopper_id
order by total_revenue desc, shopper_id asc
limit 2
