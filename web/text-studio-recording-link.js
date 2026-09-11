(() => {
  if (window.__ttsRecordingTranscriptLinkLoaded) return;
  window.__ttsRecordingTranscriptLinkLoaded = true;

  const modeTabs = document.querySelector('.mode-tabs');
  if (!modeTabs || document.getElementById('recordingTranscriptLink')) return;

  const link = document.createElement('a');
  link.id = 'recordingTranscriptLink';
  link.className = 'mode-tab';
  link.textContent = '录音转文稿';
  link.href = `http://${window.location.hostname || '127.0.0.1'}:8771/`;
  link.target = '_blank';
  link.rel = 'noopener';
  link.title = '打开独立的录音转文稿页面';
  link.style.textDecoration = 'none';
  link.style.display = 'inline-flex';
  link.style.alignItems = 'center';

  modeTabs.appendChild(link);
})();
