const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/text-studio.html', 'utf8');
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  new vm.Script(match[1]);
}
const source = html.slice(html.indexOf('const liveLabels='), html.indexOf('async function refreshLiveStatus'));
const ids = [...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]);
const elements = Object.fromEntries(ids.map(id => [id, {textContent: '', disabled: false, hidden: false}]));
const context = vm.createContext({
  state: {liveTimer: null},
  $: id => {assert.ok(elements[id], `missing element ${id}`); return elements[id];},
  broadcastReady: () => true,
  clearInterval: () => {},
});
vm.runInContext(source, context);
const render = context.renderLiveStatus;
const preparing = {status: 'starting', phase: 'preparing', prepared_segments: 4,
  preparation_total_segments: 9, prepared_rounds: 0, preparation_total_rounds: 1};
render(preparing);
assert.equal(elements.liveStatus.textContent, '正在准备音频');
assert.equal(elements.liveRound.textContent, '待开播');
assert.equal(elements.livePreparation.textContent, '4/9 段 · 0/1 轮');
assert.equal(elements.livePreparationMetric.hidden, false);
assert.equal(elements.liveStartBtn.disabled, true);
assert.equal(elements.livePauseBtn.disabled, false);
render({...preparing, status: 'paused'});
assert.equal(elements.livePauseBtn.textContent, '继续');
assert.ok(elements.liveHint.textContent.includes('准备已暂停'));
render({...preparing, status: 'running', phase: 'playing', round_number: 4, prepared_round_number: 1});
assert.equal(elements.liveGeneratedLabel.textContent, '本轮已发送');
assert.equal(elements.liveRound.textContent, '4（方案 1/1）');
assert.ok(elements.liveHint.textContent.includes('持续随机选择候选'));
render({...preparing, status: 'failed', error: '准备失败'});
assert.equal(elements.liveHint.textContent, '准备失败');
assert.equal(elements.liveStopBtn.disabled, true);
render({status: 'idle'});
assert.equal(elements.livePreparationMetric.hidden, true);
assert.equal(elements.liveStartBtn.disabled, false);
console.log('prepared live UI states passed');
