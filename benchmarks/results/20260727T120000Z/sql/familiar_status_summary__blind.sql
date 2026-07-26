select
    status,
    count(*) as order_count,
    round(sum(total_amount), 2) as total_revenue
from {{ ref('orders') }}
group by status
order by status
