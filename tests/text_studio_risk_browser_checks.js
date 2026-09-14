// Run in a fresh Text Studio page backed by an isolated project root.
(async () => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const waitUntil = async predicate => {
    const deadline = Date.now() + 5000;
    while (!await predicate()) {
      if (Date.now() > deadline) throw new Error('Timed out waiting for automatic save');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  };
  await newProject(false);
  $('projectName').value = '时间与赔付风险验收';
  const texts = [
    '现在大家看看这个产品。有问题联系客服处理售后。',
    '  欢迎光临。现在是晚上8点。今天是9月13日。还有10分钟结束。',
    '坏果包赔。不满意就赔。假一赔十。无条件赔。',
  ];
  $('sourceText').value = texts.join('\n\n');
  state.paragraphs = texts.map((text, i) => ({
    id: `p000${i + 1}`, index: i + 1, original_text: text,
    candidates: ['未选候选，现在是晚上9点，包赔。', text], selectedIndex: 1,
    editedText: text, expanded: false,
  }));
  render();
  $('riskBtn').click();
  await state.saveChain;
  check(state.riskFindings.length === 7, 'must scan only selected final text');
  check(!state.riskFindings.some(f => f.paragraph_index === 0), 'safe text flagged');
  check(riskCounts().medium === 3 && riskCounts().high === 4, 'wrong severity counts');
  const dynamicHit = state.riskFindings.find(f => f.paragraph_index === 1 && f.type === '时间点');
  check(dynamicHit.dynamic?.token === 'current_time', 'time risk is not dynamically convertible');
  const id = state.projectId;
  await loadProject(id);
  check(state.riskFindings.length === 7, 'risk results not persisted');
  openRisk();
  document.querySelector('#riskSummary [onclick="setRiskFilter(\'high\')"]').click();
  check(document.querySelectorAll('#riskResults .risk-item').length === 4, 'high filter broken');
  setRiskFilter('all');
  check([...document.querySelectorAll('#riskResults button')].some(b => b.textContent === '改为动态时间'), 'dynamic time action missing');
  const replacements = [
    '欢迎来到直播间。活动安排以页面实时信息为准。',
    '如有售后问题，请联系客服按平台规则处理。',
  ];
  for (const [index, replacement] of replacements.entries()) {
    const pi = index + 1;
    const hit = state.riskFindings.find(f => f.paragraph_index === pi);
    const button = [...document.querySelectorAll('#riskResults button')]
      .find(b => b.getAttribute('onclick') === `locateRiskById('${hit.paragraph_id}',${hit.position})`);
    check(button, 'locate button missing');
    button.click();
    await new Promise(resolve => requestAnimationFrame(resolve));
    const editor = document.querySelector(`#para-${pi} textarea.candidate`);
    check(state.riskFocusParagraph === pi && state.paragraphs[pi].expanded, 'wrong paragraph located');
    check(!document.getElementById('riskDrawer').classList.contains('open'), 'drawer not closed');
    check(document.activeElement === editor, 'editor not focused');
    check(editor.value.slice(editor.selectionStart, editor.selectionEnd) === hit.phrase, 'wrong phrase selected');
    editor.value = replacement;
    editor.dispatchEvent(new Event('input', {bubbles: true}));
    check(!state.riskChecked && state.riskFindings.length === 0, 'stale results after edit');
    // Read real persisted state without invoking save manually.
    await waitUntil(async () => {
      const saved = (await apiGet('/api/project?project_id=' + id)).project;
      return saved.paragraphs[pi].editedText === replacement && saved.risk_findings.length === 0;
    });
    await loadProject(id);
    check(selectedText(state.paragraphs[pi]) === replacement, 'edited text lost on reload');
    check(state.paragraphs[pi].candidates[1] === replacement, 'selected candidate not saved');
    check(state.paragraphs[pi].candidates[0].includes('晚上9点'), 'unselected candidate changed');
    $('riskBtn').click();
    await state.saveChain;
  }
  check(state.riskFindings.length === 0 && state.riskChecked, 'safe final text still flagged');
  check($('liveRiskStatus').textContent === '已通过', 'live risk summary stale');
  return {passed: true, projectId: id, verified: 'detect → locate → select phrase → edit → autosave → reload → rescan'};
})()
