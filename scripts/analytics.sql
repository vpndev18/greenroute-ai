-- Aggregate the Azure LLM inference trace (1 week of production chat traffic)
-- and replay it through the GreenRoute policy.

CREATE OR REPLACE TABLE __DS__.demand_10min AS
SELECT TIMESTAMP_SECONDS(DIV(UNIX_SECONDS(TIMESTAMP), 600) * 600) AS ts,
       COUNT(*) AS requests,
       SUM(ContextTokens) AS in_tokens,
       SUM(GeneratedTokens) AS out_tokens
FROM __DS__.trace_raw
GROUP BY ts;

-- Policy: right-size the model by request size, run in a green region.
-- Energy: Wh = PUE * (in*c/10 + out*c) / 1000, c = Wh per 1k output tokens per tier.
-- Baseline: every request on the large model in Mumbai (648 gCO2/kWh, Google 2023 data).
CREATE OR REPLACE TEMP TABLE replay AS
SELECT ContextTokens AS ctx, GeneratedTokens AS gen, EXTRACT(HOUR FROM TIMESTAMP) AS hour,
       CASE WHEN ContextTokens <= 1000 AND GeneratedTokens <= 150 THEN 'lite'
            WHEN ContextTokens <= 4000 AND GeneratedTokens <= 600 THEN 'flash'
            ELSE 'pro' END AS tier
FROM __DS__.trace_raw;

CREATE OR REPLACE TEMP TABLE replay_e AS
SELECT *,
  1.09 * (ctx * 2.5 / 10 + gen * 2.5) / 1000 AS base_wh,
  1.09 * (ctx * c / 10 + gen * c) / 1000 AS gr_wh
FROM (SELECT *, CASE tier WHEN 'lite' THEN 0.25 WHEN 'flash' THEN 0.6 ELSE 2.5 END AS c FROM replay);

CREATE OR REPLACE TABLE __DS__.fleet_summary AS
WITH ci AS (
  SELECT MAX(IF(region = 'asia-south1', intensity, NULL)) AS mumbai,
         MAX(IF(region = 'europe-north1', intensity, NULL)) AS finland
  FROM __DS__.region_carbon)
SELECT COUNT(*) AS requests,
       SUM(ctx + gen) AS total_tokens,
       SUM(base_wh) / 1000 * ANY_VALUE(ci.mumbai) / 1000 AS baseline_kg,
       SUM(gr_wh) / 1000 * ANY_VALUE(ci.finland) / 1000 AS greenroute_kg,
       SUM(gr_wh) / 1000 * ANY_VALUE(ci.mumbai) / 1000 AS india_kg,
       ROUND(100 * (1 - SUM(gr_wh) * ANY_VALUE(ci.finland) / (SUM(base_wh) * ANY_VALUE(ci.mumbai))), 1) AS saved_pct_global,
       ROUND(100 * (1 - SUM(gr_wh) / SUM(base_wh)), 1) AS saved_pct_india
FROM replay_e, ci;

CREATE OR REPLACE TABLE __DS__.fleet_by_tier AS
SELECT tier, COUNT(*) AS requests, ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM replay_e GROUP BY tier;

CREATE OR REPLACE TABLE __DS__.fleet_by_hour AS
SELECT hour, COUNT(*) AS requests FROM replay_e GROUP BY hour;
