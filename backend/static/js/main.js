/**
 * main.js - the dashboard's entry point.
 *
 * Importing the modules above is what registers their listeners; these calls
 * are the part of app.js that ran work, in the same order.
 */

import { setupBatchUpload } from './batch.js';
import { listJobs, loadSystemInfo } from './jobs.js';
import { state } from './state.js';
import { setupStudioModal } from './studio.js';
import { checkTikTokStatus, setupTikTokIntegration } from './tiktok.js';
import { checkYouTubeStatus, setupYouTubeIntegration } from './youtube.js';

setupStudioModal();
setupYouTubeIntegration();
setupTikTokIntegration();
setupBatchUpload();
checkYouTubeStatus();
checkTikTokStatus();
loadSystemInfo();
listJobs();

// Periodic live-sync for running jobs
setInterval(() => {
  const hasRunningJob = (state.jobs || []).some(j => j.status === 'processing' || j.status === 'created');
  if (hasRunningJob) {
    listJobs();
  }
}, 2500);

