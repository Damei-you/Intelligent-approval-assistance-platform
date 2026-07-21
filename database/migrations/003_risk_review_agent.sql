BEGIN;

-- 已有数据库不会重新执行 init 脚本，因此通过迁移补充质保检查并收敛为演示所需四项。
INSERT INTO review_check_items (
    code, name, description, prompt_template, default_severity, sort_order, enabled
)
VALUES (
    'WARRANTY',
    '质保条款检查',
    '检查质保范围、质保期限和售后责任是否符合制度要求。',
    '结合检索到的当前合同条款和企业制度，分析质保条款并输出结构化风险结论、依据和建议。',
    'MEDIUM',
    20,
    TRUE
)
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name,
    description = EXCLUDED.description,
    prompt_template = EXCLUDED.prompt_template,
    default_severity = EXCLUDED.default_severity,
    sort_order = EXCLUDED.sort_order,
    enabled = TRUE;

UPDATE review_check_items
SET enabled = CASE
    WHEN code IN ('PAYMENT_TERMS', 'WARRANTY', 'BREACH_LIABILITY', 'DISPUTE_RESOLUTION') THEN TRUE
    ELSE FALSE
END;

UPDATE review_check_items SET sort_order = 10 WHERE code = 'PAYMENT_TERMS';
UPDATE review_check_items SET sort_order = 20 WHERE code = 'WARRANTY';
UPDATE review_check_items SET sort_order = 30 WHERE code = 'BREACH_LIABILITY';
UPDATE review_check_items SET sort_order = 40 WHERE code = 'DISPUTE_RESOLUTION';

INSERT INTO review_check_item_scopes (check_item_id, contract_type_id)
SELECT item.id, type.id
FROM review_check_items item
CROSS JOIN contract_types type
WHERE item.code IN ('PAYMENT_TERMS', 'WARRANTY', 'BREACH_LIABILITY', 'DISPUTE_RESOLUTION')
  AND type.code IN ('PURCHASE', 'SALES')
ON CONFLICT DO NOTHING;

COMMIT;
