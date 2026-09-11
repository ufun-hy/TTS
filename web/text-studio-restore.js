(() => {
  if (window.__ttsTextStudioRestoreLoaded) return;
  window.__ttsTextStudioRestoreLoaded = true;

  const RESTORE_META_KIND = 'script_restore_meta';
  state.restoreAnalysis = null;
  state.restoreSummary = null;

  const style = document.createElement('style');
  style.textContent = `.restore-rounds{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.restore-round{font-size:11px;padding:5px 7px;border-radius:999px;background:#eef2ff;color:#4338ca}.restore-unique{margin-top:7px;font-size:11px;color:#92400e}.restore-status{margin-top:8px;padding:8px 9px;border-radius:9px;background:#fff;border:1px solid var(--line);font-size:11px;color:#64748b;line-height:1.55}`;
  document.head.appendChild(style);

  const side = document.querySelector('.side');
  const anchor = $('factSettings') || side?.querySelector('.settings-details');
  if (side) {
    const details = document.createElement('details');
    details.className = 'settings-details';
    details.id = 'restoreSettings';
    details.innerHTML = `
      <summary>原稿还原（重复朗读）</summary>
      <div class="settings-body">
        <div class="hint">识别主播把同一份话术按原顺序重复朗读多遍，不做普通语义去重。单轮独有内容会标为疑似临场插话。</div>
        <div class="actions"><button class="btn ghost small" id="restoreAnalyzeBtn" type="button">分析重复轮次</button><button class="btn primary small" id="restoreApplyBtn" type="button" disabled>应用还原稿</button></div>
        <div class="restore-status" id="restoreSummary">尚未分析原稿。</div>
        <div id="restoreDetail"></div>
      </div>`;
    if (anchor) side.insertBefore(details, anchor); else side.appendChild(details);
  }

  function syncRestoreMeta() {
    const meta = {kind: RESTORE_META_KIND, version: 1, restore_summary: state.restoreSummary || null};
    state.replacementHistory = (state.replacementHistory || []).filter(x => x?.kind !== RESTORE_META_KIND);
    state.replacementHistory.unshift(meta);
  }

  function restoreMeta() {
    const meta = (state.replacementHistory || []).find(x => x?.kind === RESTORE_META_KIND);
    state.restoreSummary = meta?.restore_summary || null;
    state.restoreAnalysis = null;
    renderRestoreSummary();
  }

  const previousProjectPayload = projectPayload;
  projectPayload = function restoreProjectPayload() {
    syncRestoreMeta();
    return previousProjectPayload();
  };

  const previousLoadProject = loadProject;
  loadProject = async function restoreLoadProject(projectId) {
    const result = await previousLoadProject(projectId);
    restoreMeta();
    return result;
  };

  const previousNewProject = newProject;
  newProject = async function restoreNewProject(saveCurrent = true) {
    const result = await previousNewProject(saveCurrent);
    state.restoreAnalysis = null;
    state.restoreSummary = null;
    renderRestoreSummary();
    return result;
  };

  function renderRestoreSummary(custom = '') {
    const el = $('restoreSummary');
    const detail = $('restoreDetail');
    const apply = $('restoreApplyBtn');
    if (!el || !detail || !apply) return;
    if (custom) {
      el.textContent = custom;
      detail.innerHTML = '';
      apply.disabled = true;
      return;
    }
    const a = state.restoreAnalysis;
    if (!a) {
      if (state.restoreSummary?.applied) {
        el.textContent = `已应用原稿还原：${state.restoreSummary.original_units || 0} → ${state.restoreSummary.restored_units || 0} 个语句单元。`;
      } else {
        el.textContent = '尚未分析原稿。';
      }
      detail.innerHTML = '';
      apply.disabled = true;
      return;
    }
    if (!a.detected) {
      el.textContent = `未检测到稳定重复轮次 · 置信度 ${Math.round((a.confidence || 0) * 100)}% · ${a.message || ''}`;
      detail.innerHTML = '';
      apply.disabled = true;
      return;
    }
    el.textContent = `检测到 ${a.rounds?.length || 0} 轮 · 以第 ${a.skeleton_round || 1} 轮为骨架 · ${a.original_units || 0} → ${a.restored_units || 0} 个语句单元`;
    const align = new Map((a.alignments || []).map(x => [x.round, x]));
    detail.innerHTML = `<div class="restore-rounds">${(a.rounds || []).map(r => {
      const info = align.get(r.index);
      const coverage = info ? ` · 对齐${Math.round((info.coverage || 0) * 100)}%` : '';
      return `<span class="restore-round">第${r.index}轮 ${r.length}句${coverage}</span>`;
    }).join('')}</div><div class="restore-unique">疑似临场插话 / 单轮独有：${a.suspected_insertions?.length || 0} 项（不会自动并入标准稿）</div>`;
    apply.disabled = false;
  }

  async function analyzeRestore() {
    const text = $('sourceText')?.value || '';
    if (!text.trim()) {
      showMessage('请先上传或粘贴原稿。', 'err');
      return;
    }
    const btn = $('restoreAnalyzeBtn');
    if (btn) { btn.disabled = true; btn.textContent = '分析中…'; }
    renderRestoreSummary('正在分析整稿重复轮次…');
    try {
      state.restoreAnalysis = await api('/api/restore/analyze', {text});
      renderRestoreSummary();
      if (state.restoreAnalysis.detected) {
        showMessage(`检测到 ${state.restoreAnalysis.rounds?.length || 0} 轮疑似重复朗读，可查看后应用标准原稿。`, 'ok');
      } else {
        showMessage(state.restoreAnalysis.message || '没有检测到稳定的整稿重复朗读。', 'info');
      }
    } catch (e) {
      state.restoreAnalysis = null;
      renderRestoreSummary('原稿还原分析失败。');
      showMessage('原稿还原分析失败：' + e.message, 'err');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '分析重复轮次'; }
    }
  }

  async function applyRestore() {
    const a = state.restoreAnalysis;
    if (!a?.detected || !a.standard_text) return;
    if (generatedCount() && !confirm('应用原稿还原会重新解析文本，并清空当前泛化结果。确认继续吗？')) return;
    state.restoreSummary = {
      applied: true,
      original_units: a.original_units || 0,
      restored_units: a.restored_units || 0,
      round_count: a.rounds?.length || 0,
      skeleton_round: a.skeleton_round || 1,
      suspected_insertions: a.suspected_insertions?.length || 0,
      applied_at: new Date().toISOString(),
    };
    $('sourceText').value = a.standard_text;
    try {
      await parseText();
      state.restoreAnalysis = null;
      syncRestoreMeta();
      renderRestoreSummary();
      scheduleSave(0);
      showMessage(`原稿还原完成：${state.restoreSummary.original_units} → ${state.restoreSummary.restored_units} 个语句单元。疑似临场插话 ${state.restoreSummary.suspected_insertions} 项未自动并入。`, 'ok');
    } catch (e) {
      showMessage('应用还原稿失败：' + e.message, 'err');
    }
  }

  $('restoreAnalyzeBtn')?.addEventListener('click', analyzeRestore);
  $('restoreApplyBtn')?.addEventListener('click', applyRestore);
  $('sourceText')?.addEventListener('input', () => {
    if (state.restoreAnalysis) {
      state.restoreAnalysis = null;
      renderRestoreSummary();
    }
  });

  restoreMeta();
})();
