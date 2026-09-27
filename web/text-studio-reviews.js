/* Exact-sentence human decisions shared by editor, previews and both Live modes. */
const TextStudioReviews = (() => {
  const codepoints = text => [...text].length;
  function value(p, ci) {
    return ci === (p.selectedIndex || 0) ? selectedText(p) : String(p.candidates?.[ci] || '').trim();
  }
  function reviews(p, ci, text = value(p, ci)) {
    return (p.prohibited_reviews || []).filter(r => r.candidate_index === ci && r.candidate_text === text);
  }
  function entries(p, ci) {
    const group = TextStudioVariants.groupFor(p.id);
    const paragraphs = group ? group.paragraph_ids.map(id => state.paragraphs.find(p => p.id === id)) : [p];
    let text = '';
    const spans = paragraphs.map(p => {
      const source = value(p, ci);
      if (/[A-Za-z0-9]$/.test(text) && /^[A-Za-z0-9]/.test(source)) text += ' ';
      const start = text.length;
      text += source;
      return {p, ci, source, start, end: text.length};
    });
    const hits = TextStudioProhibited.analyze(text).blocked.map(hit => {
      const affected = spans.filter(s => s.start < hit.end && s.end > hit.start);
      const targets = affected.map(s => ({paragraph_id: s.p.id, candidate_index: ci,
        candidate_text: s.source, start: codepoints(text.slice(0, hit.start)) - codepoints(text.slice(0, s.start)),
        text: hit.text, labels: hit.labels, rules_version: TextStudioProhibited.rulesVersion}));
      const approved = affected.length > 0 && affected.every((s, i) =>
        TextStudioProhibited.confirmed(s.source, hit, reviews(s.p, ci, s.source), targets[i].start));
      return {...hit, approved, targets};
    });
    return {text, spans, hits};
  }
  function analysis(p, ci = p.selectedIndex || 0) {
    const scope = entries(p, ci), span = scope.spans.find(s => s.p === p);
    const hits = scope.hits.filter(h => h.start < span.end && h.end > span.start);
    let cursor = span.start, text = '';
    for (const hit of hits.filter(h => !h.approved)) {
      text += scope.text.slice(cursor, Math.max(span.start, hit.start));
      cursor = Math.min(span.end, hit.end);
    }
    text += scope.text.slice(cursor, span.end);
    return {text: /[\p{L}\p{N}]/u.test(text) ? text.trim() : '', hits,
      blocked: hits.filter(h => !h.approved), approved: hits.filter(h => h.approved)};
  }
  function findings(selectedParagraph = null) {
    const out = [];
    const scopes = selectedParagraph ? [[selectedParagraph, selectedParagraph.selectedIndex || 0]] : state.contextGroups ? state.contextGroups.flatMap(g => g.variants.map(v =>
      [state.paragraphs.find(p => p.id === g.paragraph_ids[0]), v.candidate_index])) :
      state.paragraphs.map(p => [p, p.selectedIndex || 0]);
    for (const [p, ci] of scopes) {
      const scope = entries(p, ci);
      for (const hit of scope.hits) for (const span of scope.spans) {
        if (selectedParagraph && span.p !== selectedParagraph) continue;
        const start = Math.max(span.start, hit.start), end = Math.min(span.end, hit.end);
        if (start >= end) continue;
        out.push({paragraph_id: span.p.id, paragraph_index: state.paragraphs.indexOf(span.p), candidate_index: ci,
          position: start - span.start, end: end - span.start, phrase: scope.text.slice(start, end), context: hit.text,
          type: '禁止播报', label: '规则命中', severity: hit.approved ? 'approved' : 'blocked',
          prohibited: true, blocked: !hit.approved, approved: hit.approved, ignored: false,
          reason: hit.labels.join('、'), review_targets: hit.targets,
          suggestion: hit.approved ? '人工已确认可播报；规则命中记录保留，可恢复屏蔽。' : '已整句屏蔽。可定位修改，或根据商品实际权益确认本句可播报。'});
      }
    }
    return out;
  }
  function prune() {
    for (const p of state.paragraphs) if (p.prohibited_reviews) {
      p.prohibited_reviews = p.prohibited_reviews.filter(r => r.candidate_text === value(p, r.candidate_index));
    }
  }
  async function decide(id, position, ci, allow) {
    const hit = findings().find(f => f.paragraph_id === id && f.position === position && f.candidate_index === ci);
    if (!hit) { showMessage('话术已变化，请重新检查后处理。', 'info'); return; }
    for (const target of hit.review_targets) {
      const p = state.paragraphs.find(p => p.id === target.paragraph_id);
      p.prohibited_reviews = (p.prohibited_reviews || []).filter(r => !(r.candidate_index === target.candidate_index &&
        r.candidate_text === target.candidate_text && r.start === target.start && r.text === target.text));
      if (allow) {
        const {paragraph_id, ...record} = target;
        p.prohibited_reviews.push({...record, confirmed_at: new Date().toISOString()});
      }
    }
    state.replacementHistory.push({scope: 'prohibited_confirmation', decision: allow ? 'allow' : 'revoke',
      paragraph_id: id, candidate_index: ci, text: hit.context, time: new Date().toISOString()});
    render(); renderRiskDrawer();
    try {
      await queueSave(true);
      showMessage(allow ? '本句已人工确认可播报，规则提示已保留。正在运行的直播需停止后重新启动才使用新审核结果。' :
        '本句已恢复屏蔽；已生成的音频不受影响。', 'ok');
    } catch (e) { showMessage('处理已更新，但保存失败：' + e.message, 'err'); }
  }
  function controls(f) {
    return `<button class="btn ghost small" onclick="TextStudioReviews.decide('${escapeHtml(f.paragraph_id)}',${f.position},${f.candidate_index},${!f.approved})">${f.approved ? '恢复屏蔽' : '确认可播报'}</button>`;
  }
  function sentenceCards(p) {
    return findings(p).map(f =>
      `<div class="prohibited-sentence ${f.approved ? 'approved-sentence' : ''}"><b>${f.approved ? '规则命中 · 人工已确认可播报' : '禁止播报 · 已屏蔽整句'}</b><div>${escapeHtml(f.context)}</div><div>${escapeHtml(f.reason)}</div><button class="btn ghost small" onclick="locateRiskById('${escapeHtml(p.id)}',${f.position},${f.candidate_index})">定位修改</button> ${controls(f)}</div>`).join('');
  }
  function transportReviews(p, ci) {
    return reviews(p, ci).map(r => ({...r, candidate_index: 0}));
  }
  function previewPayload(p) {
    const ci = p.selectedIndex || 0;
    const result = {text: value(p, ci), voice: currentVoice(), prohibited_reviews: transportReviews(p, ci)};
    if (TextStudioVariants.groupFor(p.id)) {
      result.paragraph_id = p.id;
      result.context_segments = entries(p, ci).spans.map(s => ({id: s.p.id, text: s.source,
        prohibited_reviews: transportReviews(s.p, ci)}));
    }
    return result;
  }
  function liveSegments() {
    if (state.contextGroups) return state.paragraphs.map(p => ({id: p.id, original_text: p.original_text,
      candidates: p.candidates.slice(), prohibited_reviews: p.prohibited_reviews || []}));
    return state.paragraphs.filter(p => (p.candidates || []).some((_, ci) => analysis(p, ci).text))
      .map(p => ({id: p.id, candidates: p.candidates.map((_, ci) => value(p, ci)),
        prohibited_reviews: p.prohibited_reviews || []}));
  }
  function exportParagraph(p) {
    const result = analysis(p), text = result.text;
    // The filtered export has new offsets; carry only confirmations of surviving exact sentences.
    const confirmed = TextStudioProhibited.analyze(text).blocked.flatMap(hit => {
      const original = result.approved.find(h => h.text === hit.text && JSON.stringify(h.labels) === JSON.stringify(hit.labels));
      if (!original) return [];
      const review = reviews(p, p.selectedIndex || 0).find(r => r.text === original.text && r.start === original.targets.find(t => t.paragraph_id === p.id)?.start);
      return review ? [{...review, candidate_index: 0, candidate_text: text, start: codepoints(text.slice(0, hit.start))}] : [];
    });
    return {id:p.id, original_text:p.original_text, candidates:[text], selected_index:0, selected_text:text,
      ...(confirmed.length ? {prohibited_reviews:confirmed} : {})};
  }
  return {analysis, findings, prune, decide, controls, sentenceCards, previewPayload, liveSegments, exportParagraph};
})();
