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
    const direct = raw.match(/\d+(?:\.\d{1,2})?/);
    if (direct && !/[块元]/.test(raw.slice(0, direct.index + direct[0].length))) return String(Number(direct[0]));
    const block = raw.match(/^([0-9零〇一二两三四五六七八九十百]+)(?:块钱?|块|元)(.*)$/);
    if (!block) return direct ? String(Number(direct[0])) : '';
    const integer = parseChineseInteger(block[1]);
    if (!Number.isFinite(integer)) return '';
    const tailDigits = [...block[2]].map(ch => (/\d/.test(ch) ? ch : (zhDigit[ch] ?? ''))).join('').slice(0, 2);
    return tailDigits ? `${integer}.${tailDigits}` : String(integer);
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

  function sentenceRangesAdvanced(text) {
    const out = [];
    const re = /[^。！？!?；;\n]+[。！？!?；;]?/g;
    let m;
    while ((m = re.exec(String(text || '')))) {
      const value = m[0].trim();
      if (value) out.push({text: value, start: m.index});
    }
    return out;
  }

  function workingText(p) {
    return p?.candidates?.length ? selectedText(p) : String(p?.original_text || '').trim();
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

  function renderEnhancedFactDrawer() {
    if (!$('factSummary') || !$('factResults')) return;
    const counts = state.factFindings.reduce((m, x) => (m[x.type] = (m[x.type] || 0) + 1, m), {});
    const types = ['价格', '包邮', '运费险', '包赔', '发货时间', '主播人设'];
    $('factSummary').innerHTML = types.filter(t => counts[t]).map(t => `<span class="fact-chip">${t} ${counts[t]}</span>`).join('') || '<span class="fact-chip">没有待处理冲突</span>';
    $('factResults').innerHTML = state.factFindings.length ? state.factFindings.map(x => `<div class="fact-item"><div class="fact-top"><span class="fact-type">${escapeHtml(x.type)}</span><b>${escapeHtml(x.paragraph_id)}</b></div><div class="fact-context">${highlightText(x.context, x.phrase)}</div><div class="fact-reason">${escapeHtml(x.reason)} 目标口径：${escapeHtml(x.expected)}</div><div class="fact-actions"><button class="btn ghost small" data-price-patch-locate="${x.paragraph_index}">定位修改</button></div></div>`).join('') : '<div class="risk-empty">当前没有商品事实冲突。</div>';
    if ($('factFixBtn')) $('factFixBtn').disabled = !state.factFindings.length;
    $('factResults').querySelectorAll('[data-price-patch-locate]').forEach(btn => btn.addEventListener('click', () => {
      const pi = Number(btn.dataset.pricePatchLocate);
      if (!state.paragraphs[pi]) return;
      state.riskFocusParagraph = pi;
      state.searchFocusParagraph = -1;
      state.paragraphs[pi].expanded = true;
      $('factDrawer')?.classList.remove('open');
      $('drawerBackdrop')?.classList.remove('open');
      switchView('prepare');
      render();
      requestAnimationFrame(() => document.getElementById(`para-${pi}`)?.scrollIntoView({behavior:'smooth', block:'center'}));
    }));
  }

  function supplementPriceFindings() {
    const configured = canonicalPriceAdvanced($('factPrice')?.value || '');
    if (!configured || !state.paragraphs?.length) return;
    const existing = new Set((state.factFindings || []).filter(x => x.type === '价格').map(x => `${x.paragraph_index}:${x.position}:${x.phrase}`));
    let added = 0;
    state.paragraphs.forEach((p, pi) => {
      for (const sentence of sentenceRangesAdvanced(workingText(p))) {
        for (const hit of extractProductPrices(sentence.text)) {
          if (hit.value === configured) continue;
          const position = sentence.start + hit.index;
          const key = `${pi}:${position}:${hit.phrase}`;
          if (existing.has(key)) continue;
          existing.add(key);
          state.factFindings.push({paragraph_id:p.id, paragraph_index:pi, position, type:'价格', phrase:hit.phrase, context:sentence.text, expected:`${configured}元`, reason:`当前配置到手价为 ${configured}元。`});
          added++;
        }
      }
    });
    if (!added) return;
    state.factFindings.sort((a,b) => a.paragraph_index - b.paragraph_index || a.position - b.position);
    renderEnhancedFactDrawer();
    showMessage(`发现 ${state.factFindings.length} 处商品事实疑似不一致，其中补充识别 ${added} 处价格话术。`, 'info');
  }

  const checkBtn = $('factCheckBtn');
  if (checkBtn) checkBtn.addEventListener('click', () => setTimeout(supplementPriceFindings, 0));
})();
