// Run in a fresh Text Studio browser page backed by a temporary project root.
(async () => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const initialCount=(await apiGet('/api/projects')).projects.length;
  await newProject(false);
  state.providerStatus = {}; // deterministic fallback; no real model or TTS calls
  $('projectName').value = '验收清理项目';
  $('sourceText').value = '来自云南果园。这里阳光充足。口感清甜多汁。到手19.9元，一箱5斤。48小时内发货。坏果包赔。';
  await parseText();
  check(state.paragraphs.length >= 4, 'topics were not separated');
  check(state.projectKind === 'draft', 'parse promoted draft');
  check((await apiGet('/api/projects')).projects.length === initialCount, 'draft visible in default list');
  state.paragraphs = [{id:'p0001',index:1,original_text:'到手19.9元，一箱5斤。',candidates:['到手19.9元，一箱5斤。','现在29.9，一箱5斤。哥哥我推荐。','只要三十九块九。送运费险。坏果包赔。'], selectedIndex:0,editedText:'到手19.9元，一箱5斤。'}];
  $('factPrice').value = '9.9';$('factPersona').value='neutral';$('factShippingInsurance').value='no';$('factCompensation').value='no';
  $('factCheckBtn').click();
  check(state.factFindings.some(f=>f.candidate_index===2), 'hidden candidate not scanned');
  check(!$('factFixBtn').disabled,'fix button not enabled');
  $('factFixBtn').click();
  await new Promise(r=>setTimeout(r,50));
  check(state.paragraphs[0].candidates[2].includes('为准。'), 'sentence punctuation lost');
  check(state.paragraphs[0].selectedIndex===0,'selection changed');
  check(state.paragraphs[0].candidates.every(t=>!t.includes('19.9')&&!t.includes('29.9')&&!t.includes('三十九')&&!t.includes('哥哥我')&&!t.includes('送运费险')&&!t.includes('坏果包赔')),'candidate facts remain');
  check(state.paragraphs[0].original_text.includes('9.9元'),'regeneration source is stale');
  check(state.paragraphs[0].candidates[0].includes('5斤'),'specification lost');
  selectCandidate(0,2);
  check(selectedText(state.paragraphs[0])===state.paragraphs[0].candidates[2],'candidate switch stale');
  // A provider response that restores a conflicting fact must be rejected.
  const originalApi=api, originalProvider=$('provider').value;
  $('provider').value='codex';state.providerStatus={codex:true};
  state.paragraphs[0].candidates[1]='现在9.9元。送运费险。';
  api=async(path,body)=>path==='/api/generalize'?{paragraphs:body.paragraphs.map(p=>({id:p.id,candidates:['到手99.9元。送运费险。']}))}:originalApi(path,body);
  try {
    $('factCheckBtn').click();$('factFixBtn').click();
    await new Promise(r=>setTimeout(r,50));
    check(!state.paragraphs[0].candidates[1].includes('99.9')&&!state.paragraphs[0].candidates[1].includes('送运费险'),'polish restored stale facts');
  } finally {api=originalApi;state.providerStatus={};$('provider').value=originalProvider;}
  await queueSave(false);
  const id=state.projectId;
  check((await apiGet('/api/projects')).projects.length===initialCount+1,'explicit save not listed');
  await loadProject(id);
  check(state.paragraphs[0].candidates.length===3,'candidates lost on reload');
  check(state.paragraphs[0].selectedIndex===2,'selection lost on reload');
  const before=JSON.stringify((await apiGet('/api/project?project_id='+id)).project.paragraphs);
  $('importProjectSelect').value=id;
  await $('importProjectBtn').onclick();
  check(state.projectId!==id && state.projectKind==='draft','import overwrote source');
  check(state.paragraphs.length>0,'import empty');
  check(JSON.stringify((await apiGet('/api/project?project_id='+id)).project.paragraphs)===before,'source project changed');
  check((await apiGet('/api/projects')).projects.length===initialCount+1,'import polluted project list');
  return {passed:true, savedProject:id, importedUnits:state.paragraphs.length};
})()
