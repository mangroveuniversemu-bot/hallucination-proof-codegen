with percentile_buckets as (
    select
        customer_id,
        clv,
        ntile(5) over (order by clv) as clv_bucket
    from {{ ref('customers') }}
)

select
    clv_bucket,
    count(*) as customer_count,
    min(clv) as min_clv,
    round(avg(clv), 2) as avg_clv,
    max(clv) as max_clv
from percentile_buckets
group by clv_bucket
order by clv_bucket
