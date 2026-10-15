# TikTok App Review — demo video shot list

TikTok wants mp4/mov, up to 5 files, 50 MB each, showing the **real** app end to end,
with the domain in the video matching the Website URL field. Every selected product and
scope must appear on screen; unused ones must be removed from the submission first.

Record the browser/app itself (Win+G on this machine, or `wsappcapture`), not slides.

| # | Shot | What proves |
| --- | --- | --- |
| 1 | Open ClipForge at the Website URL's host, or show the desktop app with the URL visible in the window | the integration lives in this app, domain matches |
| 2 | Click **Connect TikTok Account** → TikTok's consent page appears, showing the app name and the requested scopes; approve | Login Kit |
| 3 | Redirect back into the app; the connected `@handle` is shown in the modal | the OAuth callback and `user.info.basic` |
| 4 | Pick a job, show a rendered clip, press its TikTok publish action | where posting is triggered by a human |
| 5 | The upload progress / success line in the app (status polling result) | Content Posting API, `video.upload` or `video.publish` |
| 6 | Switch to TikTok (test account, sandbox) and show the clip in the **inbox** or in **Drafts**, then stop there | nothing auto-posts; the creator taps post |

Keep it under ~2 minutes, one clip only. Show no secrets: the client key/secret and any
token must not be visible on screen.

## Not done yet

This video cannot be recorded before step 5 works once for real — which is the same
blocker as the consent flow itself. Draft the submission only after one upload has
succeeded against a sandbox test user.

## The 1000-character "how each product and scope works" field

Two ready versions, both inside the limit, each describing only what the code actually does:

- `tiktok-review-notes-minimal.txt` (962 chars) — scopes `user.info.basic` + `video.upload`,
  matches `TIKTOK_POST_MODE=UPLOAD` (clip lands in the creator's inbox).
- `tiktok-review-notes-drafts.txt` (953 chars) — scopes `user.info.basic` + `video.publish`,
  matches `TIKTOK_POST_MODE=DRAFT` and needs the Direct Post grant.

Pick the one matching the scopes left checked; the text and the scope list must agree.
 Draft mode / upload mode both still need the OAuth
connection to land.
