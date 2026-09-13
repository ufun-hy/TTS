// Risk rules scan final text; UI and persistence remain in text-studio.html.
const TextStudioRisk = (() => {
  const riskRules=[{type:'物流',label:'发货时间承诺',severity:'high',re:/当天发货|今天发货|明天发货|马上发货|立即发货|极速发货|次日达|(?:24|48|72|[0-9一二三四五六七八九十]+)小时内发货|[0-9一二三四五六七八九十]+天内发货/g,reason:'明确承诺具体发货或送达时效。'},
    {type:'运费险',label:'运费险承诺',severity:'medium',re:/运费险/g,reason:'涉及运费险，需要确认实际订单和平台规则。'},
    {type:'售后',label:'无理由 / 退款承诺',severity:'high',re:/七天无理由|7天无理由|无条件退款|无条件退/g,reason:'涉及无理由退货或无条件退款承诺。'},
    {type:'承诺',label:'绝对化承诺',severity:'high',re:/绝对|保证|百分百|百分之百|100%|一定|肯定|绝不|永久|必定|必须有效/g,reason:'使用绝对化或保证性表达。'},
    {type:'夸大',label:'最高级 / 排他性表达',severity:'medium',re:/全网最低|最低价|最便宜|最好|第一|顶级|最强|唯一|行业第一|全网第一|史上最低|天花板/g,reason:'可能构成最高级、排他性或无法验证的宣传。'},
    {type:'效果',label:'效果承诺',severity:'high',re:/根治|治愈|药到病除|立刻见效|马上见效|绝对有效|无副作用/g,reason:'对效果作确定性或医疗化承诺。'}],softQualifiers=/以[^。！？!?]{0,18}为准|具体[^。！？!?]{0,18}为准|页面显示|订单页面|实际情况|预计|一般情况下|通常情况下|可能|视情况|根据[^。！？!?]{0,18}情况/,strongCommit=/保证|一定|肯定|必|都有|全都有|赠送|送你|包|无条件|百分百|100%/;

  function sentenceRanges(text) {
    const ranges = [];
    for (const match of text.matchAll(/[^。！？!?]+[。！？!?]?/g)) {
      const value = match[0].trim();
      if (value) ranges.push({text: value, start: match.index + match[0].indexOf(value)});
    }
    return ranges;
  }
  const number = '[0-9零〇一二两三四五六七八九十百]+';
  const daypart = '(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚间|今晚)';
  const clock = `${number}\\s*(?:点(?:半|一刻|三刻|\\s*${number}\\s*分?)?|[:：][0-5][0-9])`;
  const date = `(?:${number}\\s*年\\s*)?${number}\\s*月\\s*${number}\\s*[日号]`;
  const duration = `(?:${number}|半)\\s*(?:个半?小时|小时|分钟|秒钟|秒)`;
  const event = '(?:结束|下播|收播|截止|开播|开抢|下架|恢复原价)';
  riskRules.push({
    type: '时间点', label: '时间点 / 倒计时', severity: 'medium',
    re: new RegExp([
      `(?:现在|此刻|目前)\\s*(?:已经是|已经|是)?\\s*${daypart}?\\s*${clock}`,
      `${daypart}\\s*${clock}`,
      `(?:今天|今日)\\s*(?:的日期)?\\s*(?:是|为)?\\s*${date}`,
      `(?:还有|还剩|剩下|最后)\\s*${duration}\\s*(?:就|后|以后)?\\s*(?:我们|本场直播|直播|活动|优惠)?\\s*(?:就)?${event}`,
      `(?:距离|离)${event}\\s*(?:还有|还剩|只剩)?\\s*${duration}`,
      `${duration}\\s*(?:后|以后)\\s*(?:就)?${event}`,
      `${clock}\\s*(?:准时|就|正式)?${event}`,
    ].join('|'), 'g'),
    reason: '包含具体时刻、当日日期或直播倒计时，循环播放时可能与真实时间不符。',
    suggestion: '建议去掉固定时间或倒计时，改为“欢迎来到直播间”“活动安排以页面实时信息为准”。',
  }, {
    type: '售后赔付', label: '售后赔付承诺', severity: 'high',
    re: new RegExp(`假一赔${number}|(?:坏果|烂果|破损|不满意|有问题)(?:我们)?(?:都|就|直接|一律|马上|全额|给你|给您|给大家|包){0,3}赔(?:付|偿)?|(?:无条件|无理由|全额|直接|包)赔(?:付|偿)?|赔付|赔偿`, 'g'),
    reason: '涉及包赔、倍数赔偿或无条件赔付等强售后承诺，需要确认实际政策及适用条件。',
    suggestion: '建议改为“如有售后问题，请联系客服按平台规则处理”，明确适用条件，避免无条件或倍数赔付承诺。',
  });

  // Keep qualification local: unrelated clauses must not soften a firm promise.
  function compensationSeverity(sentence, match) {
    const before = sentence.slice(0, match.index).split(/[，,；;：:\n]/).pop();
    if (/(?:不|未|不能|不会|无法|不再|没有|并非|不作|不做)(?:承诺|保证|支持|提供)?\s*$/.test(before)) return null;
    const strong = !/^(?:赔付|赔偿)$/.test(match[0]);
    if (strong || /(?:保证|一定|肯定|一律|马上|立即|都会|会|将|负责|给你|给您)\s*$/.test(before)) return 'high';
    return 'low';
  }

  function scan(text) {
    const findings = [];
    for (const sentence of sentenceRanges(text)) {
      for (const rule of riskRules) {
        rule.re.lastIndex = 0;
        for (const match of sentence.text.matchAll(rule.re)) {
          const qualified = softQualifiers.test(sentence.text);
          let severity = rule.severity;
          if (rule.type === '售后赔付') {
            severity = compensationSeverity(sentence.text, match);
            if (!severity) continue;
          } else if (rule.type !== '时间点') {
            if (rule.type === '运费险' && !strongCommit.test(sentence.text)) severity = 'low';
            else if (qualified && severity === 'high' && !strongCommit.test(sentence.text)) severity = 'medium';
            else if (qualified && severity === 'medium') severity = 'low';
          }
          findings.push({
            position: sentence.start + match.index, phrase: match[0], context: sentence.text,
            type: rule.type, label: rule.label, severity,
            reason: rule.type === '售后赔付' && severity === 'low'
              ? '提及赔付或赔偿流程，未识别到明确的强赔付承诺，可核对适用条件。' : rule.reason,
            suggestion: rule.suggestion || (qualified
              ? '已包含条件说明，建议确认事实后保留或进一步明确。'
              : '建议改为条件性、可验证表达，并以订单/平台实际信息为准。'),
            ignored: false,
          });
        }
      }
    }
    return findings;
  }
  return {scan};
})();
if (typeof module !== 'undefined') module.exports = TextStudioRisk;
