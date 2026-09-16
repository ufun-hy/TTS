// Execute in the isolated Text Studio fixture with /test-observed; never use a real live manager.
(async () => {
  const check = (value, message) => {if (!value) throw new Error(message);};
  const waitUntil = async condition => {
    const until = Date.now()+5000;
    while (!await condition()) {if (Date.now()>until) throw new Error('autosave timed out');await new Promise(r=>setTimeout(r,50));}
  };
  await newProject(false);
  $('projectName').value='禁止播报验收';
  const raw='大家可以先试吃，不好吃不要钱。口感清甜。';
  state.paragraphs=[
    {id:'p1',index:1,original_text:raw,candidates:[raw,'有问题联系客服处理售后。'],selectedIndex:0,editedText:raw},
    {id:'p2',index:2,original_text:'免费试用。',candidates:['免费试用。'],selectedIndex:0,editedText:'免费试用。'},
  ];
  state.ttsReady=true;render();renderRiskDrawer();
  check(state.riskFindings.filter(f=>f.blocked).length===2,'automatic prohibition missing');
  check(document.querySelector('#para-0 .prohibited-mark').textContent.includes('先试吃'),'source not red');
  check(document.querySelector('#para-0 .generated-preview').textContent==='口感清甜。','unsafe final preview');
  check(document.querySelector('#para-1 .para-head-actions button').disabled,'empty paragraph preview enabled');
  check(JSON.stringify(liveCandidatePools())===JSON.stringify([{id:'p1',candidates:['口感清甜。']}]),'unsafe live candidates');
  $('riskBtn').click();setRiskFilter('blocked');
  check(document.querySelectorAll('#riskResults .prohibited-item').length===2,'blocked filter missing');
  check(![...document.querySelectorAll('#riskResults button')].some(b=>b.textContent==='忽略'),'prohibition can be ignored');
  const finding=state.riskFindings.find(f=>f.paragraph_id==='p1'&&f.blocked);
  ignoreRiskFinding('p1',finding.position,finding.phrase);
  check(broadcastText(state.paragraphs[0])==='口感清甜。','ignore bypassed block');
  await preview(0);await preview(1);await startLive();
  let observed=await apiGet('/test-observed');
  check(observed.preview.length===1&&observed.preview[0].text==='口感清甜。','TTS received banned text or empty paragraph');
  check(observed.live.length===1&&observed.live[0][0].candidates[0]==='口感清甜。','live backend received banned text');
  clearInterval(state.liveTimer);state.liveTimer=null;state.liveStatus={status:'idle'};
  locateRiskById('p1',finding.position);
  await new Promise(r=>requestAnimationFrame(r));
  let editor=document.querySelector('#para-0 textarea');
  check(document.activeElement===editor&&editor.value.slice(editor.selectionStart,editor.selectionEnd)===finding.phrase,'locate did not select full sentence');
  const repaired='口感清甜。有问题联系客服处理售后。';
  editor.value=repaired;editor.dispatchEvent(new Event('input',{bubbles:true}));
  check(document.querySelector('#para-0 textarea')===editor,'editing replaced focused textarea');
  check(broadcastText(state.paragraphs[0])===repaired,'edit did not update broadcast text');
  await waitUntil(async()=>{if(!state.projectId)return false;const p=(await apiGet('/api/project?project_id='+state.projectId)).project;return p.paragraphs[0].editedText===repaired;});
  const id=state.projectId;await loadProject(id);
  check(state.paragraphs[0].original_text===raw,'original text destroyed');
  check(broadcastText(state.paragraphs[1])==='','reload restored blocked text');
  selectCandidate(0,1);check(broadcastText(state.paragraphs[0])==='有问题联系客服处理售后。','safe candidate switch failed');
  state.paragraphs[0].candidates[0]=raw;selectCandidate(0,0);
  check(broadcastText(state.paragraphs[0])==='口感清甜。','unsafe candidate switch escaped filtering');
  // Export only actual broadcast text, while saved editing data retains the source.
  const originalDownload=download;const downloads=[];
  download=(name,content)=>downloads.push({name,content});
  try {$('exportTxt').click();$('exportJson').click();} finally {download=originalDownload;}
  check(downloads[0].content==='口感清甜。','TXT export leaked source');
  const exported=JSON.parse(downloads[1].content);
  check(exported.paragraphs.length===1&&exported.paragraphs[0].selected_text==='口感清甜。','JSON selected text unsafe');
  check(exported.paragraphs[0].candidates.every(t=>!TextStudioProhibited.analyze(t).blocked.length),'JSON candidates unsafe');
  editCandidate(0,'七天无理由。');
  check(!broadcastReady()&&!liveCandidatePools().length,'all blocked document remained broadcastable');
  await preview(0);await startLive();
  observed=await apiGet('/test-observed');
  check(observed.preview.length===1&&observed.live.length===1,'all-blocked action made TTS request');
  // An emptied edit must never fall back to the original source.
  editCandidate(0,'');check(broadcastText(state.paragraphs[0])==='','empty edit restored original');
  await queueSave(true);
  return {passed:true,projectId:id,checks:'automatic red marks, whole-sentence suppression, locate/edit/autosave/reload, candidate changes, exports, preview/live transport, empty guards'};
})()
