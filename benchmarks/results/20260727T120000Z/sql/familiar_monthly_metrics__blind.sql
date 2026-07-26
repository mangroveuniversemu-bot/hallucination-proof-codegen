with monthly as (
    select
        date_trunc('month', order_date) as month_start,
        count(*) as order_count,
        avg(order_amount) as average_order_value
    from {{ ref('orders') }}
    group by 1
)

select
    month_start,
    order_count,
    round(average_order_value, 2) as average_order_value
from monthly
order by month_start
