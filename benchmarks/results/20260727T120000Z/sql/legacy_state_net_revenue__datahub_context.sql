with source as (
    select
        checkout_state_cd as lifecycle_state,
        (gross_revenue_usd - discount_value_usd) as net_revenue_raw
    from {{ ref('fct_cart_checkouts') }}
)

select
    lifecycle_state,
    count(*) as checkout_count,
    round(sum(net_revenue_raw), 2) as net_revenue
from source
group by lifecycle_state
order by lifecycle_state;
