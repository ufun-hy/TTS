// Run: node tests/test_text_studio_search.js
const assert = require('node:assert/strict');
const {findMatches} = require('../web/text-studio-search.js');

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
console.log('Text Studio search checks passed: original text, all candidates, dynamic count and misses.');
