/* Server-owned preparation keeps running while the next text batch is generated. */
const TextStudioPreparation = (() => {
  let timer = null;
  let current = {status: 'idle'};
  let pending = false;
  let observedProject;

  function render(data) {
    if (data && data.project_id !== state.projectId) return;
    if (data) current = data;
    const labels = {idle: '尚未开始音频准备', waiting_text: '等待下一批泛化结果',
      preparing: '正在准备首轮音频', ready: '首轮音频已准备好', paused: '音频准备已暂停', failed: '音频准备需要处理'};
    $('audioPreparationStatus').textContent = current.error ||
      `${labels[current.status] || current.status} · ${current.prepared_segments || 0}/${current.total_segments || 0} 块`;
    $('audioPreparationHint').textContent = '每批保存后自动合成完整块；尾块等待下一批。准备好后点击开播，不会自动播放。';
    $('audioPreparationStart').disabled = pending || !state.paragraphs.length ||
      (current.enabled === true && current.status !== 'failed');
    $('audioPreparationStart').textContent = current.status === 'paused' || current.status === 'failed' ? '继续准备音频' : '准备首轮音频';
    $('audioPreparationPause').disabled = pending || current.enabled !== true;
  }

  async function refresh() {
    const id = state.projectId;
    if (!id) { current = {status: 'idle'}; render(); return; }
    try {
      const data = await apiGet('/api/preparation/status?project_id=' + encodeURIComponent(id));
      if (state.projectId === id) render(data);
    } catch (e) {
      if (state.projectId === id) $('audioPreparationStatus').textContent = '读取音频准备状态失败：' + e.message;
    }
  }

  function watch() {
    if (!timer) timer = setInterval(refresh, 2000);
  }

  async function begin() {
    if (pending) return;
    pending = true;
    render();
    try {
      await queueSave(true);
      const id = state.projectId;
      if (!id) throw new Error('请先保存项目');
      render(await api('/api/preparation/start', {project_id: id}));
      watch();
    } finally { pending = false; render(); }
  }

  async function beforeGeneralize() {
    if (!$('prepareWhileGeneralizing').checked) return;
    try { await begin(); }
    catch (e) {
      // Text generation remains usable when TTS is unavailable.
      $('audioPreparationStatus').textContent = '音频准备未启动：' + e.message;
    }
  }

  async function pause() {
    if (!state.projectId) return;
    render(await api('/api/preparation/pause', {project_id: state.projectId}));
  }

  function projectChanged() {
    current = {status: 'idle'};
    render();
    refresh();
    watch();
  }
  function observeProject() {
    if (observedProject === state.projectId) return;
    observedProject = state.projectId;
    projectChanged();
  }
  return {begin, pause, beforeGeneralize, render, observeProject};
})();
