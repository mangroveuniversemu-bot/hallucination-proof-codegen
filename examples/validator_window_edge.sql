with percentile_buckets as (
    select
        customer_id,
        ntile(5) over (order by clv) as clv_band
    from {{ ref('customers') }}
)

select
    clv_band,
    count(*) as customer_count
from percentile_buckets
group by clv_band
order by clv_band
