(() => {
  if (window.__ttsTextStudioPricePatchLoaded) return;
  window.__ttsTextStudioPricePatchLoaded = true;

  const zhDigit = {'零':'0','〇':'0','一':'1','二':'2','两':'2','三':'3','四':'4','五':'5','六':'6','七':'7','八':'8','九':'9'};

  function parseChineseInteger(value) {
    const s = String(value || '');
    if (!s) return NaN;
    if (/^\d+$/.test(s)) return Number(s);
    let total = 0, current = 0, digitBuffer = '';
    for (const ch of s) {
      if (zhDigit[ch] != null) {
        current = Number(zhDigit[ch]);
        digitBuffer += zhDigit[ch];
      } else if (ch === '十') {
        total += (current || 1) * 10;
        current = 0;
        digitBuffer = '';
      } else if (ch === '百') {
        total += (current || 1) * 100;
        current = 0;
        digitBuffer = '';
      }
    }
    if (total) return total + current;
    return digitBuffer ? Number(digitBuffer) : NaN;
  }

  function canonicalPriceAdvanced(value) {
    const raw = String(value || '').trim().replace(/[￥¥\s]/g, '');
    if (!raw) return '';
    const block = raw.match(/^([0-9零〇一二两三四五六七八九十百]+)(?:块钱?|块|元)(.*)$/);
    if (block) {
      const integer = parseChineseInteger(block[1]);
      if (!Number.isFinite(integer)) return '';
      const tailDigits = [...block[2]].map(ch => (/\d/.test(ch) ? ch : (zhDigit[ch] ?? ''))).join('').slice(0, 2);
      return tailDigits ? `${integer}.${tailDigits}` : String(integer);
    }
    const direct = raw.match(/\d+(?:\.\d{1,2})?/);
    return direct ? String(Number(direct[0])) : '';
  }

  const explicitPricePattern = /(?:[￥¥]\s*)?(?:\d+(?:\.\d{1,2})?|[零〇一二两三四五六七八九十百]+)\s*(?:块钱?|块|元)(?:\s*(?:\d|[零〇一二两三四五六七八九])(?:\s*(?:毛|角))?(?:\s*(?:\d|[零〇一二两三四五六七八九])(?:分)?)?)?/g;
  const currencyPattern = /[￥¥]\s*\d+(?:\.\d{1,2})?/g;
  const bareDecimalPattern = /(?<![\d.])\d{1,5}\.\d{1,2}(?![\d.])/g;
  const strongPriceCue = /价格|价钱|售价|到手|只要|仅需|卖|拍|下单|拿下|带走|链接|秒杀|福利价|活动价|优惠价|现价|今日价|今天|现在|一单|一件|一份|一箱|一组|发\s*\d|到手\s*\d|整整\s*\d/;

  function isDiscountOrOtherAmount(sentence, index, phrase) {
    const before = sentence.slice(Math.max(0, index - 18), index);
    const after = sentence.slice(index + phrase.length, Math.min(sentence.length, index + phrase.length + 18));
    const around = before + phrase + after;
    if (/(?:运费|邮费|快递费|配送费|价值|面值)[^，。！？]{0,8}$/.test(before)) return true;
    if (/(?:补贴(?:了|给你|你)?|立减|省(?:了|下)?|优惠券|领券|券(?:后|减)?|减了|返现|返)[^，。！？]{0,7}$/.test(before)) return true;
    if (/^[^，。！？]{0,7}(?:的?券|优惠券|补贴|立减|返现|省下)/.test(after)) return true;
    if (/满[^，。！？]{0,8}(?:减|送)|(?:满|每满)[^，。！？]{0,8}减/.test(around)) return true;
    return false;
  }

  function extractProductPrices(sentence) {
    const found = [];
    const seen = new Set();
    const add = (m, bare = false) => {
      const index = m.index || 0;
      const phrase = m[0];
      if (isDiscountOrOtherAmount(sentence, index, phrase)) return;
      const before = sentence.slice(Math.max(0, index - 14), index);
      const after = sentence.slice(index + phrase.length, Math.min(sentence.length, index + phrase.length + 14));
      if (bare && !strongPriceCue.test(before + phrase + after)) return;
      const value = canonicalPriceAdvanced(phrase);
      if (!value) return;
      const key = `${index}:${phrase}`;
      if (seen.has(key)) return;
      seen.add(key);
      found.push({index, phrase, value});
    };
    explicitPricePattern.lastIndex = 0;
    for (const m of sentence.matchAll(explicitPricePattern)) add(m, false);
    currencyPattern.lastIndex = 0;
    for (const m of sentence.matchAll(currencyPattern)) add(m, false);
    bareDecimalPattern.lastIndex = 0;
    for (const m of sentence.matchAll(bareDecimalPattern)) add(m, true);
    return found;
  }

  window.ttsExtractProductPrices = extractProductPrices;
})();
