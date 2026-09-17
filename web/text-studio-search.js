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

      function patchedLocateRiskById(id, pos) {
        const paragraphIndex = state.paragraphs.findIndex(paragraph => paragraph.id === id);
        if (paragraphIndex < 0) return;
        const paragraphState = state.paragraphs[paragraphIndex];
        const activeText = selectedText(paragraphState);
        const findings = state.riskFindings.filter(finding => (
          finding.paragraph_id === id && finding.position === pos
        ));
        const finding = findings.find(item => (
          activeText.slice(item.position, item.position + String(item.phrase ?? '').length) === item.phrase
        )) || findings[0];

        state.riskFocusParagraph = paragraphIndex;
        state.searchFocusParagraph = -1;
        paragraphState.expanded = true;
        closeRisk();
        switchView('prepare');
        render();

        window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
          const paragraph = document.getElementById(`para-${paragraphIndex}`);
          const editor = paragraph?.querySelector('textarea.candidate');
          if (!editor) {
            paragraph?.scrollIntoView({behavior: 'smooth', block: 'center'});
            return;
          }

          // The editable textarea is the true target. Scrolling the entire card can
          // leave the editor off-screen for tall expanded paragraphs.
          editor.scrollIntoView({behavior: 'smooth', block: 'center'});
          if (!finding) return;
          const start = findingOffset(editor.value, finding);
          if (start < 0) return;
          editor.focus({preventScroll: true});
          editor.setSelectionRange(start, start + String(finding.phrase ?? '').length);
        }));
      }

      window.jumpToMatch = patchedJumpToMatch;
      window.replaceCurrent = patchedReplaceCurrent;
      window.locateRiskById = patchedLocateRiskById;
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
  return {findMatches, replacementForHit, findingOffset};
})();

if (typeof module !== 'undefined') module.exports = TextStudioSearch;
