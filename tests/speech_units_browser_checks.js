// Execute in a Text Studio page backed by a temporary project root only.
(async () => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  await newProject(false);
  const source = '这个石榴汁水很多。吃起来很甜。籽也比较软。';
  $('sourceText').value = source;
  await parseText();
  check(state.paragraphs.length === 3, '完整句被重新合并');
  check(state.paragraphs.map(p=>p.original_text).join('') === source, '原文不一致');
  await queueSave(false);
  const parsedId = state.projectId;
  await loadProject(parsedId);
  check(state.paragraphs.length === 3, '新单元保存恢复失败');

  const longText = '姐妹们这个石榴是四川会理发过来的，果子个头比较大，汁水也比较足，吃起来很甜，而且籽还特别软。';
  await newProject(true);
  $('sourceText').value = longText;
  await parseText();
  check(state.paragraphs.length > 1, '长逗号句未拆分');
  check(state.paragraphs.every(p=>p.original_text.length <= 35), '普通话术未缩短');
  check(state.paragraphs.map(p=>p.original_text).join('') === longText, '逗号拆分丢失内容');

  const legacy = await api('/api/project/save', {
    name:'旧结构兼容验收', source_text:source,
    paragraphs:[{id:'p0001',index:1,original_text:source,candidates:['候选一。','候选二。'],selectedIndex:1,editedText:'人工确认到手19.9元，六个。'}],
    replacement_history:[{kind:'product_fact_meta',product_facts:{price:'19.9'},fact_checked:true}]
  });
  await loadProject(legacy.project_id);
  check(state.paragraphs.length === 1, '打开旧项目触发自动重切');
  check(state.paragraphs[0].candidates.length === 2 && state.paragraphs[0].selectedIndex === 1, '旧候选丢失');
  check(selectedText(state.paragraphs[0]) === '人工确认到手19.9元，六个。', '人工修改丢失');
  check($('factPrice').value === '19.9', '事实配置丢失');
  await queueSave(true);
  await loadProject(legacy.project_id);
  check(state.paragraphs.length === 1, '自动保存迁移了旧结构');
  return {passed:true, strongSentenceUnits:3, legacyUnits:state.paragraphs.length};
})()
