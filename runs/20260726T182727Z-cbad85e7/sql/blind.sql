select
    customer_key,
    given_name,
    family_name,
    coalesce(lifetime_value, 0) as lifetime_value,
    ntile(5) over (order by coalesce(lifetime_value, 0)) as value_quintile
from {{ ref('customers') }}
order by value_quintile, customer_key
