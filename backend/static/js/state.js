/**
 * state.js - part of the job dashboard, split out of app.js.
 *
 * The single mutable store plus the element cache. Built once, at
 * import time: this module is the only one with no imports, so every
 * other module can read it while it is being evaluated.
 */


export const state = {
  jobs: [],
  activeJobId: null,
  activeJob: null,
  jobTimers: {}
};
window.state = state;

export const els = {
  form: document.getElementById('jobForm'),
  videoUrl: document.getElementById('videoUrl'),
  urlMetaBadge: document.getElementById('urlMetaBadge'),
  clipCountInput: document.getElementById('clipCount'),
  provider: document.getElementById('provider'),
  captionStyle: document.getElementById('captionStyle'),
  layoutMode: document.getElementById('layoutMode'),
  whisperModel: document.getElementById('whisperModel'),
  hardwareInfo: document.getElementById('hardwareInfo'),
  transcriptionEstimate: document.getElementById('transcriptionEstimate'),
  status: document.getElementById('generatorStatus'),
  jobsList: document.getElementById('jobsList'),
  clipsGrid: document.getElementById('clipsGrid'),
  jobCount: document.getElementById('jobCount'),
  statsClipCount: document.getElementById('statsClipCount'),
  readyCount: document.getElementById('readyCount'),
  refreshJobsBtn: document.getElementById('refreshJobsBtn'),
  focusComposerBtn: document.getElementById('focusComposerBtn'),
  sourceTabUrl: document.getElementById('sourceTabUrl'),
  sourceTabUpload: document.getElementById('sourceTabUpload'),
  sourceUrlPanel: document.getElementById('sourceUrlPanel'),
  sourceUploadPanel: document.getElementById('sourceUploadPanel'),
  videoFile: document.getElementById('videoFile'),
  uploadDropZone: document.getElementById('uploadDropZone'),
  uploadDropContent: document.getElementById('uploadDropContent'),
  uploadFileInfo: document.getElementById('uploadFileInfo'),
  uploadFileName: document.getElementById('uploadFileName'),
  uploadFileSize: document.getElementById('uploadFileSize'),
  uploadRemoveBtn: document.getElementById('uploadRemoveBtn'),
  activeJobProgressPanel: document.getElementById('activeJobProgressPanel'),
  activeJobIdBadge: document.getElementById('activeJobIdBadge'),
  activeJobPercentText: document.getElementById('activeJobPercentText'),
  activeJobStageLabel: document.getElementById('activeJobStageLabel'),
  activeJobClipsCount: document.getElementById('activeJobClipsCount'),
  activeJobProgressBar: document.getElementById('activeJobProgressBar'),
  stepDownload: document.getElementById('stepDownload'),
  stepAudio: document.getElementById('stepAudio'),
  stepTranscribe: document.getElementById('stepTranscribe'),
  stepAI: document.getElementById('stepAI'),
  stepRender: document.getElementById('stepRender')
};
