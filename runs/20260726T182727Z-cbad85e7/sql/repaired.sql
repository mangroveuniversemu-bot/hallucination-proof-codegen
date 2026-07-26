select
    customer_id as customer_key,
    coalesce(customer_lifetime_value, 0) as lifetime_value,
    ntile(5) over (order by coalesce(customer_lifetime_value, 0) asc) as value_quintile
from {{ ref('customers') }}
order by value_quintile, customer_key
