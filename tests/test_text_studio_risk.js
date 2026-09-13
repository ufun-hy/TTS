// Run: node tests/test_text_studio_risk.js
const assert = require('node:assert/strict');
const {scan} = require('../web/text-studio-risk.js');
const findings = (text, type) => scan(text).filter(f => f.type === type);

const times = [
  '现在是晚上8点', '今天是9月13日', '还有10分钟结束',
  '现在是 20：30', '现在已经是晚上八点半', '下午三点一刻',
  '今天是二〇二六年九月十三号', '今日为9月13日',
  '还剩十分钟就下播', '最后半小时活动结束', '距离结束还有10分钟',
  '10分钟后结束', '八点准时开播', '预计还有10分钟结束',
];
for (const text of times) {
  const matches = findings(text, '时间点');
  assert.equal(matches.length, 1, text);
  assert.equal(matches[0].severity, 'medium', text);
}
const commitments = [
  '坏果包赔', '不满意就赔', '假一赔十', '无条件赔', '假一赔100',
  '烂果直接赔付', '破损包赔', '有问题就赔', '全额赔偿', '直接赔付',
  '我们会赔付', '我们负责赔偿', '坏果包赔，具体规则以页面为准',
  '不包赔，但不满意就赔',
];
for (const text of commitments) {
  const matches = findings(text, '售后赔付');
  assert.equal(matches.length, 1, text);
  assert.equal(matches[0].severity, 'high', text);
}
for (const text of [
  '现在大家看看这个产品', '今天给大家介绍产品', '现在只要8元',
  '这个产品有8点优势', '生产日期是9月13日', '保质期还有10分钟',
  '使用10分钟后清洗', '每天使用两小时', '有问题联系客服处理售后',
  '不包赔', '不赔付', '不承诺无条件赔', '并非假一赔十',
]) {
  assert.equal(scan(text).length, 0, text);
}
for (const text of ['赔付以平台规则为准', '符合条件可申请赔付', '赔偿流程请咨询客服']) {
  assert.equal(findings(text, '售后赔付')[0].severity, 'low', text);
}
for (const [text, type, severity] of [
  ['48小时内发货', '物流', 'high'], ['预计48小时内发货', '物流', 'medium'],
  ['运费险以页面显示为准', '运费险', 'low'], ['七天无理由', '售后', 'high'],
  ['无条件退款', '售后', 'high'], ['百分百', '承诺', 'high'],
  ['全网最低', '夸大', 'medium'], ['立刻见效', '效果', 'high'],
]) assert.equal(findings(text, type)[0].severity, severity, text);

const multi = '🍎欢迎光临。\n  现在是晚上8点，坏果包赔。\n今天是9月13日。还有10分钟结束。不满意就赔，无条件赔，假一赔十。';
const first = scan(multi);
assert.equal(first.length, 7);
assert.deepEqual(scan(multi), first, 'global regex state leaked between scans');
for (const hit of first) {
  assert.equal(multi.slice(hit.position, hit.position + hit.phrase.length), hit.phrase);
  assert.ok(hit.context.includes(hit.phrase));
  assert.ok(hit.reason && hit.suggestion);
  assert.equal(hit.ignored, false);
}
assert.deepEqual(scan(''), []);
console.log('Text Studio risk checks passed: positive, negative, severity, offsets and existing rules.');
