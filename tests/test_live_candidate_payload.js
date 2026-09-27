const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const state = {contextGroups: null, paragraphs: [
  {id: 'p1', candidates: ['A1', 'A2', 'A3'], selectedIndex: 1, editedText: 'A2 edited', prohibited_reviews: []},
  {id: 'p2', candidates: ['B1', 'B2', 'B3'], selectedIndex: 1, prohibited_reviews: []},
  {id: 'p3', candidates: ['免费试吃。', '正常介绍。'], selectedIndex: 0, prohibited_reviews: []},
  {id: 'p4', candidates: ['免费试吃。'], selectedIndex: 0, prohibited_reviews: []},
]};
const context = vm.createContext({state,
  selectedText: p => String(p.editedText || p.candidates[p.selectedIndex || 0]).trim(),
  TextStudioVariants: {groupFor: () => null},
  TextStudioProhibited: require('../web/text-studio-prohibited.js'),
});
vm.runInContext(fs.readFileSync('web/text-studio-reviews.js', 'utf8'), context);
const payload = JSON.parse(vm.runInContext('JSON.stringify(TextStudioReviews.liveSegments())', context));
assert.deepEqual(payload.map(p => p.id), ['p1', 'p2', 'p3']);
assert.deepEqual(payload[0].candidates, ['A1', 'A2 edited', 'A3']);
assert.deepEqual(payload[1].candidates, ['B1', 'B2', 'B3']);
assert.equal(payload[2].candidates.length, 2); // Server filters by original candidate index.
const review = {candidate_index: 2, candidate_text: 'A3', start: 0, text: 'A3', labels: ['test'], rules_version: 1};
state.paragraphs[0].prohibited_reviews = [review];
assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(TextStudioReviews.liveSegments()[0].prohibited_reviews)', context)), [review]);
console.log('Live sends all candidates with original review indexes');
