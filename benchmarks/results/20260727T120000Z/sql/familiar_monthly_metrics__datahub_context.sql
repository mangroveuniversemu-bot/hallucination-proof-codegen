select
    date_trunc('month', order_date) as month_start,
    count(*) as order_count,
    round(avg(total_amount), 2) as average_order_value
from {{ ref('orders') }}
group by 1
order by 1
