/* Group state, generation and serialization; paragraph candidates remain the text source. */
const TextStudioVariants = (() => {
  let pending = null;
  const active = () => Array.isArray(state.contextGroups);
  const groupFor = id => state.contextGroups?.find(g => g.paragraph_ids.includes(id));
  const projectFields = () => active() ? {context_groups: state.contextGroups} : {};
  const members = group => group.paragraph_ids.map(id => state.paragraphs.find(p => p.id === id));
  const snapshot = group => JSON.stringify(members(group).map(p => [p.id, p.original_text, p.candidates]));
  const complete = () => !active() || state.contextGroups.every(g => g.variants?.length && members(g).every(p => p?.candidates?.length === g.variants.length));

  function select(i, ci) {
    const group = groupFor(state.paragraphs[i].id);
    if (!group) return false;
    const variant = group.variants[ci];
    if (!variant) return true;
    group.selected_variant_id = variant.id;
    for (const p of members(group)) {
      p.selectedIndex = ci;
      p.editedText = p.candidates[ci];
    }
    return true;
  }

  function restore() {
    pending = null;
    for (const group of state.contextGroups || []) {
      if (!group.variants?.length) continue;
      const variant = group.variants.find(v => v.id === group.selected_variant_id);
      if (!variant) throw new Error(`上下文组 ${group.id} 缺少选中的版本`);
      const i = state.paragraphs.findIndex(p => p.id === group.paragraph_ids[0]);
      select(i, variant.candidate_index);
    }
  }

  async function createCopy() {
    if (state.generalizing || !state.paragraphs.length) return;
    state.generalizing = true;
    render();
    try {
      const original = state.paragraphs;
      const originalId = state.projectId;
      const {context_groups} = await api('/api/context/groups', {paragraphs: original});
      if (state.paragraphs !== original || state.projectId !== originalId) return;
      await queueSave(true);
      if (state.paragraphs !== original) return;
      clearTimeout(state.saveTimer);
      state.projectId = null;
      state.projectKind = 'draft';
      state.projectName = (state.projectName || defaultProjectName()) + '-整组试验';
      $('projectName').value = state.projectName;
      state.paragraphs = original.map(p => ({...p, candidates: [], selectedIndex: 0, editedText: ''}));
      state.contextGroups = context_groups;
      pending = null;
      invalidateReview();
      render();
      await queueSave(true);
      showMessage(`已复制为 ${context_groups.length} 个上下文组。原项目保留；点击泛化即可逐组生成完整版本。`, 'ok');
    } catch (e) { showMessage('创建整组试验失败：' + e.message, 'err'); }
    finally { state.generalizing = false; render(); }
  }

  async function generate(group) {
    const paragraphs = members(group);
    const project = state.paragraphs, before = snapshot(group);
    const start = project.indexOf(paragraphs[0]), end = start + paragraphs.length;
    const request = {
      paragraphs: paragraphs.map(p => ({id: p.id, original_text: p.original_text})),
      group_id: group.id, revision: group.revision, candidate_count: Number($('candidateCount').value),
      provider: $('provider').value, model: state.selectedModel, instruction: $('instruction').value,
      before: project[start - 1]?.original_text || '', after: project[end]?.original_text || '',
    };
    const result = await api('/api/context/generalize', request);
    if (state.paragraphs !== project || snapshot(group) !== before)
      throw new Error('生成期间项目或组内文本发生变化；已保留当前编辑，请重新生成。');
    return {result, group, project, before};
  }

  function apply(data) {
    if (state.paragraphs !== data.project || snapshot(data.group) !== data.before)
      throw new Error('当前内容已变化，不能应用过期结果。');
    const byId = Object.fromEntries(data.result.paragraphs.map(p => [p.id, p]));
    for (const p of members(data.group)) {
      p.candidates = byId[p.id].candidates;
      p.selectedIndex = 0;
      p.editedText = p.candidates[0];
    }
    Object.assign(data.group, data.result.group);
    invalidateReview();
    refreshSearchMatches(false);
  }

  async function generateAll() {
    if (state.generalizing) return;
    state.generalizing = true;
    render();
    try {
      const groups = state.contextGroups;
      for (const group of groups.filter(g => !g.variants?.length)) {
        if (state.contextGroups !== groups) break;
        showMessage(`正在生成上下文组 ${group.id}，每个版本覆盖 ${group.paragraph_ids.length} 个编辑单元…`, 'info');
        apply(await generate(group));
        render();
        await queueSave(true);
      }
      showMessage('整组泛化完成。请检查不同版本的连贯性与风险，再开始智播。', 'ok');
    } catch (e) { showMessage('整组泛化失败：' + e.message + ' 已完成组已保存，可继续。', 'err'); }
    finally { state.generalizing = false; render(); }
  }

  async function regenerate(i) {
    if (state.generalizing) return;
    const group = groupFor(state.paragraphs[i].id);
    state.generalizing = true;
    render();
    try {
      const data = await generate(group);
      if (!group.variants.length) {
        apply(data);
        await queueSave(true);
      } else {
        pending = data;
        showMessage(`${group.id} 的新版本已生成，请在下方预览后应用；当前人工编辑仍保留。`, 'info');
      }
    } catch (e) { showMessage('整组重生成失败：' + e.message, 'err'); }
    finally { state.generalizing = false; render(); }
  }

  async function applyPending() {
    try {
      if (!pending) return;
      apply(pending);
      pending = null;
      render();
      await queueSave(true);
      showMessage('已应用完整新版本。', 'ok');
    } catch (e) { showMessage(e.message, 'err'); }
  }

  function scanRisks() {
    return state.paragraphs.flatMap((p, pi) => (p.candidates || []).flatMap((text, ci) =>
      TextStudioRisk.scan(text).map(f => ({...f, paragraph_id: p.id, paragraph_index: pi,
        candidate_index: ci, variant_id: groupFor(p.id)?.variants[ci]?.id}))));
  }

  async function polishTargets(targets, instruction, readTarget, writeTarget, conflicts) {
    const work = new Map();
    for (const [pi, ci] of targets) {
      const group = groupFor(state.paragraphs[pi].id);
      if (!group) continue;
      const key = `${group.id}:${ci}`;
      if (!work.has(key)) work.set(key, {group, ci, targets: []});
      work.get(key).targets.push(pi);
    }
    let applied = false;
    for (const {group, ci, targets: indexes} of work.values()) {
      const project = state.paragraphs;
      const indexesInGroup = group.paragraph_ids.map(id => project.findIndex(p => p.id === id));
      const paragraphs = indexesInGroup.map(pi => ({id: project[pi].id, original_text: readTarget(pi, ci)}));
      const result = await api('/api/context/polish', {group_id: group.id, revision: group.revision,
        provider: $('provider').value, model: state.selectedModel, instruction, paragraphs,
        target_ids: indexes.map(pi => project[pi].id)});
      if (state.paragraphs !== project || paragraphs.some((p, i) => readTarget(indexesInGroup[i], ci) !== p.original_text))
        throw new Error('润色期间内容发生变化，保留当前编辑。');
      const originals = indexes.map(pi => readTarget(pi, ci));
      for (const pi of indexes) writeTarget(pi, ci, result.paragraphs.find(p => p.id === project[pi].id).candidates[0]);
      if (conflicts().some(f => indexes.includes(f.paragraph_index) && f.candidate_index === ci)) {
        indexes.forEach((pi, i) => writeTarget(pi, ci, originals[i]));
      } else applied = true;
    }
    return applied;
  }

  function blockedFindings() {
    const findings = [];
    for (const group of state.contextGroups || []) {
      for (const variant of group.variants) {
        const ci = variant.candidate_index, spans = [];
        let text = '';
        for (const p of members(group)) {
          const value = (p.candidates[ci] || '').trim();
          if (/[A-Za-z0-9]$/.test(text) && /^[A-Za-z0-9]/.test(value)) text += ' ';
          const start = text.length;
          text += value;
          spans.push({p, value, start, end: text.length});
        }
        for (const hit of TextStudioProhibited.analyze(text).blocked) {
          for (const span of spans) {
            const start = Math.max(span.start, hit.start), end = Math.min(span.end, hit.end);
            if (start >= end) continue;
            findings.push({paragraph_id: span.p.id, paragraph_index: state.paragraphs.indexOf(span.p),
              candidate_index: ci, variant_id: variant.id, position: start - span.start, end: end - span.start,
              phrase: text.slice(start, end), context: hit.text, type: '禁止播报', label: '禁止播报',
              severity: 'blocked', blocked: true, ignored: false, reason: hit.labels.join('、'),
              suggestion: '整句不会播报；若整组版本丢失完整单元，需修改后才能开始智播。'});
          }
        }
      }
    }
    return findings;
  }

  function renderGroups() {
    const button = $('contextCopyBtn'), info = $('contextInfo');
    if (!button || !info) return;
    button.disabled = state.generalizing || !state.paragraphs.length;
    const preview = $('contextPending');
    if (!active()) { preview.hidden = true; info.textContent = '整组模式先复制项目，原稿、旧候选和人工编辑仍留在原项目。'; return; }
    info.textContent = `整组试验：${state.contextGroups.filter(g => g.variants.length).length}/${state.contextGroups.length} 组完成。每轮按组选择完整版本；点击候选会切换整组。`;
    for (const [i, p] of state.paragraphs.entries()) {
      const group = groupFor(p.id), card = document.getElementById(`para-${i}`);
      if (!group || !card) continue;
      const meta = card.querySelector('.para-meta');
      if (meta) meta.textContent += ` · ${group.id} · 版本 ${(p.selectedIndex || 0) + 1}`;
      const regenerateButton = card.querySelector('button[onclick^="regenerate"]');
      if (regenerateButton) { regenerateButton.textContent = '重生成所在组'; regenerateButton.disabled = state.generalizing; }
    }
    preview.hidden = !pending;
    if (pending) {
      const count = pending.result.group.variants.length;
      preview.querySelector('pre').textContent = Array.from({length: count}, (_, i) =>
        `版本 ${i + 1}\n` + pending.result.paragraphs.map(p => p.candidates[i]).join('')).join('\n\n');
    }
  }

  return {active, groupFor, projectFields, complete, select, restore, createCopy,
    generateAll, regenerate, applyPending, scanRisks, blockedFindings, polishTargets, render: renderGroups};
})();
