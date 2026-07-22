-- 已有数据库不会重新执行 database/init，因此通过迁移补充审批幂等约束。
-- 同一份风险审查报告只能创建一个审批实例，避免重复点击或并发请求产生两套流程。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_approval_instances_review_run'
          AND conrelid = 'approval_instances'::regclass
    ) THEN
        ALTER TABLE approval_instances
            ADD CONSTRAINT uq_approval_instances_review_run UNIQUE (review_run_id);
    END IF;
END
$$;
