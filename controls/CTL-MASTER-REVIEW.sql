-- remediation: Add same or different with a one-line reason for {source_a} / {source_b} (score {score}) to config/customer_matches.yaml, then run c2c signoff.
-- stage: mart
-- Every pair of customer-name variants that scores in the doubt band needs a reviewed decision.
-- The candidates carry a NULL decision exactly when the pair is in the doubt band and has none
-- (a pair below the band is 'none', one above it 'auto').

select
    system_a,
    source_a,
    system_b,
    source_b,
    score,
    rules_fired
from mart.fct_match_candidates
where decision is null
order by system_a, source_a, system_b, source_b
