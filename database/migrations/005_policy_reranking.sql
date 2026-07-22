-- 为制度侧重排序补充运行级和命中级追踪字段。
-- 本迁移只增加可空字段，不改写既有风险审查记录。
ALTER TABLE retrieval_runs
    ADD COLUMN IF NOT EXISTS final_top_k INTEGER,
    ADD COLUMN IF NOT EXISTS ranking_strategy VARCHAR(32) NOT NULL DEFAULT 'VECTOR',
    ADD COLUMN IF NOT EXISTS rerank_model VARCHAR(128),
    ADD COLUMN IF NOT EXISTS rerank_latency_ms INTEGER,
    ADD COLUMN IF NOT EXISTS rerank_error TEXT;

ALTER TABLE retrieval_hits
    ADD COLUMN IF NOT EXISTS rerank_rank_no INTEGER,
    ADD COLUMN IF NOT EXISTS rerank_score DOUBLE PRECISION;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'retrieval_runs_final_top_k_check'
    ) THEN
        ALTER TABLE retrieval_runs
            ADD CONSTRAINT retrieval_runs_final_top_k_check
            CHECK (final_top_k IS NULL OR final_top_k > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'retrieval_runs_ranking_strategy_check'
    ) THEN
        ALTER TABLE retrieval_runs
            ADD CONSTRAINT retrieval_runs_ranking_strategy_check
            CHECK (ranking_strategy IN ('VECTOR', 'RERANK', 'RERANK_FALLBACK'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'retrieval_runs_rerank_latency_ms_check'
    ) THEN
        ALTER TABLE retrieval_runs
            ADD CONSTRAINT retrieval_runs_rerank_latency_ms_check
            CHECK (rerank_latency_ms IS NULL OR rerank_latency_ms >= 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'retrieval_hits_rerank_rank_no_check'
    ) THEN
        ALTER TABLE retrieval_hits
            ADD CONSTRAINT retrieval_hits_rerank_rank_no_check
            CHECK (rerank_rank_no IS NULL OR rerank_rank_no > 0);
    END IF;
END
$$;
