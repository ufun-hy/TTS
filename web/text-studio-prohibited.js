// The browser and Python load the same rule data; exclusions never edit source text.
const TextStudioProhibited = (() => {
  const config = typeof module !== 'undefined'
    ? require('../config/text-studio-prohibited.json') : TextStudioProhibitedRules;
  const rules = config.rules.map(rule => ({...rule, re: new RegExp(rule.pattern, 'g')}));
  const negation = new RegExp(config.negation_pattern);
  function analyze(text) {
    const blocked = [], kept = [];
    let cursor = 0;
    for (const sentence of text.matchAll(new RegExp(config.sentence_pattern, 'g'))) {
      const raw = sentence[0], normalized = raw.normalize('NFKC').replace(/\s+/g, '');
      const labels = [];
      for (const rule of rules) {
        rule.re.lastIndex = 0;
        for (const hit of normalized.matchAll(rule.re)) {
          if (rule.negation_sensitive && negation.test(normalized.slice(0, hit.index))) continue;
          labels.push(rule.label);
          break;
        }
      }
      if (labels.length) {
        kept.push(text.slice(cursor, sentence.index));
        cursor = sentence.index + raw.length;
        blocked.push({start: sentence.index, end: cursor, text: raw, labels});
      }
    }
    kept.push(text.slice(cursor));
    let safe = kept.join('').trim();
    if (!/[\p{L}\p{N}]/u.test(safe)) safe = '';
    return {text: safe, blocked};
  }
  return {analyze};
})();
if (typeof module !== 'undefined') module.exports = TextStudioProhibited;
