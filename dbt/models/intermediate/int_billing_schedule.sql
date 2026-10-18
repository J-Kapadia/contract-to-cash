-- The invoices the contracts call for, one row per contract line and billing event.
--
-- Subscription lines bill in advance, annual or quarterly from term_start, at the latest contract
-- version effective on each period's first day. An amendment that takes effect inside a period
-- adds a stub dated its effective date for the rest of that period: the change in the period's
-- price less its elapsed part (a decrease is negative, a credit). Overage bills each usage month
-- on the month's last day: (units - included units) x the rate, when positive.
--
-- `source`, `as_of` and `case` let forecast contracts join this model later; these rows are the
-- actual source, with neither an as-of date nor a case.
--
-- Where the contracts are silent: the billing grid (frequency and term) is the first version's;
-- a period that is cut short by the term end still bills its full price; a stub ends with the
-- period it falls in, which is the contract's term end for a one-period contract; only the first
-- overage line (by id) of the governing version bills usage.

with versions as (
    select
        contract_id,
        version,
        effective_from,
        lag(version) over (partition by contract_id order by version) as prev_version
    from {{ ref('stg_contracts') }}
),

first_version as (
    select
        contract_id,
        crm_account_id as customer_id,
        term_start,
        term_end,
        billing_frequency,
        case billing_frequency when 'annual' then 12 else 3 end as step_months
    from {{ ref('stg_contracts') }}
    qualify row_number() over (partition by contract_id order by version) = 1
),

periods as (
    select
        first_version.contract_id,
        first_version.customer_id,
        first_version.billing_frequency,
        grid.n as period_index,
        (first_version.term_start + to_months(grid.n * first_version.step_months))::date as period_start,
        least(
            (first_version.term_start + to_months((grid.n + 1) * first_version.step_months))::date - 1,
            first_version.term_end
        ) as period_end
    from first_version
    cross join range(0, 400) as grid(n)
    where (first_version.term_start + to_months(grid.n * first_version.step_months))::date <= first_version.term_end
),

subscription_fees as (
    select contract_id, version, line_id, annual_fee_cents
    from {{ ref('stg_contract_lines') }}
    where product = 'subscription'
),

-- A quarter bills a quarter of the annual fee; the four sum to the fee exactly.
quarter_prices as (
    select
        subscription_fees.contract_id,
        subscription_fees.version,
        subscription_fees.line_id,
        quarters.quarter,
        {{ split_exact('subscription_fees.annual_fee_cents', 'quarters.weight_bps', 'quarters.quarter',
                       'subscription_fees.contract_id, subscription_fees.version, subscription_fees.line_id') }}
            as quarter_cents
    from subscription_fees
    cross join (values (0, 2500), (1, 2500), (2, 2500), (3, 2500)) as quarters(quarter, weight_bps)
),

-- What each version's line would bill in each billing period.
period_prices as (
    select
        periods.contract_id,
        periods.customer_id,
        periods.period_index,
        periods.period_start,
        periods.period_end,
        subscription_fees.version,
        subscription_fees.line_id,
        case periods.billing_frequency
            when 'annual' then subscription_fees.annual_fee_cents
            else quarter_prices.quarter_cents
        end as price_cents
    from periods
    inner join subscription_fees on subscription_fees.contract_id = periods.contract_id
    left join quarter_prices
        on quarter_prices.contract_id = subscription_fees.contract_id
        and quarter_prices.version = subscription_fees.version
        and quarter_prices.line_id = subscription_fees.line_id
        and quarter_prices.quarter = periods.period_index % 4
),

governing_period_version as (
    select
        periods.contract_id,
        periods.period_index,
        coalesce(max(versions.version) filter (where versions.effective_from <= periods.period_start),
                 min(versions.version)) as version
    from periods
    inner join versions on versions.contract_id = periods.contract_id
    group by periods.contract_id, periods.period_index
),

regular as (
    select
        customer_id,
        contract_id,
        line_id,
        period_start as billed_on,
        period_start as starts_on,
        period_end as ends_on,
        price_cents as cents
    from period_prices
    inner join governing_period_version using (contract_id, period_index, version)
    where price_cents <> 0
),

