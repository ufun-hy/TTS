(() => {
  const $ = id => document.getElementById(id), base = '/api/voice-registration/jobs';
  const active = new Set(['queued','decoding','vad','quality','emotion','transcript','preparing','preview','saving','pausing']);
  const labels = {paused:'已暂停',failed:'处理失败',ready:'候选已就绪',queued:'准备开始',decoding:'解码录音',vad:'检测人声',quality:'检查音质',emotion:'分析声音风格',transcript:'转写参考原文',preparing:'准备参考音频',preview:'合成对比试听',saving:'保存音色',pausing:'正在暂停'};
  let selected = '', job = null, polling = false, renderKey = '', sending = false;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function message(value,error=false){$('message').textContent=value;$('message').classList.toggle('error',error)}
  async function api(url, body) {
    const response = await fetch(url,body === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json(); if(!response.ok) throw new Error(data.error || '请求失败'); return data;
  }
  async function list(preferred='') {
    const data=await api(base), previous=preferred || selected;
    $('jobs').replaceChildren(new Option('选择一个任务',''),...data.jobs.map(j=>new Option(`${j.name} · ${labels[j.state]||j.state} · ${j.filename}`,j.id)));
    if(data.jobs.some(j=>j.id===previous)) $('jobs').value=previous;
  }
  function candidateHTML(c) {
    const media=variant=>`${base}/${job.id}/media/${c.id}/${c.version}/${variant}`;
    const emotion=c.emotion || {}, score=Number(emotion.score||0);
    return `<article class="candidate" data-id="${c.id}"><header><h3>${escape(c.style)}</h3><span class="badge">${c.start.toFixed(2)}–${c.end.toFixed(2)} 秒</span></header>
      <p class="hint">${escape(emotion.suggested||'待确认')} · 模型标签 ${escape(emotion.label||'未分析')} · 分数 ${score.toFixed(2)}（不是准确率）</p>
      <p class="hint">${escape(c.quality.flags.join('；')||'请试听确认无插话、音乐及明显混响')} · 人声占比 ${Math.round(c.quality.speech_ratio*100)}% · ${c.quality.rms_dbfs} dBFS${c.characters_per_second ? ` · ${c.characters_per_second} 字/秒` : ''}${c.pitch_range_hz == null ? '' : ` · 音高变化范围 ${c.pitch_range_hz} Hz`}</p>
      <fieldset ${active.has(job.state)||c.registered?'disabled':''} style="border:0;padding:0;margin:0;min-width:0">
      <div class="fields"><label>开始秒数<input data-field="start" type="number" step="0.01" value="${c.start.toFixed(3)}"></label><label>结束秒数<input data-field="end" type="number" step="0.01" value="${c.end.toFixed(3)}"></label>
      <label>参考版本<select data-field="variant"><option value="denoised" ${c.variant==='denoised'?'selected':''}>轻降噪（6 dB）</option><option value="original" ${c.variant==='original'?'selected':''}>原音</option></select></label>
      <label>风格名称<input data-field="style" maxlength="20" value="${escape(c.style)}"></label></div>
      <button data-action="edit">应用范围／版本／风格</button>
      <p class="hint">改变范围或版本会重新转写，清除旧确认和试听。</p>
      ${c.prepared?`<label>参考录音<audio controls preload="none" src="${media(c.variant)}"></audio></label><details><summary>对比${c.variant==='denoised'?'原音':'轻降噪版'}</summary><audio controls preload="none" src="${media(c.variant==='denoised'?'original':'denoised')}"></audio></details>`:''}
      <label>实际原文<textarea data-field="text" maxlength="2000" rows="3">${escape(c.text||'')}</textarea></label>
      <label class="check"><input data-field="confirmed" type="checkbox" ${c.confirmed?'checked':''}>已核对同一主播、完整边界和原文一致</label>
      <div class="actions"><button data-action="confirm" ${c.prepared?'':'disabled'}>保存核对</button><button data-action="preview" ${c.confirmed?'':'disabled'}>用统一文案生成试听</button></div>
      ${c.preview?`<label>新文案试听<audio controls preload="none" src="${media('preview')}"></audio></label><p class="hint">试听文案：${escape(c.preview_text)}</p><label class="check"><input data-field="heard" type="checkbox">我已试听，确认音色和风格效果</label><button class="primary" data-action="save">保存音色</button>`:''}
      </fieldset>${c.registered?`<p>已保存：${escape(c.registered.label)}。返回工作台刷新音色列表即可使用。</p>`:''}</article>`;
  }
  function render(data) {
    job=data; const busy=active.has(job.state);
    $('jobStatus').textContent=`${job.name} · ${labels[job.state]||job.state}`;
    $('pause').disabled=!busy || job.state==='pausing'; $('resume').disabled=busy || !job.pending;
    $('more').disabled=busy || !job.candidates.some(c=>c.quality.usable&&!c.prepared);
    const p=job.progress||{}, total=p.total_seconds||p.total, done=p.processed_seconds||p.completed||0;
    $('jobProgress').hidden=!busy||!total; $('jobProgress').value=total?100*done/total:0;
    if(total&&busy) $('jobStatus').textContent+=` · ${Math.floor(done)} / ${Math.ceil(total)}${p.total_seconds?' 秒':''}`;
    if(job.error||job.notice) message(job.error||job.notice,!!job.error);
    const visible=job.candidates.filter(c=>c.prepared || (job.recommended_ids||[]).includes(c.id));
    const key=JSON.stringify([job.id,busy,visible]);
    if(renderKey!==key) {
      renderKey=key;
      const groups=new Map();for(const c of visible){if(!groups.has(c.style))groups.set(c.style,[]);groups.get(c.style).push(c)}
      $('candidates').innerHTML=visible.length?[...groups].map(([style,items])=>`<h3>${escape(style)}</h3>${items.map(candidateHTML).join('')}`).join(''):'<p class="empty">尚无候选；分析完成后可在这里核对和试听。</p>';
    }
  }
  async function poll() {
    if(!selected||polling||sending)return; polling=true;
    try {const id=selected;await api(`${base}/${id}/heartbeat`,{});const data=await api(`${base}/${id}`);if(id===selected)render(data)}
    catch(error){message(error.message,true)}finally{polling=false}
  }
  async function act(action,body={}) {
    sending=true;message('');
    try{const data=await api(`${base}/${selected}/${action}`,body);render(data)}
    catch(error){message(error.message,true)}finally{sending=false}
  }
  $('candidates').addEventListener('input', event=>{
    const card=event.target.closest('[data-id]');if(!card)return;
    if(['text','start','end','variant'].includes(event.target.dataset.field)) {
      card.querySelector('[data-field="confirmed"]').checked=false;
      const preview=card.querySelector('[data-action="preview"]');if(preview)preview.disabled=true;
      const save=card.querySelector('[data-action="save"]');if(save)save.disabled=true;
    }
  });
  $('candidates').addEventListener('click',async event=>{
    const button=event.target.closest('[data-action]');if(!button||sending)return;
    const card=button.closest('[data-id]'),c=job.candidates.find(c=>c.id===card.dataset.id);
    const field=name=>card.querySelector(`[data-field="${name}"]`);
    const action=button.dataset.action;
    const body={candidate_id:c.id,version:c.version};
    if(action==='edit'||action==='confirm')Object.assign(body,{start:field('start').value,end:field('end').value,variant:field('variant').value,style:field('style').value,text:field('text').value,confirmed:action==='confirm'&&field('confirmed').checked});
    if(action==='preview')body.test_text=$('testText').value;
    if(action==='save')body.heard=field('heard').checked;
    await act(action==='confirm'?'edit':action,body);
  });
  $('jobs').onchange=()=>{if(selected)navigator.sendBeacon(`${base}/${selected}/pause`,'');selected=$('jobs').value;renderKey='';message('');poll()};
  $('refreshJobs').onclick=()=>list().catch(e=>message(e.message,true));
  $('pause').onclick=()=>act('pause');$('resume').onclick=()=>act('resume');$('more').onclick=()=>act('more');
  $('uploadForm').onsubmit=async event=>{
    event.preventDefault();const file=$('sourceFile').files[0];if(!file||!file.size||file.size>2*1024**3){message('请选择不超过 2 GiB 的录音',true);return}
    $('uploadButton').disabled=true;$('uploadProgress').hidden=false;message('');
    const params=new URLSearchParams({name:$('speakerName').value,start:$('rangeStart').value});if($('rangeEnd').value)params.set('end',$('rangeEnd').value);
    try{
      const data=await new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();xhr.open('POST',base+'?'+params);xhr.setRequestHeader('X-Filename',encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type','application/octet-stream');
        xhr.upload.onprogress=e=>{if(e.lengthComputable)$('uploadProgress').value=e.loaded/e.total*100};
        xhr.onerror=()=>reject(new Error('上传连接中断，请检查任务列表后重试'));
        xhr.onload=()=>{try{const d=JSON.parse(xhr.responseText);xhr.status<300?resolve(d):reject(new Error(d.error||'上传失败'))}catch(e){reject(e)}};xhr.send(file);
      });
      if(selected)navigator.sendBeacon(`${base}/${selected}/pause`,'');selected=data.id;await list(selected);render(data);
    }catch(error){message(error.message,true)}finally{$('uploadButton').disabled=false;$('uploadProgress').hidden=true}
  };
  window.addEventListener('pagehide',()=>{if(selected)navigator.sendBeacon(`${base}/${selected}/pause`,'')});
  (async()=>{
    try{const status=await api(base+'/readiness');$('uploadButton').disabled=!status.ready;$('readiness').textContent=status.ready?'离线模型就绪，录音不会上传到第三方。':'缺少文件或依赖：\n'+status.missing.join('\n');await list()}
    catch(error){$('readiness').textContent=error.message}
    setInterval(poll,1800);
  })();
})();
