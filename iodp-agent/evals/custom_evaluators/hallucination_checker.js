// evals/custom_evaluators/hallucination_checker.js
// 自定义 Promptfoo 评估器：检测 BugReport 中的幻觉迹象
//
// 幻觉检测规则：
//   1. 高置信度但无证据 → 可能幻觉
//   2. 置信度与 root_cause 描述不一致 → 可能幻觉
//   3. severity 与 error_rate 不匹配 → 可能幻觉
//   4. root_cause 过于简短（< 20字）但置信度高 → 可能幻觉

module.exports = {
  async evaluate({ output, context, vars }) {
    // Parse output
    let bugReport;
    try {
      const parsed = typeof output === 'string' ? JSON.parse(output) : output;
      bugReport = parsed.bug_report || parsed;
    } catch (e) {
      // If output isn't JSON, skip hallucination check
      return { pass: true, score: 1.0, reason: 'Output is not JSON, skipping hallucination check' };
    }

    if (!bugReport || typeof bugReport !== 'object') {
      return { pass: true, score: 1.0, reason: 'No bug_report to check' };
    }

    const rootCause       = bugReport.root_cause || '';
    const errorCodes      = bugReport.error_codes || [];
    const evidenceIds     = bugReport.evidence_trace_ids || [];
    const confidence      = parseFloat(bugReport.confidence_score) || 0;
    const severity        = bugReport.severity || '';

    const issues = [];
    const warnings = [];

    // ── Rule 1: 置信度与 root_cause 的不一致性 ──
    const insufficientPhrases = ['证据不足', '需进一步排查', 'insufficient evidence', 'unknown', '未知'];
    const hasInsufficiencyMarker = insufficientPhrases.some(p =>
      rootCause.toLowerCase().includes(p.toLowerCase())
    );

    if (confidence > 0.7 && hasInsufficiencyMarker) {
      issues.push(
        `Confidence ${confidence} is high but root_cause indicates insufficient evidence: "${rootCause.substring(0, 50)}"`
      );
    }

    if (confidence < 0.3 && !hasInsufficiencyMarker && rootCause.length > 50) {
      warnings.push(
        `Confidence ${confidence} is low but root_cause appears detailed — consider if confidence should be higher`
      );
    }

    // ── Rule 2: 高置信度但缺乏支撑证据 ──
    if (confidence > 0.5 && evidenceIds.length === 0 && errorCodes.length === 0) {
      issues.push(
        `Confidence ${confidence} > 0.5 but no evidence_trace_ids or error_codes provided`
      );
    }

    // ── Rule 3: root_cause 内容过于简短 ──
    if (confidence > 0.6 && rootCause.replace(/\s/g, '').length < 15) {
      issues.push(
        `Root cause is too brief (${rootCause.length} chars) for confidence ${confidence}: "${rootCause}"`
      );
    }

    // ── Rule 4: severity 与 error_rate 不一致（如果 vars 中提供了 error_rate）──
    if (vars && vars.error_rate !== undefined) {
      const rateStr = String(vars.error_rate).replace('%', '');
      const rate = parseFloat(rateStr) / (rateStr.includes('%') || parseFloat(rateStr) <= 1 ? 1 : 100);
      const normalizedRate = rate > 1 ? rate / 100 : rate;  // normalize to 0-1

      if (normalizedRate > 0.20 && severity !== 'P0') {
        issues.push(
          `Error rate ${(normalizedRate * 100).toFixed(1)}% > 20% should be P0, but severity is ${severity}`
        );
      }
      if (normalizedRate < 0.01 && (severity === 'P0' || severity === 'P1')) {
        issues.push(
          `Error rate ${(normalizedRate * 100).toFixed(2)}% < 1% should not be P0/P1, but severity is ${severity}`
        );
      }
    }

    // ── Rule 5: recommended_fix 不应与 root_cause 矛盾 ──
    const recommendedFix = bugReport.recommended_fix || '';
    if (
      rootCause.toLowerCase().includes('database') &&
      recommendedFix.toLowerCase().includes('restart frontend')
    ) {
      issues.push('recommended_fix (restart frontend) contradicts root_cause (database issue)');
    }

    // ── 返回评估结果 ──
    if (issues.length > 0) {
      return {
        pass: false,
        score: Math.max(0, 1 - issues.length * 0.3),
        reason: `Hallucination indicators detected: ${issues.join(' | ')}`,
      };
    }

    if (warnings.length > 0) {
      return {
        pass: true,
        score: 0.8,
        reason: `No critical hallucination but has warnings: ${warnings.join(' | ')}`,
      };
    }

    return {
      pass: true,
      score: 1.0,
      reason: 'No hallucination indicators detected',
    };
  },
};