-- Each amendment that takes effect after a period began and on or before its last day.
amendment_periods as (
    select
        versions.contract_id,
        versions.version,
        versions.prev_version,
        versions.effective_from,
        periods.period_index,
        periods.customer_id,
        periods.period_start as grid_start,
        periods.period_end as grid_end
    from versions
    inner join periods on periods.contract_id = versions.contract_id
    where versions.prev_version is not null
        and periods.period_start < versions.effective_from
        and versions.effective_from <= periods.period_end
),

changes as (
    select
        amendment_periods.customer_id,
        amendment_periods.contract_id,
        amendment_periods.effective_from,
        amendment_periods.grid_start,
        amendment_periods.grid_end,
        lines.line_id,
        coalesce(newer.price_cents, 0) - coalesce(older.price_cents, 0) as change_cents
    from amendment_periods
    inner join (select distinct contract_id, line_id from subscription_fees) as lines
        on lines.contract_id = amendment_periods.contract_id
    left join period_prices as newer
        on newer.contract_id = amendment_periods.contract_id
        and newer.period_index = amendment_periods.period_index
        and newer.version = amendment_periods.version
        and newer.line_id = lines.line_id
    left join period_prices as older
        on older.contract_id = amendment_periods.contract_id
        and older.period_index = amendment_periods.period_index
        and older.version = amendment_periods.prev_version
        and older.line_id = lines.line_id
    where newer.line_id is not null or older.line_id is not null
),

-- The stub is the change less the part of it elapsed before the effective date, by the ratable
-- definition, so the stub and the elapsed part sum to the change exactly.
stubs as (
    select
        customer_id,
        contract_id,
        line_id,
        effective_from as billed_on,
        effective_from as starts_on,
        grid_end as ends_on,
        case when change_cents > 0 then 1 else -1 end * (
            abs(change_cents)
            - {{ c2c_cumulative_cents('abs(change_cents)', 'grid_start', 'grid_end', '(effective_from - 1)') }}
        ) as cents
    from changes
    where change_cents <> 0
),

overage_lines as (
    select
        contract_id,
        version,
        line_id,
        included_units,
        overage_cents_per_unit
    from {{ ref('stg_contract_lines') }}
    where product = 'overage'
    qualify row_number() over (partition by contract_id, version order by line_id) = 1
),

usage_months as (
    select
        stg_usage.contract_id,
        first_version.customer_id,
        stg_usage.units,
        (stg_usage.month || '-01')::date as month_first,
        last_day((stg_usage.month || '-01')::date) as month_last
    from {{ ref('stg_usage') }}
    inner join first_version on first_version.contract_id = stg_usage.contract_id
),

governing_usage_version as (
    select
        usage_months.contract_id,
        usage_months.month_last,
        coalesce(max(versions.version) filter (where versions.effective_from <= usage_months.month_last),
                 min(versions.version)) as version
    from usage_months
    inner join versions on versions.contract_id = usage_months.contract_id
    group by usage_months.contract_id, usage_months.month_last
),

overage as (
    select
        usage_months.customer_id,
        usage_months.contract_id,
        overage_lines.line_id,
        usage_months.month_last as billed_on,
        usage_months.month_first as starts_on,
        usage_months.month_last as ends_on,
        (usage_months.units - overage_lines.included_units) * overage_lines.overage_cents_per_unit as cents
    from usage_months
    inner join governing_usage_version using (contract_id, month_last)
    inner join overage_lines
        on overage_lines.contract_id = usage_months.contract_id
        and overage_lines.version = governing_usage_version.version
    where usage_months.units > overage_lines.included_units
        and overage_lines.overage_cents_per_unit <> 0
),

events as (
    select * from regular
    union all
    select * from stubs
    union all
    select * from overage
)

select
    'actual' as source,
    null::date as as_of,
    null::varchar as "case",
    customer_id,
    contract_id,
    line_id,
    billed_on as invoice_date,
    starts_on as service_start,
    ends_on as service_end,
    cents::bigint as amount_cents
from events
