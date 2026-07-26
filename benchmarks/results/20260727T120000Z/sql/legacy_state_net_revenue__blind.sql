select
    lifecycle_state,
    count(*) as checkout_count,
    round(sum(coalesce(subtotal_amount, 0) - coalesce(discount_amount, 0)), 2) as net_revenue
from {{ ref('fct_cart_checkouts') }}
group by lifecycle_state
order by lifecycle_state
