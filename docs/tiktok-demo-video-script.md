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
Both modes still need the OAuth connection to land before either sentence is true.

## How to record it

1. **Win+G** → Capture → the circle button records the current window (not the whole desktop, so
   no stray chat windows). Stop with Win+G again. The clip lands in `Videos\Captures` as mp4.
2. Record one take per shot, in the table order. ~2 minutes total is plenty.
3. Before pressing record: close anything showing a token, a key, or `.env`. Put an address bar
   (or a window title carrying the domain) in frame at least once — TikTok compares it with the
   Website URL field.
4. Keep the file under 50 MB. Too big means re-record at 720p or cut the dead time.
5. Name it something obvious, e.g. `clipforge-tiktok-demo.mp4`.
6. Only after shot 5 has succeeded once for real. A generated or staged clip is a false statement
   to the reviewer and is not acceptable here.

