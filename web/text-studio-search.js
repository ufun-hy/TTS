// Search the original text and every saved generalized candidate.
const TextStudioSearch = (() => {
  function sources(paragraph) {
    const original = typeof paragraph?.original_text === 'string'
      ? [{kind: 'original', candidateIndex: null, text: paragraph.original_text}]
      : [];
    const candidates = Array.isArray(paragraph?.candidates)
      ? paragraph.candidates
          .map((text, candidateIndex) => ({kind: 'candidate', candidateIndex, text}))
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

  return {findMatches};
})();

if (typeof module !== 'undefined') module.exports = TextStudioSearch;
