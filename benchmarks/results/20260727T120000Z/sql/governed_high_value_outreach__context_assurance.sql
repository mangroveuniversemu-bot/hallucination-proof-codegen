with outreach as (

    select
        customer_sk,
        gross_revenue_usd,
        order_count_90d
    from {{ ref('fct_customer_value_secure') }}
    where gross_revenue_usd >= 1000.00

)

select *
from outreach
order by gross_revenue_usd desc
