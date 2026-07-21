BEGIN;

INSERT INTO contract_types (code, name)
VALUES
    ('PURCHASE', '采购合同'),
    ('SALES', '销售合同')
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name,
    enabled = TRUE;

INSERT INTO review_check_items (
    code,
    name,
    description,
    prompt_template,
    default_severity,
    sort_order
)
VALUES
    (
        'PAYMENT_TERMS',
        '付款条款检查',
        '检查付款条件、付款节点及付款比例是否存在风险。',
        '结合检索到的当前合同条款和企业制度，分析付款条款并输出结构化风险结论、依据和建议。',
        'HIGH',
        10
    ),
    (
        'DELIVERY',
        '交付条款检查',
        '检查交付时间、地点、方式及责任是否明确。',
        '结合检索到的当前合同条款和企业制度，分析交付约定并输出结构化风险结论、依据和建议。',
        'MEDIUM',
        20
    ),
    (
        'ACCEPTANCE',
        '验收条款检查',
        '检查验收标准、期限和异议机制是否明确。',
        '结合检索到的当前合同条款和企业制度，分析验收约定并输出结构化风险结论、依据和建议。',
        'MEDIUM',
        30
    ),
    (
        'BREACH_LIABILITY',
        '违约责任检查',
        '检查双方违约责任是否明确且相对合理。',
        '结合检索到的当前合同条款和企业制度，分析违约责任并输出结构化风险结论、依据和建议。',
        'HIGH',
        40
    ),
    (
        'TERMINATION',
        '解除终止检查',
        '检查解除条件、通知方式和终止后的责任。',
        '结合检索到的当前合同条款和企业制度，分析解除终止条款并输出结构化风险结论、依据和建议。',
        'MEDIUM',
        50
    ),
    (
        'DISPUTE_RESOLUTION',
        '争议解决检查',
        '检查适用法律、管辖法院或仲裁机构是否明确。',
        '结合检索到的当前合同条款和企业制度，分析争议解决条款并输出结构化风险结论、依据和建议。',
        'MEDIUM',
        60
    )
ON CONFLICT (code) DO UPDATE
SET name = EXCLUDED.name,
    description = EXCLUDED.description,
    prompt_template = EXCLUDED.prompt_template,
    default_severity = EXCLUDED.default_severity,
    sort_order = EXCLUDED.sort_order,
    enabled = TRUE;

INSERT INTO review_check_item_scopes (check_item_id, contract_type_id)
SELECT review_check_items.id, contract_types.id
FROM review_check_items
CROSS JOIN contract_types
WHERE review_check_items.enabled = TRUE
  AND contract_types.code IN ('PURCHASE', 'SALES')
ON CONFLICT DO NOTHING;

COMMIT;
