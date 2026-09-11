(() => {
  if (window.__ttsTextStudioFactsLoaded) return;
  window.__ttsTextStudioFactsLoaded = true;

  const FACT_META_KIND = 'product_fact_meta';
  const defaultFacts = () => ({
    price: '',
    free_shipping: 'unknown',
    shipping_insurance: 'unknown',
    compensation: 'unknown',
    shipping_time: '',
    host_persona: 'unknown',
  });

  state.productFacts = {...defaultFacts(), ...(state.productFacts || {})};
  state.factFindings = [];
  state.factChecked = false;

  const css = `
    .fact-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.fact-grid .field{margin-top:8px}.fact-grid .wide{grid-column:1/-1}
    .fact-summary{display:flex;gap:7px;flex-wrap:wrap;padding:12px 18px;border-bottom:1px solid var(--line)}
    .fact-chip{font-size:12px;padding:6px 9px;border-radius:999px;background:#f8fafc;border:1px solid var(--line)}
    .fact-drawer{position:fixed;top:0;right:0;height:100vh;width:min(500px,94vw);background:#fff;z-index:42;box-shadow:-24px 0 60px rgba(15,23,42,.18);transform:translateX(105%);transition:transform .2s ease;display:flex;flex-direction:column}
    .fact-drawer.open{transform:translateX(0)}.fact-list{padding:14px 18px 28px;overflow:auto;display:flex;flex-direction:column;gap:10px}
    .fact-item{padding:12px;border:1px solid #bfdbfe;border-radius:12px;background:#f8fbff}.fact-top{display:flex;gap:8px;align-items:center}.fact-type{font-size:11px;padding:3px 7px;border-radius:999px;background:#dbeafe;color:#1d4ed8}.fact-item b{font-size:12px;color:#1e3a8a}.fact-context{margin:8px 0;font-size:13px;line-height:1.65;color:#334155}.fact-reason{font-size:12px;color:#475569;line-height:1.5}.fact-actions{display:flex;gap:7px;margin-top:10px}
    @media(max-width:760px){.fact-grid{grid-template-columns:1fr}.fact-grid .wide{grid-column:auto}}
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const side = document.querySelector('.side');
  const firstSettings = side?.querySelector('.settings-details');
  if (side) {
    const details = document.createElement('details');
    details.className = 'settings-details';
    details.id = 'factSettings';
    details.open = true;
    details.innerHTML = `
      <summary>商品事实</summary>
      <div class="settings-body">
        <div class="fact-grid">
          <div class="field wide"><label for="factPrice">产品到手价格</label><input id="factPrice" placeholder="例如：9.9；留空表示不校验" /></div>
          <div class="field"><label for="factFreeShipping">是否包邮</label><select id="factFreeShipping"><option value="unknown">未设置</option><option value="yes">包邮</option><option value="no">不包邮</option></select></div>
          <div class="field"><label for="factShippingInsurance">运费险</label><select id="factShippingInsurance"><option value="unknown">未设置</option><option value="yes">有</option><option value="no">没有</option></select></div>
          <div class="field"><label for="factCompensation">包赔</label><select id="factCompensation"><option value="unknown">未设置</option><option value="yes">有</option><option value="no">没有</option></select></div>
          <div class="field"><label for="factPersona">主播人设 / 称谓</label><select id="factPersona"><option value="unknown">未设置</option><option value="neutral">中性</option><option value="female">女性</option><option value="male">男性</option></select></div>
          <div class="field wide"><label for="factShippingTime">发货时间口径</label><input id="factShippingTime" placeholder="例如：48小时内发货 / 以订单页面为准" /></div>
        </div>
        <div class="actions"><button class="btn ghost small" id="factCheckBtn" type="button">检查全篇一致性</button><button class="btn primary small" id="factFixBtn" type="button" disabled>批量修正</button></div>
        <div class="hint">以这里配置的事实为准。规则负责定位冲突；批量修正后，必要时用当前 Provider / Model 做自然口语润色。</div>
      </div>`;
    if (firstSettings) side.insertBefore(details, firstSettings); else side.appendChild(details);
  }

  const drawer = document.createElement('aside');
  drawer.className = 'fact-drawer';
  drawer.id = 'factDrawer';
  drawer.setAttribute('aria-label', '商品事实一致性结果');
  drawer.innerHTML = `<div class="drawer-head"><div><h3>商品事实一致性</h3><p>统一价格、包邮、运费险、包赔、发货时间和主播称谓。</p></div><button class="btn ghost small drawer-close" id="closeFact" type="button">关闭</button></div><div class="fact-summary" id="factSummary"></div><div class="fact-list" id="factResults"></div>`;
  document.body.appendChild(drawer);

  function readFactsFromForm() {
    state.productFacts = {
      price: $('factPrice')?.value.trim() || '',
      free_shipping: $('factFreeShipping')?.value || 'unknown',
      shipping_insurance: $('factShippingInsurance')?.value || 'unknown',
      compensation: $('factCompensation')?.value || 'unknown',
      shipping_time: $('factShippingTime')?.value.trim() || '',
      host_persona: $('factPersona')?.value || 'unknown',
    };
    return state.productFacts;
  }

  function writeFactsToForm(facts) {
    const f = {...defaultFacts(), ...(facts || {})};
    state.productFacts = f;
    if ($('factPrice')) $('factPrice').value = f.price || '';
    if ($('factFreeShipping')) $('factFreeShipping').value = f.free_shipping || 'unknown';
    if ($('factShippingInsurance')) $('factShippingInsurance').value = f.shipping_insurance || 'unknown';
    if ($('factCompensation')) $('factCompensation').value = f.compensation || 'unknown';
    if ($('factShippingTime')) $('factShippingTime').value = f.shipping_time || '';
    if ($('factPersona')) $('factPersona').value = f.host_persona || 'unknown';
  }

  function syncFactMeta() {
    const factMeta = {kind: FACT_META_KIND, version: 1, product_facts: readFactsFromForm(), fact_checked: !!state.factChecked};
    state.replacementHistory = (state.replacementHistory || []).filter(x => x?.kind !== FACT_META_KIND);
    state.replacementHistory.unshift(factMeta);
  }

  function restoreFactMeta() {
    const meta = (state.replacementHistory || []).find(x => x?.kind === FACT_META_KIND);
    writeFactsToForm(meta?.product_facts || defaultFacts());
    state.factChecked = !!meta?.fact_checked;
    state.factFindings = [];
    renderFactDrawer();
  }

  const baseProjectPayload = projectPayload;
  projectPayload = function extendedProjectPayload() {
    syncFactMeta();
    return baseProjectPayload();
  };

  const baseLoadProject = loadProject;
  loadProject = async function extendedLoadProject(projectId) {
    const result = await baseLoadProject(projectId);
    restoreFactMeta();
    return result;
  };

  const baseNewProject = newProject;
  newProject = async function extendedNewProject(saveCurrent = true) {
    const result = await baseNewProject(saveCurrent);
    writeFactsToForm(defaultFacts());
    state.factFindings = [];
    state.factChecked = false;
    renderFactDrawer();
    return result;
  };

  function factWorkingText(p) {
    return p.candidates?.length ? selectedText(p) : String(p.original_text || '').trim();
  }

  function sentenceRanges(text) {
    const out = [];
    const re = /[^。！？!?；;\n]+[。！？!?；;]?/g;
    let m;
    while ((m = re.exec(String(text || '')))) {
      const value = m[0].trim();
      if (value) out.push({text: value, start: m.index});
    }
    return out;
  }

  const zhDigits = {'零':'0','〇':'0','一':'1','二':'2','两':'2','三':'3','四':'4','五':'5','六':'6','七':'7','八':'8','九':'9'};
  function chineseInteger(s) {
    if (!s) return 0;
    if (s.includes('十')) {
      const [a, b] = s.split('十');
      const tens = a ? Number(zhDigits[a] || 0) : 1;
      const ones = b ? Number(zhDigits[b] || 0) : 0;
      return tens * 10 + ones;
    }
    return Number([...s].map(ch => zhDigits[ch] || '').join('') || 0);
  }

  function canonicalPrice(value) {
    const v = String(value || '').trim().replace(/[￥¥\s]/g, '');
    const arabicBlock = v.match(/(\d+)(?:块钱?|元)(\d)?/);
    if (arabicBlock) return arabicBlock[2] ? `${Number(arabicBlock[1])}.${arabicBlock[2]}` : String(Number(arabicBlock[1]));
    const direct = v.match(/\d+(?:\.\d+)?/);
    if (direct) return String(Number(direct[0]));
    const block = v.match(/([零〇一二两三四五六七八九十]+)块(?:钱)?([零〇一二两三四五六七八九])?/);
    if (block) {
      const integer = chineseInteger(block[1]);
      const decimal = block[2] ? zhDigits[block[2]] : '';
      return decimal ? `${integer}.${decimal}` : String(integer);
    }
    return '';
  }

  const pricePattern = /(?:[￥¥]\s*)?(?:\d+(?:\.\d+)?\s*元|\d+(?:块钱?|元)\d?|[零〇一二两三四五六七八九十]+块(?:钱)?[零〇一二两三四五六七八九]?)/g;
  const insuranceWord = '(?:运费险|晕飞险|运飞险|云飞险|孕飞险)';
  const patterns = {
    free_yes: /全国包邮|包邮|免邮|邮费(?:我们|商家|我)?(?:出|承担)/g,
    free_no: /不包邮|不含邮费|运费自理|邮费自理|需要[^，。！？]{0,8}(?:运费|邮费)/g,
    insurance_yes: new RegExp(`(?:有|带|送|赠送|买|购买|安排)[^，。！？]{0,8}${insuranceWord}|${insuranceWord}[^，。！？]{0,8}(?:有|送|赠送|买|购买|安排)`, 'g'),
    insurance_no: new RegExp(`没有${insuranceWord}|无${insuranceWord}|不含${insuranceWord}|不送${insuranceWord}|不买${insuranceWord}`, 'g'),
    comp_yes: /包赔|坏果[^，。！？]{0,10}(?:赔|赔付|赔偿)|烂果[^，。！？]{0,10}(?:赔|赔付|赔偿)|磕碰[^，。！？]{0,10}(?:赔|赔付|赔偿)|有问题[^，。！？]{0,10}直接赔|直接给你赔|直接赔付|直接赔偿/g,
    comp_no: /不包赔|没有包赔|不赔付|不赔偿/g,
    shipping: /当天发货|今天发货|明天发货|马上发货|立即发货|次日发|(?:24|48|72|\d+)小时内发货|\d+天内发货|\d+天左右(?:到|送达)|当天(?:发走|发出)/g,
    female_self: /妹妹我|姐姐我|姐跟你说|姐姐给你|姐给你|妹跟你说/g,
    male_self: /哥哥我|哥跟你说|哥哥给你|哥给你|兄弟我|老哥我/g,
  };

  function pushFinding(out, pi, p, sentence, type, phrase, expected, reason, position) {
    out.push({paragraph_id: p.id, paragraph_index: pi, position: sentence.start + (position || 0), type, phrase, context: sentence.text, expected, reason});
  }

  function detectFactConflicts(silent = false) {
    if (!state.paragraphs.length) {
      showMessage('请先解析原稿，再检查商品事实。', 'err');
      return;
    }
    const f = readFactsFromForm();
    const wantedPrice = canonicalPrice(f.price);
    const out = [];
    state.paragraphs.forEach((p, pi) => {
      const versions = [{candidate_index: -1, text: p.original_text || ''},
        ...(p.candidates || []).map((text, candidate_index) => ({candidate_index, text: candidate_index === (p.selectedIndex || 0) ? factWorkingText(p) : text}))];
      for (const version of versions) {
      const begin = out.length;
      const text = version.text;
      for (const sentence of sentenceRanges(text)) {
        if (wantedPrice) {
          const hits = window.ttsExtractProductPrices ? window.ttsExtractProductPrices(sentence.text) : [];
          for (const hit of hits) {
            if (Number(hit.value) !== Number(wantedPrice)) pushFinding(out, pi, p, sentence, '价格', hit.phrase, `${wantedPrice}元`, `当前配置到手价为 ${wantedPrice}元。`, hit.index);
          }
        }
        if (f.free_shipping !== 'unknown') {
          const bad = f.free_shipping === 'yes' ? patterns.free_no : patterns.free_yes;
          bad.lastIndex = 0;
          for (const m of sentence.text.matchAll(bad)) pushFinding(out, pi, p, sentence, '包邮', m[0], f.free_shipping === 'yes' ? '包邮' : '不承诺包邮', `当前商品配置为${f.free_shipping === 'yes' ? '包邮' : '不包邮'}。`, m.index);
        }
        if (f.shipping_insurance !== 'unknown' && !/以[^，。！？]{0,12}为准|页面(?:显示|为准)|订单页面/.test(sentence.text)) {
          const bad = f.shipping_insurance === 'yes' ? patterns.insurance_no : patterns.insurance_yes;
          bad.lastIndex = 0;
          for (const m of sentence.text.matchAll(bad)) pushFinding(out, pi, p, sentence, '运费险', m[0], f.shipping_insurance === 'yes' ? '有运费险' : '没有运费险', `当前商品配置为${f.shipping_insurance === 'yes' ? '有' : '没有'}运费险。`, m.index);
        }
        if (f.compensation !== 'unknown') {
          const bad = f.compensation === 'yes' ? patterns.comp_no : patterns.comp_yes;
          bad.lastIndex = 0;
          for (const m of sentence.text.matchAll(bad)) pushFinding(out, pi, p, sentence, '包赔', m[0], f.compensation === 'yes' ? '有包赔' : '没有包赔', `当前商品配置为${f.compensation === 'yes' ? '有' : '没有'}包赔。`, m.index);
        }
        if (f.shipping_time) {
          patterns.shipping.lastIndex = 0;
          for (const m of sentence.text.matchAll(patterns.shipping)) {
            if (!sentence.text.includes(f.shipping_time)) pushFinding(out, pi, p, sentence, '发货时间', m[0], f.shipping_time, `当前发货口径为“${f.shipping_time}”。`, m.index);
          }
        }
        let personaBad = null;
        if (f.host_persona === 'female') personaBad = patterns.male_self;
        else if (f.host_persona === 'male') personaBad = patterns.female_self;
        else if (f.host_persona === 'neutral') personaBad = /妹妹我|姐姐我|姐跟你说|姐姐给你|姐给你|妹跟你说|哥哥我|哥跟你说|哥哥给你|哥给你|兄弟我|老哥我/g;
        if (personaBad) {
          personaBad.lastIndex = 0;
          for (const m of sentence.text.matchAll(personaBad)) pushFinding(out, pi, p, sentence, '主播人设', m[0], f.host_persona === 'female' ? '女性自称' : f.host_persona === 'male' ? '男性自称' : '中性自称', `当前主播人设为${f.host_persona === 'female' ? '女性' : f.host_persona === 'male' ? '男性' : '中性'}。`, m.index);
        }
      }
      out.slice(begin).forEach(finding => finding.candidate_index = version.candidate_index);
      }
    });
    state.factFindings = out;
    state.factChecked = true;
    if (silent) return out;
    syncFactMeta();
    renderFactDrawer();
    scheduleSave(0);
    openFact();
    showMessage(out.length ? `发现 ${out.length} 处商品事实疑似不一致。` : '商品事实一致性检查通过。', out.length ? 'info' : 'ok');
  }

  function replacementFor(finding, facts) {
    if (finding.type === '价格') return `${canonicalPrice(facts.price)}元`;
    if (finding.type === '包邮') return facts.free_shipping === 'yes' ? '当前商品支持包邮' : '运费信息以订单页面实际显示为准';
    if (finding.type === '运费险') return facts.shipping_insurance === 'yes' ? '当前订单支持运费险' : '具体售后保障以订单页面实际显示为准';
    if (finding.type === '包赔') return facts.compensation === 'yes' ? '符合规则的售后问题按包赔政策处理' : '如有售后问题，请联系客服按平台规则处理';
    if (finding.type === '发货时间') return facts.shipping_time || '发货时间以订单页面为准';
    if (finding.type === '主播人设') {
      if (facts.host_persona === 'neutral') return '我';
      if (facts.host_persona === 'female') return finding.phrase.replace(/哥哥我|兄弟我|老哥我/g, '妹妹我').replace(/哥跟你说/g, '妹跟你说').replace(/哥哥给你|哥给你/g, '妹妹给你');
      return finding.phrase.replace(/妹妹我|姐姐我/g, '我').replace(/姐跟你说|妹跟你说/g, '哥跟你说').replace(/姐姐给你|姐给你/g, '哥给你');
    }
    return finding.phrase;
  }

  function setFinalText(pi, text) {
    const p = state.paragraphs[pi];
    if (!p) return;
    if (p.candidates?.length) {
      const ci = p.selectedIndex || 0;
      p.candidates[ci] = text;
      p.editedText = text;
    } else {
      p.original_text = text;
      p.sentences = sentenceRanges(text).map(x => x.text);
    }
  }

  function updateSourceIfUngenerated() {
    if (state.paragraphs.some(p => p.candidates?.length)) return;
    $('sourceText').value = state.paragraphs.map(p => p.original_text || '').join('\n\n');
  }

  function targetText(pi, ci) {
    const p = state.paragraphs[pi];
    return ci < 0 ? p.original_text : ci === (p.selectedIndex || 0) ? factWorkingText(p) : p.candidates[ci];
  }

  function writeTarget(pi, ci, text) {
    const p = state.paragraphs[pi];
    if (ci < 0) { p.original_text = text; p.sentences = sentenceRanges(text).map(x => x.text); }
    else { p.candidates[ci] = text; if (ci === (p.selectedIndex || 0)) p.editedText = text; }
  }

  async function polishParagraphs(targets) {
    const provider = $('provider').value;
    if (!targets.length || !state.providerStatus[provider]) return false;
    const snapshot = state.paragraphs;
    const paragraphs = targets.map(([pi, ci], i) => ({id: `fix${i}`, original_text: targetText(pi, ci)}));
    const data = await api('/api/generalize', {project_id: state.projectId, paragraph_indexes: targets.map(t => t[0]), provider, model: state.selectedModel, candidate_count: 1,
      instruction: `商品事实：${JSON.stringify(readFactsFromForm())}。只做必要的自然口语润色，不得恢复旧事实或新增事实。`, paragraphs});
    if (state.paragraphs !== snapshot) return false;
    targets.forEach(([pi, ci], i) => {
      const candidate = data.paragraphs.find(p => p.id === `fix${i}`)?.candidates?.[0];
      if (candidate && targetText(pi, ci) === paragraphs[i].original_text) {
        writeTarget(pi, ci, candidate);
        if (detectFactConflicts(true).some(f => f.paragraph_index === pi && f.candidate_index === ci))
          writeTarget(pi, ci, paragraphs[i].original_text);
      }
    });
    return true;
  }

  async function fixFactConflicts() {
    detectFactConflicts(true);
    if (!state.factFindings.length) return;
    const facts = readFactsFromForm();
    const byParagraph = new Map();
    const complex = new Set();
    for (const finding of state.factFindings) {
      const key = `${finding.paragraph_index}:${finding.candidate_index}`;
      if (!byParagraph.has(key)) byParagraph.set(key, []);
      byParagraph.get(key).push(finding);
      if (!['价格', '主播人设'].includes(finding.type)) complex.add(key);
    }
    for (const [key, findings] of byParagraph) {
      const [pi, ci] = key.split(':').map(Number);
      let text = targetText(pi, ci);
      const contexts = new Map();
      for (const f of findings) {
        if (!contexts.has(f.context)) contexts.set(f.context, []);
        contexts.get(f.context).push(f);
      }
      for (const [context, group] of contexts) {
        if (!text.includes(context)) continue;
        let fixed = context;
        for (const finding of group) {
          const replacement = replacementFor(finding, facts);
          if (finding.type === '价格' || finding.type === '主播人设') {
            fixed = fixed.replace(finding.phrase, replacement);
          } else {
            const parts = fixed.split(/([，；])/);
            let changed = false;
            fixed = parts.map(part => {
              if (!changed && part.includes(finding.phrase)) { changed = true; return replacement + (part.match(/[。！？!?][”’」』）)]*$/)?.[0] || ''); }
              return part;
            }).join('').replace(/(?:，|；){2,}/g, '，');
          }
        }
        text = text.replace(context, fixed);
      }
      writeTarget(pi, ci, text);
    }
    updateSourceIfUngenerated();
    invalidateReview();
    state.factFindings = [];
    state.factChecked = false;
    renderFactDrawer();
    render();
    showMessage('商品事实已做确定性修正，正在处理必要的自然口语润色…', 'info');
    scheduleSave(0);
    let polished = false;
    let polishError = '';
    try {
      const indexes = [...complex].map(key => key.split(':').map(Number));
      polished = await polishParagraphs(indexes);
    } catch (e) {
      polishError = e.message;
    }
    detectFactConflicts();
    closeFact();
    scheduleSave(0);
    showMessage(polishError ? `事实已修正；模型润色未完成：${polishError}` : state.factFindings.length ? `已修正，仍有 ${state.factFindings.length} 处需要人工确认。` : polished ? '商品事实已批量修正，并用当前模型完成必要润色。' : '商品事实已批量修正。', polishError || state.factFindings.length ? 'info' : 'ok');
  }

  function counts() {
    return state.factFindings.reduce((m, x) => (m[x.type] = (m[x.type] || 0) + 1, m), {});
  }

  function renderFactDrawer() {
    if (!$('factSummary') || !$('factResults')) return;
    const c = counts();
    const types = ['价格', '包邮', '运费险', '包赔', '发货时间', '主播人设'];
    $('factSummary').innerHTML = types.filter(t => c[t]).map(t => `<span class="fact-chip">${t} ${c[t]}</span>`).join('') || '<span class="fact-chip">没有待处理冲突</span>';
    $('factResults').innerHTML = state.factFindings.length ? state.factFindings.map(x => `<div class="fact-item"><div class="fact-top"><span class="fact-type">${escapeHtml(x.type)}</span><b>${escapeHtml(x.paragraph_id)} · ${x.candidate_index < 0 ? '原稿' : '候选 ' + (x.candidate_index + 1)}</b></div><div class="fact-context">${highlightText(x.context, x.phrase)}</div><div class="fact-reason">${escapeHtml(x.reason)} 目标口径：${escapeHtml(x.expected)}</div><div class="fact-actions"><button class="btn ghost small" data-fact-locate="${x.paragraph_index}" data-candidate="${x.candidate_index}">定位修改</button></div></div>`).join('') : '<div class="risk-empty">当前没有商品事实冲突。</div>';
    $('factFixBtn').disabled = !state.factFindings.length;
    $('factResults').querySelectorAll('[data-fact-locate]').forEach(btn => btn.addEventListener('click', () => locateFact(Number(btn.dataset.factLocate), Number(btn.dataset.candidate))));
  }

  function locateFact(pi, ci) {
    if (ci >= 0) selectCandidate(pi, ci);
    if (!state.paragraphs[pi]) return;
    state.riskFocusParagraph = pi;
    state.searchFocusParagraph = -1;
    state.paragraphs[pi].expanded = true;
    closeFact();
    switchView('prepare');
    render();
    requestAnimationFrame(() => document.getElementById(`para-${pi}`)?.scrollIntoView({behavior: 'smooth', block: 'center'}));
  }

  function openFact() {
    $('drawerBackdrop')?.classList.add('open');
    $('riskDrawer')?.classList.remove('open');
    $('factDrawer')?.classList.add('open');
  }

  function closeFact() {
    $('factDrawer')?.classList.remove('open');
    if (!$('riskDrawer')?.classList.contains('open')) $('drawerBackdrop')?.classList.remove('open');
  }

  $('factCheckBtn')?.addEventListener('click', () => detectFactConflicts());
  $('factFixBtn')?.addEventListener('click', fixFactConflicts);
  $('closeFact')?.addEventListener('click', closeFact);
  $('drawerBackdrop')?.addEventListener('click', closeFact);
  ['factPrice', 'factFreeShipping', 'factShippingInsurance', 'factCompensation', 'factShippingTime', 'factPersona'].forEach(id => $(id)?.addEventListener('input', () => {
    readFactsFromForm();
    state.factChecked = false;
    state.factFindings = [];
    renderFactDrawer();
    scheduleSave(500);
  }));

  restoreFactMeta();
})();
