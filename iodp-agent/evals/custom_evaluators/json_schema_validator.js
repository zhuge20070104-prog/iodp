// evals/custom_evaluators/json_schema_validator.js
// 自定义 Promptfoo 评估器：验证 BugReport JSON Schema 合法性
// 安装依赖：npm install ajv ajv-formats

const Ajv = require('ajv');
const addFormats = require('ajv-formats');

const ajv = new Ajv({ allErrors: true });
addFormats(ajv);

const BUG_REPORT_SCHEMA = {
  type: 'object',
  required: [
    'report_id',
    'generated_at',
    'severity',
    'affected_service',
    'affected_user_id',
    'root_cause',
    'error_codes',
    'recommended_fix',
    'confidence_score',
  ],
  properties: {
    report_id: {
      type: 'string',
      // UUID v4 format
      pattern: '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    },
    generated_at: {
      type: 'string',
      description: 'ISO 8601 timestamp',
    },
    severity: {
      type: 'string',
      enum: ['P0', 'P1', 'P2', 'P3'],
    },
    affected_service: {
      type: 'string',
      minLength: 1,
    },
    affected_user_id: {
      type: 'string',
    },
    incident_time_range: {
      type: 'object',
      properties: {
        start: { type: 'string' },
        end: { type: 'string' },
      },
    },
    root_cause: {
      type: 'string',
      minLength: 5,
      description: 'Must be a meaningful explanation, not just "unknown"',
    },
    error_codes: {
      type: 'array',
      items: { type: 'string' },
    },
    evidence_trace_ids: {
      type: 'array',
      items: { type: 'string' },
    },
    error_rate_at_incident: {
      type: 'number',
      minimum: 0.0,
      maximum: 1.0,
    },
    reproduction_steps: {
      type: 'array',
      items: { type: 'string' },
    },
    recommended_fix: {
      type: 'string',
      minLength: 10,
    },
    kb_references: {
      type: 'array',
      items: { type: 'string' },
    },
    confidence_score: {
      type: 'number',
      minimum: 0.0,
      maximum: 1.0,
    },
  },
  additionalProperties: true,
};

const validate = ajv.compile(BUG_REPORT_SCHEMA);

module.exports = {
  async evaluate({ output, context, vars }) {
    // Parse bug_report from output
    let bugReport;
    try {
      const parsedOutput = typeof output === 'string' ? JSON.parse(output) : output;
      bugReport = parsedOutput.bug_report || parsedOutput;
    } catch (e) {
      return {
        pass: false,
        score: 0,
        reason: `Failed to parse output as JSON: ${e.message}`,
      };
    }

    if (!bugReport || typeof bugReport !== 'object') {
      return {
        pass: false,
        score: 0,
        reason: 'bug_report is null, missing, or not an object',
      };
    }

    const valid = validate(bugReport);

    if (!valid) {
      const errors = validate.errors
        .map(e => `${e.instancePath || '(root)'}: ${e.message}`)
        .join('; ');
      return {
        pass: false,
        score: 0,
        reason: `BugReport schema validation failed: ${errors}`,
      };
    }

    // Extra semantic checks beyond schema
    const warnings = [];

    if (bugReport.root_cause === 'unknown' || bugReport.root_cause === '未知') {
      warnings.push('root_cause is just "unknown" — consider if confidence_score is appropriately low');
    }

    if (bugReport.confidence_score > 0.7 && bugReport.evidence_trace_ids?.length === 0) {
      warnings.push('High confidence but no evidence_trace_ids provided');
    }

    if (warnings.length > 0) {
      return {
        pass: true,
        score: 0.7,
        reason: `Schema valid but has semantic warnings: ${warnings.join('; ')}`,
      };
    }

    return {
      pass: true,
      score: 1.0,
      reason: 'BugReport schema and semantic validation passed',
    };
  },
};
