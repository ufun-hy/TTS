// Windows-only extension; the existing voice picker remains the source of selection.
(() => {
  const container = document.querySelector('.voice-inline');
  if (!container || document.getElementById('addVoice')) return;
  const button = document.createElement('button');
  button.id = 'addVoice';
  button.className = 'btn ghost small';
  button.type = 'button';
  button.textContent = '添加音色';
  button.style.whiteSpace = 'nowrap';
  container.append(button);
  const style = document.createElement('style');
  style.textContent = `
    #voiceDialog{width:min(560px,calc(100vw - 32px));max-height:90vh;overflow:auto;border:1px solid var(--line);border-radius:18px;padding:24px;color:var(--text);box-shadow:var(--shadow)}
    #voiceDialog::backdrop{background:rgb(15 23 42 / .4)}
    #voiceDialog h2{font-size:20px;margin:0 0 8px}
    #voiceDialog input[type=file]{display:block}
    #voiceDialog fieldset{border:0;padding:0;margin:0;min-width:0}
    #voiceDialog audio{width:100%;margin-top:10px}
    #voiceDialog .voice-confirm{display:flex;align-items:flex-start;gap:8px;font-size:13px;margin-top:12px}
    #voiceDialog .voice-confirm input{width:auto;margin-top:3px}
    #voiceDialog .voice-status{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.6}
    #voiceDialog .voice-status.error{color:#b91c1c}
    #voiceDialog button:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}
    @media(max-width:1100px){.modebar{flex-wrap:wrap}.voice-inline{flex-wrap:wrap;min-width:0}}
  `;
  document.head.append(style);
  const dialog = document.createElement('dialog');
  dialog.id = 'voiceDialog';
  dialog.setAttribute('aria-labelledby', 'voiceDialogTitle');
  dialog.innerHTML = `
    <h2 id="voiceDialogTitle">用录音创建音色</h2>
    <p class="hint">选一段单人、清晰、无背景音乐的录音。支持 3～30 秒，建议 8～20 秒；录音和音色保存在本机。</p>
    <form id="voiceCreateForm">
      <fieldset id="voiceFields">
        <div class="field"><label for="voiceName">音色名称</label><input id="voiceName" maxlength="60" required placeholder="例如：黄桃女声" autocomplete="off"></div>
        <div class="field"><label for="voiceRecording">参考录音（最多 50 MiB）</label><input id="voiceRecording" type="file" accept=".wav,.mp3,.m4a,.mp4" required></div>
        <audio id="voiceReferencePlayer" controls hidden aria-label="试听参考录音"></audio>
        <div class="field"><label for="voiceTranscript">录音原文</label><textarea id="voiceTranscript" rows="4" maxlength="2000" required placeholder="填写这段录音实际说出的每一句话，保留口头重复，不要润色。"></textarea></div>
        <label class="voice-confirm"><input id="voiceConfirmed" type="checkbox" required>我已核对原文与这段录音一致</label>
      </fieldset>
      <p id="voiceCreateStatus" class="voice-status" role="status" aria-live="polite"></p>
      <div class="actions"><button id="voiceCreateSubmit" class="btn primary" type="submit">创建音色</button><button id="voiceCreatePreview" class="btn ghost" type="button" hidden>试听新音色</button><button id="voiceCreateClose" class="btn ghost" type="button">关闭</button></div>
      <audio id="voiceResultPlayer" controls hidden aria-label="试听新音色"></audio>
    </form>`;
  document.body.append(dialog);
  const el = id => document.getElementById(id);
  let busy = false, ready = false, draftId = '', createdVoice = '', referenceUrl = '', resultUrl = '';
  const status = (text, error = false) => {
    el('voiceCreateStatus').textContent = text;
    el('voiceCreateStatus').classList.toggle('error', error);
  };
  async function request(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '请求失败，请重试');
    return body;
  }
  function setBusy(value) {
    busy = value;
    el('voiceFields').disabled = value || !!createdVoice;
    el('voiceCreateSubmit').disabled = value || !ready || !!createdVoice;
    el('voiceCreateClose').disabled = value;
    el('voiceCreatePreview').disabled = value;
    dialog.setAttribute('aria-busy', String(value));
  }
  button.addEventListener('click', async () => {
    if (createdVoice) {
      el('voiceCreateForm').reset();
      draftId = ''; createdVoice = '';
      el('voiceReferencePlayer').hidden = true;
      el('voiceResultPlayer').hidden = true;
      el('voiceCreatePreview').hidden = true;
    }
    ready = false; setBusy(false);
    dialog.showModal();
    status('检查创建音色所需文件…');
    try {
      const info = await request('/api/voice-registration');
      ready = info.ready;
      status(ready ? '填写名称、选择录音并核对原文后即可创建。' :
        '缺少以下文件，请补充到显示的位置后重新打开：\n' + info.missing.join('\n'), !ready);
    } catch (error) { status(error.message, true); }
    setBusy(false);
  });
  el('voiceCreateClose').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    el('voiceReferencePlayer').pause(); el('voiceResultPlayer').pause();
  });
  el('voiceRecording').addEventListener('change', () => {
    draftId = ''; el('voiceConfirmed').checked = false;
    if (referenceUrl) URL.revokeObjectURL(referenceUrl);
    const file = el('voiceRecording').files[0];
    el('voiceReferencePlayer').hidden = !file;
    if (file) {
      referenceUrl = URL.createObjectURL(file);
      el('voiceReferencePlayer').src = referenceUrl;
    }
  });
  el('voiceTranscript').addEventListener('input', () => { el('voiceConfirmed').checked = false; });
  el('voiceCreateForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !ready || createdVoice) return;
    const file = el('voiceRecording').files[0];
    if (!file || !file.size || file.size > 50 * 1024 * 1024) {
      status('请选择不超过 50 MiB 的录音文件。', true); return;
    }
    setBusy(true);
    try {
      if (!draftId) {
        status('正在上传并检查录音…');
        const upload = await request('/api/voice-registration/upload', {
          method: 'POST', headers: {'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name)}, body: file,
        });
        draftId = upload.draft_id;
      }
      status('正在创建音色，请保持页面打开，通常需要几十秒…');
      const voice = await request('/api/voice-registration/register', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({draft_id: draftId, name: el('voiceName').value,
          transcript: el('voiceTranscript').value, confirmed: el('voiceConfirmed').checked}),
      });
      createdVoice = voice.id;
      await loadVoices(voice.id);
      el('voiceCreatePreview').hidden = false;
      status(document.getElementById('voice').value === voice.id
        ? `“${voice.label}”已创建并选中。请点击“试听新音色”检查效果。`
        : `“${voice.label}”已创建，音色列表暂未更新，请稍后刷新页面。也可以先试听。`);
    } catch (error) { status(error.message + '（录音和原有音色均保留，可重试。）', true); }
    finally { setBusy(false); }
  });
  el('voiceCreatePreview').addEventListener('click', async () => {
    setBusy(true); status('正在合成试听…');
    try {
      const response = await fetch('/api/tts/preview', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({voice: createdVoice, text: '你好，这是新音色的试听。欢迎来到直播间，很高兴和大家见面。'}),
      });
      if (!response.ok) throw new Error((await response.json()).error || '试听生成失败');
      if (resultUrl) URL.revokeObjectURL(resultUrl);
      resultUrl = URL.createObjectURL(await response.blob());
      el('voiceResultPlayer').src = resultUrl; el('voiceResultPlayer').hidden = false;
      status('试听已生成，请播放并确认声音效果。');
      await el('voiceResultPlayer').play().catch(() => {});
    } catch (error) { status('音色已创建，试听失败：' + error.message, true); }
    finally { setBusy(false); }
  });
})();
