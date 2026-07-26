select
    customer_id,
    count(*) as order_count,
    round(sum(total_amount), 2) as total_amount
from {{ ref('orders') }}
group by customer_id
order by total_amount desc, customer_id asc
limit 3
