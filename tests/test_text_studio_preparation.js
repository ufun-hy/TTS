const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = Object.fromEntries(['audioPreparationStatus', 'audioPreparationHint', 'audioPreparationStart',
  'audioPreparationPause', 'prepareWhileGeneralizing'].map(id => [id, {checked: true, textContent: '', disabled: false}]));
const state = {projectId: 'project-0001', paragraphs: [{id: 'p1'}]};
const calls = [];
const context = vm.createContext({state, $: id => nodes[id],
  queueSave: async () => {calls.push('save');},
  api: async (path, body) => {
    calls.push(path);
    return {project_id: body.project_id, status: path.endsWith('/pause') ? 'paused' : 'waiting_text',
      enabled: !path.endsWith('/pause'), prepared_segments: 2, total_segments: 3};
  },
  apiGet: async () => ({project_id: state.projectId, status: 'idle'}),
  setInterval: () => 1,
});
vm.runInContext(fs.readFileSync('web/text-studio-preparation.js', 'utf8'), context);
(async () => {
  await vm.runInContext('TextStudioPreparation.beforeGeneralize()', context);
  assert.deepEqual(calls, ['save', '/api/preparation/start']);
  assert.ok(nodes.audioPreparationStatus.textContent.includes('2/3'));
  assert.equal(nodes.audioPreparationPause.disabled, false);
  await vm.runInContext('TextStudioPreparation.pause()', context);
  assert.equal(nodes.audioPreparationStart.textContent, '继续准备音频');
  vm.runInContext("TextStudioPreparation.render({project_id:'project-0001',status:'failed',enabled:true,error:'TTS failed'})", context);
  assert.equal(nodes.audioPreparationStart.disabled, false);
  const before = nodes.audioPreparationStatus.textContent;
  vm.runInContext("TextStudioPreparation.render({project_id:'old-project',status:'ready'})", context);
  assert.equal(nodes.audioPreparationStatus.textContent, before);
  nodes.prepareWhileGeneralizing.checked = false;
  calls.length = 0;
  await vm.runInContext('TextStudioPreparation.beforeGeneralize()', context);
  assert.deepEqual(calls, []);
  context.api = async () => {throw new Error('TTS unavailable');};
  nodes.prepareWhileGeneralizing.checked = true;
  await vm.runInContext('TextStudioPreparation.beforeGeneralize()', context);
  assert.ok(nodes.audioPreparationStatus.textContent.includes('TTS unavailable'));
  const html = fs.readFileSync('web/text-studio.html', 'utf8');
  assert.ok(html.includes('await TextStudioPreparation.beforeGeneralize();for(let offset='));
  assert.ok(html.includes("project_id:state.projectId,...TextStudioVariants.projectFields()"));
  console.log('Preparation UI: async start, pause, opt-out, stale response and error handling passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
