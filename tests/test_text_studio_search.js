// Run: node tests/test_text_studio_search.js
const assert = require('node:assert/strict');
const {findMatches, replacementForHit, findingOffset, riskContext} = require('../web/text-studio-search.js');

const paragraphs = [{
  id: 'p0001',
  original_text: '原文可以搜索到',
  candidates: ['泛化一可以搜索到', '泛化二可以搜索到', '泛化三可以搜索到'],
}];

for (const [query, kind, candidateIndex] of [
  ['原文可以', 'original', null],
  ['泛化一可以', 'candidate', 0],
  ['泛化二可以', 'candidate', 1],
  ['泛化三可以', 'candidate', 2],
]) {
  assert.deepEqual(findMatches(paragraphs, query), [{paragraphIndex: 0, start: 0, kind, candidateIndex}], query);
}

const dynamic = [{
  original_text: '只有原文',
  candidates: Array.from({length: 8}, (_, index) => `候选${index + 1}`),
}];
assert.equal(findMatches(dynamic, '候选8').length, 1);
assert.equal(findMatches(dynamic, '不存在').length, 0);
assert.equal(findMatches([{original_text: '未泛化也可搜索'}], '未泛化').length, 1);

// Candidate hits use the same trimmed coordinate system as selectedText().
assert.deepEqual(
  findMatches([{original_text: '原文', candidates: ['  前导空白关键词  ']}], '前导空白'),
  [{paragraphIndex: 0, start: 0, kind: 'candidate', candidateIndex: 0}],
);

const replacementParagraph = {
  original_text: '原稿也有关键词',
  selectedIndex: 0,
  candidates: ['关键词在候选一', '普通候选', '关键词在候选三'],
};

assert.deepEqual(
  replacementForHit(
    replacementParagraph,
    {paragraphIndex: 0, start: 0, kind: 'candidate', candidateIndex: 2},
    '关键词',
    '替换词',
  ),
  {ok: false, reason: 'candidate_not_selected', candidateIndex: 2},
  'unselected candidate must never be modified by replace-current',
);

assert.deepEqual(
  replacementForHit(
    replacementParagraph,
    {paragraphIndex: 0, start: 0, kind: 'original', candidateIndex: null},
    '关键词',
    '替换词',
  ),
  {ok: false, reason: 'read_only_source'},
  'original text is searchable but read-only for replace-current',
);

assert.deepEqual(
  replacementForHit(
    replacementParagraph,
    {paragraphIndex: 0, start: 0, kind: 'candidate', candidateIndex: 0},
    '关键词',
    '替换词',
  ),
  {ok: true, candidateIndex: 0, start: 0, text: '替换词在候选一'},
  'selected candidate replacement must target the exact search hit',
);

assert.equal(
  replacementForHit(
    replacementParagraph,
    {paragraphIndex: 0, start: 3, kind: 'candidate', candidateIndex: 0},
    '关键词',
    '替换词',
  ).reason,
  'stale_match',
  'stale offsets must not fall back to another occurrence',
);

const repeatedRiskText = '风险 正常 风险 结尾';
assert.equal(findingOffset(repeatedRiskText, {position: 6, phrase: '风险'}), 6);
assert.equal(findingOffset(repeatedRiskText, {position: 5, phrase: '风险'}), 6);
assert.equal(findingOffset('没有目标', {position: 0, phrase: '风险'}), -1);
assert.deepEqual(
  riskContext(repeatedRiskText, {position: 6, phrase: '风险'}, 4),
  {before: '…正常 ', phrase: '风险', after: ' 结尾', start: 6},
  'exact risk editor context must highlight the same occurrence used by selection',
);

console.log('Text Studio search checks passed: search scope, exact replacement target, trim offsets and exact risk-editor context.');
