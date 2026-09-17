// Search the original text and every saved generalized candidate.
// This module also installs the shared browser-side search/risk navigation fixes
// used by both macOS and Windows Text Studio pages.
const TextStudioSearch = (() => {
  function sources(paragraph) {
    const original = typeof paragraph?.original_text === 'string'
      ? [{kind: 'original', candidateIndex: null, text: paragraph.original_text}]
      : [];
    const candidates = Array.isArray(paragraph?.candidates)
      ? paragraph.candidates
          .map((text, candidateIndex) => ({
            kind: 'candidate',
            candidateIndex,
            // selectedText() trims the editable/final candidate. Search candidates
            // in the same coordinate system so hit.start remains safe for replace.
            text: typeof text === 'string' ? text.trim() : text,
          }))
          .filter(source => typeof source.text === 'string')
      : [];
    return original.concat(candidates);
  }

  function findMatches(paragraphs, query) {
    const needle = String(query ?? '').trim();
    if (!needle) return [];
    const matches = [];
    (Array.isArray(paragraphs) ? paragraphs : []).forEach((paragraph, paragraphIndex) => {
      sources(paragraph).forEach(source => {
        let start = source.text.indexOf(needle);
        while (start >= 0) {
          matches.push({paragraphIndex, start, kind: source.kind, candidateIndex: source.candidateIndex});
          start = source.text.indexOf(needle, start + Math.max(1, needle.length));
        }
      });
    });
    return matches;
  }

  function selectedCandidateIndex(paragraph) {
    return Number.isInteger(paragraph?.selectedIndex) ? paragraph.selectedIndex : 0;
  }

  function replacementForHit(paragraph, hit, query, replacement) {
    const needle = String(query ?? '').trim();
    if (!needle) return {ok: false, reason: 'empty_query'};
    if (!hit || hit.kind !== 'candidate' || !Number.isInteger(hit.candidateIndex)) {
      return {ok: false, reason: 'read_only_source'};
    }
    if (!Array.isArray(paragraph?.candidates)
        || hit.candidateIndex < 0
        || hit.candidateIndex >= paragraph.candidates.length) {
      return {ok: false, reason: 'invalid_candidate'};
    }
    if (hit.candidateIndex !== selectedCandidateIndex(paragraph)) {
      return {ok: false, reason: 'candidate_not_selected', candidateIndex: hit.candidateIndex};
    }

    const text = String(paragraph.candidates[hit.candidateIndex] ?? '').trim();
    const start = Number.isInteger(hit.start) ? hit.start : -1;
    if (start < 0 || text.slice(start, start + needle.length) !== needle) {
      return {ok: false, reason: 'stale_match'};
    }
    const next = text.slice(0, start) + String(replacement ?? '') + text.slice(start + needle.length);
    return {ok: true, candidateIndex: hit.candidateIndex, start, text: next};
  }

  function findingOffset(editorValue, finding) {
    const text = String(editorValue ?? '');
    const phrase = String(finding?.phrase ?? '');
    if (!phrase) return -1;
    const expected = Number.isInteger(finding?.position) ? finding.position : -1;
    if (expected >= 0 && text.slice(expected, expected + phrase.length) === phrase) return expected;

    const positions = [];
    let at = text.indexOf(phrase);
    while (at >= 0) {
      positions.push(at);
      at = text.indexOf(phrase, at + Math.max(1, phrase.length));
    }
    if (!positions.length) return -1;
    if (expected < 0) return positions[0];
    return positions.reduce((best, current) => (
      Math.abs(current - expected) < Math.abs(best - expected) ? current : best
    ), positions[0]);
  }

  function riskContext(text, finding, radius = 42) {
    const source = String(text ?? '');
    const phrase = String(finding?.phrase ?? '');
    const start = findingOffset(source, finding);
    if (start < 0 || !phrase) {
      return {before: source.slice(0, radius), phrase: '', after: source.slice(radius, radius * 2), start: -1};
    }
    const left = Math.max(0, start - radius);
    const right = Math.min(source.length, start + phrase.length + radius);
    return {
      before: (left > 0 ? '…' : '') + source.slice(left, start),
      phrase,
      after: source.slice(start + phrase.length, right) + (right < source.length ? '…' : ''),
      start,
    };
  }

  function installUiFixes() {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;

    const install = () => {
      const prevButton = document.getElementById('prevMatch');
      const nextButton = document.getElementById('nextMatch');
      const replaceButton = document.getElementById('replaceCurrent');
      if (!prevButton || !nextButton || !replaceButton) return;

      function scrollToSearchHit(hit) {
        if (!hit) return;
        window.requestAnimationFrame(() => {
          const paragraph = document.getElementById(`para-${hit.paragraphIndex}`);
          if (!paragraph) return;

          if (hit.kind === 'original') {
            const original = paragraph.querySelector('.compare-col:first-child .compare-body');
            (original || paragraph).scrollIntoView({behavior: 'smooth', block: 'center'});
            return;
          }

          const tabs = paragraph.querySelectorAll('.tabs .tab');
          const tab = tabs[hit.candidateIndex];
          const paragraphState = state.paragraphs[hit.paragraphIndex];
          if (tab) {
            tab.scrollIntoView({behavior: 'smooth', block: 'center', inline: 'nearest'});
            tab.style.boxShadow = '0 0 0 3px #bfdbfe';
            window.setTimeout(() => {
              if (tab.isConnected) tab.style.boxShadow = '';
            }, 1200);
          } else {
            paragraph.scrollIntoView({behavior: 'smooth', block: 'center'});
          }

          if (selectedCandidateIndex(paragraphState) === hit.candidateIndex) {
            const editor = paragraph.querySelector('textarea.candidate');
            if (!editor) return;
            editor.scrollIntoView({behavior: 'smooth', block: 'center'});
            const needle = String(state.searchQuery ?? '').trim();
            if (needle && editor.value.slice(hit.start, hit.start + needle.length) === needle) {
              editor.focus({preventScroll: true});
              editor.setSelectionRange(hit.start, hit.start + needle.length);
            }
          } else if (typeof showMessage === 'function') {
            showMessage(`命中候选 ${hit.candidateIndex + 1}，请先点击该候选再修改；搜索不会自动改变最终智播候选。`, 'info');
          }
        });
      }

      function patchedJumpToMatch(delta = 0) {
        if (!state.searchMatches.length) return;
        state.searchIndex = (state.searchIndex + delta + state.searchMatches.length) % state.searchMatches.length;
        const hit = state.searchMatches[state.searchIndex];
        state.searchFocusParagraph = hit.paragraphIndex;
        state.riskFocusParagraph = -1;
        state.paragraphs[hit.paragraphIndex].expanded = true;
        render();
        scrollToSearchHit(hit);
      }

      function patchedReplaceCurrent() {
        if (!state.searchMatches.length || !state.searchQuery) return;
        const hit = state.searchMatches[state.searchIndex];
        const paragraph = state.paragraphs[hit.paragraphIndex];
        const replacement = document.getElementById('replaceText')?.value ?? '';
        const result = replacementForHit(paragraph, hit, state.searchQuery, replacement);

        if (!result.ok) {
          if (result.reason === 'read_only_source') {
            showMessage('当前搜索命中原稿。原稿只用于定位，不直接替换；请修改对应候选文本。', 'info');
          } else if (result.reason === 'candidate_not_selected') {
            showMessage(`当前命中候选 ${result.candidateIndex + 1}，请先选择该候选再替换。`, 'info');
          } else {
            showMessage('当前搜索结果已变化，请重新定位后再替换。', 'info');
            refreshSearchMatches(false);
            render();
          }
          scrollToSearchHit(hit);
          return;
        }

        paragraph.candidates[result.candidateIndex] = result.text;
        paragraph.editedText = result.text;
        state.replacementHistory.push({
          from: state.searchQuery,
          to: replacement,
          scope: 'current',
          time: new Date().toISOString(),
        });
        invalidateReview();
        refreshSearchMatches(false);
        render();
        scheduleSave(0);
        showMessage('已替换当前命中的候选文本并自动保存。', 'ok');
      }

      function ensureRiskEditor() {
        let backdrop = document.getElementById('exactRiskEditorBackdrop');
        let drawer = document.getElementById('exactRiskEditor');
        if (backdrop && drawer) return {backdrop, drawer};

        backdrop = document.createElement('div');
        backdrop.id = 'exactRiskEditorBackdrop';
        Object.assign(backdrop.style, {
          position: 'fixed', inset: '0', background: 'rgba(15,23,42,.28)',
          zIndex: '80', display: 'none',
        });

        drawer = document.createElement('aside');
        drawer.id = 'exactRiskEditor';
        drawer.setAttribute('aria-label', '风险精确编辑');
        Object.assign(drawer.style, {
          position: 'fixed', top: '0', right: '0', width: 'min(620px,96vw)', height: '100vh',
          background: '#fff', zIndex: '81', boxShadow: '-24px 0 60px rgba(15,23,42,.2)',
          display: 'none', flexDirection: 'column',
        });

        const header = document.createElement('div');
        Object.assign(header.style, {
          padding: '18px 20px 14px', borderBottom: '1px solid #e5e7eb',
          display: 'flex', alignItems: 'flex-start', gap: '12px',
        });
        const heading = document.createElement('div');
        heading.style.flex = '1';
        const title = document.createElement('div');
        title.id = 'exactRiskEditorTitle';
        title.textContent = '定位修改';
        Object.assign(title.style, {fontSize: '18px', fontWeight: '760', color: '#111827'});
        const meta = document.createElement('div');
        meta.id = 'exactRiskEditorMeta';
        Object.assign(meta.style, {marginTop: '5px', fontSize: '12px', color: '#64748b'});
        heading.append(title, meta);

        const close = document.createElement('button');
        close.type = 'button';
        close.textContent = '关闭';
        close.className = 'btn ghost small';
        close.id = 'exactRiskEditorClose';
        header.append(heading, close);

        const body = document.createElement('div');
        Object.assign(body.style, {padding: '18px 20px', overflow: 'auto', flex: '1'});
        const contextLabel = document.createElement('div');
        contextLabel.textContent = '风险位置';
        Object.assign(contextLabel.style, {fontSize: '12px', fontWeight: '700', color: '#475569', marginBottom: '7px'});
        const context = document.createElement('div');
        context.id = 'exactRiskEditorContext';
        Object.assign(context.style, {
          padding: '12px 14px', border: '1px solid #fed7aa', borderRadius: '12px',
          background: '#fffaf5', fontSize: '14px', lineHeight: '1.75', color: '#334155',
          whiteSpace: 'pre-wrap', wordBreak: 'break-word', marginBottom: '16px',
        });
        const editorLabel = document.createElement('label');
        editorLabel.htmlFor = 'exactRiskEditorText';
        editorLabel.textContent = '当前最终候选文本';
        Object.assign(editorLabel.style, {display: 'block', fontSize: '12px', fontWeight: '700', color: '#475569', marginBottom: '7px'});
        const textarea = document.createElement('textarea');
        textarea.id = 'exactRiskEditorText';
        Object.assign(textarea.style, {
          width: '100%', minHeight: '320px', resize: 'vertical', lineHeight: '1.75',
          fontSize: '14px', padding: '12px 14px', border: '1px solid #cbd5e1',
          borderRadius: '12px', outline: 'none', background: '#fff', color: '#111827',
        });
        const hint = document.createElement('div');
        hint.textContent = '这里直接编辑当前实际使用的候选文本。保存后会回写原记录，并要求重新执行风险检测。';
        Object.assign(hint.style, {fontSize: '12px', color: '#64748b', marginTop: '8px', lineHeight: '1.55'});
        body.append(contextLabel, context, editorLabel, textarea, hint);

        const footer = document.createElement('div');
        Object.assign(footer.style, {
          padding: '14px 20px', borderTop: '1px solid #e5e7eb',
          display: 'flex', justifyContent: 'flex-end', gap: '8px', background: '#fff',
        });
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.textContent = '取消';
        cancel.className = 'btn ghost';
        cancel.id = 'exactRiskEditorCancel';
        const save = document.createElement('button');
        save.type = 'button';
        save.textContent = '保存修改';
        save.className = 'btn primary';
        save.id = 'exactRiskEditorSave';
        footer.append(cancel, save);

        drawer.append(header, body, footer);
        document.body.append(backdrop, drawer);
        return {backdrop, drawer};
      }

      function closeExactRiskEditor() {
        const backdrop = document.getElementById('exactRiskEditorBackdrop');
        const drawer = document.getElementById('exactRiskEditor');
        if (backdrop) backdrop.style.display = 'none';
        if (drawer) drawer.style.display = 'none';
        if (drawer) delete drawer.dataset.paragraphIndex;
        if (drawer) delete drawer.dataset.candidateIndex;
      }

      function renderRiskContext(container, text, finding) {
        container.replaceChildren();
        const context = riskContext(text, finding);
        if (context.start < 0) {
          container.textContent = String(finding?.context || finding?.phrase || text || '');
          return context;
        }
        container.append(document.createTextNode(context.before));
        const mark = document.createElement('mark');
        mark.textContent = context.phrase;
        Object.assign(mark.style, {background: '#fde68a', borderRadius: '4px', padding: '1px 2px'});
        container.append(mark, document.createTextNode(context.after));
        return context;
      }

      function openExactRiskEditor(id, pos) {
        const paragraphIndex = state.paragraphs.findIndex(paragraph => paragraph.id === id);
        if (paragraphIndex < 0) return;
        const paragraph = state.paragraphs[paragraphIndex];
        const candidateIndex = selectedCandidateIndex(paragraph);
        if (!Array.isArray(paragraph.candidates) || !paragraph.candidates.length) {
          showMessage('当前话术没有可编辑候选。', 'info');
          return;
        }

        const activeText = selectedText(paragraph);
        const findings = state.riskFindings.filter(finding => (
          finding.paragraph_id === id && finding.position === pos
        ));
        const finding = findings.find(item => (
          activeText.slice(item.position, item.position + String(item.phrase ?? '').length) === item.phrase
        )) || findings[0];
        if (!finding) {
          showMessage('该风险项已经变化，请重新执行风险检测。', 'info');
          return;
        }

        const {backdrop, drawer} = ensureRiskEditor();
        const title = document.getElementById('exactRiskEditorTitle');
        const meta = document.getElementById('exactRiskEditorMeta');
        const context = document.getElementById('exactRiskEditorContext');
        const editor = document.getElementById('exactRiskEditorText');
        const save = document.getElementById('exactRiskEditorSave');
        const close = document.getElementById('exactRiskEditorClose');
        const cancel = document.getElementById('exactRiskEditorCancel');

        title.textContent = '定位修改';
        meta.textContent = `${id} · 候选 ${candidateIndex + 1} · ${finding.label || finding.type || '风险项'}`;
        editor.value = activeText;
        drawer.dataset.paragraphIndex = String(paragraphIndex);
        drawer.dataset.candidateIndex = String(candidateIndex);
        renderRiskContext(context, activeText, finding);

        const closeHandler = () => closeExactRiskEditor();
        close.onclick = closeHandler;
        cancel.onclick = closeHandler;
        backdrop.onclick = closeHandler;
        save.onclick = () => {
          const currentParagraphIndex = Number(drawer.dataset.paragraphIndex);
          const currentCandidateIndex = Number(drawer.dataset.candidateIndex);
          const currentParagraph = state.paragraphs[currentParagraphIndex];
          if (!currentParagraph || !Array.isArray(currentParagraph.candidates)
              || currentCandidateIndex < 0 || currentCandidateIndex >= currentParagraph.candidates.length) {
            showMessage('当前记录已经变化，请重新打开定位修改。', 'info');
            closeExactRiskEditor();
            return;
          }
          const nextText = editor.value;
          currentParagraph.candidates[currentCandidateIndex] = nextText;
          if (selectedCandidateIndex(currentParagraph) === currentCandidateIndex) {
            currentParagraph.editedText = nextText;
          }
          state.replacementHistory.push({
            from: String(finding.phrase ?? ''),
            to: nextText,
            scope: 'risk_editor',
            time: new Date().toISOString(),
          });
          invalidateReview();
          refreshSearchMatches(false);
          render();
          scheduleSave(0);
          closeExactRiskEditor();
          showMessage('风险话术已保存，请重新执行风险检测确认结果。', 'ok');
        };

        if (typeof closeRisk === 'function') closeRisk();
        backdrop.style.display = 'block';
        drawer.style.display = 'flex';
        window.requestAnimationFrame(() => {
          const start = findingOffset(editor.value, finding);
          editor.focus();
          if (start >= 0) {
            editor.setSelectionRange(start, start + String(finding.phrase ?? '').length);
            window.setTimeout(() => {
              if (document.activeElement === editor) {
                editor.setSelectionRange(start, start + String(finding.phrase ?? '').length);
              }
            }, 0);
          }
        });
      }

      window.jumpToMatch = patchedJumpToMatch;
      window.replaceCurrent = patchedReplaceCurrent;
      window.locateRiskById = openExactRiskEditor;
      window.closeExactRiskEditor = closeExactRiskEditor;
      prevButton.onclick = () => patchedJumpToMatch(-1);
      nextButton.onclick = () => patchedJumpToMatch(1);
      replaceButton.onclick = patchedReplaceCurrent;
    };

    if (document.readyState === 'loading') {
      window.addEventListener('DOMContentLoaded', install, {once: true});
    } else {
      window.setTimeout(install, 0);
    }
  }

  installUiFixes();
  return {findMatches, replacementForHit, findingOffset, riskContext};
})();

if (typeof module !== 'undefined') module.exports = TextStudioSearch;
