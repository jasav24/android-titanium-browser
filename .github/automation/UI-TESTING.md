# UI testing and protected-change review

This checklist is for reviewing an upstream integration or any change that
touches protected Titanium UI. It is intentionally longer than a smoke test:
the automation can classify a hunk, but only a person can confirm that the
branding and interaction still look and behave correctly.

Nothing in this document is evidence that a test was run. Record actual
evidence for each review; do not mark a checklist item complete based on the
automation report alone.

## 1. What is protected here

| Surface | Why it matters |
| --- | --- |
| `patch.sh` sections marked `protected` | Toolbar, extension menu, popup geometry, extension settings UI, NTP behavior, omnibox, app menu, DevTools viewport. |
| `patch.sh` unlabeled preamble | Startup/branding/manifest edits before the first section header. These default to `review`. |
| `patch.sh` trailing `crbug.com` comment blocks | Not section headers, so those fixes inherit the previous section (`desktop: menu`) and are `protected`. |
| `res/**` | Launcher icon, themed icon and icon generation. |
| `fastlane/**` | Store listing, screenshots and store-facing copy. |
| `extensions/**` | Bundled-extension staging and packaging. |
| `args.gn` | Package id, Chromium features, product identity. |
| `build.sh`, `common.sh` | Patch application, signing and packaging. |
| `.github/**` | The automation and its security boundary. |

An upstream commit that changes one of these is excluded from the candidate
branch. That is deliberate. Review it as a human change, not as an automation
failure to work around.

## 2. Build and install the review artifact

Use a full build from the exact commit/tree under review. A test build from
`validate-build.yml` is signed with an ephemeral key and is suitable for UI
testing; never upload it as a release.

On the build host or an attached device:

```bash
adb install -r artifacts/*-arm64-v8a.apk
adb shell monkey -p io.github.jqssun.helium \
  -c android.intent.category.LAUNCHER 1
adb shell pidof io.github.jqssun.helium
```

The current package id is `io.github.jqssun.helium`. Confirm it with
`aapt dump badging` or `adb shell pm list packages | grep helium`; do not guess.

Record before and after evidence at the same device state:

- build commit, tree and artifact hash;
- device model, ABI, Android SDK and display size/density;
- light and dark theme;
- portrait and landscape;
- phone and tablet if the change affects adaptive layout;
- regular and incognito/OTR profiles.

A screenshot is evidence only if it shows the screen you mean. A screenshot of
the launcher does not prove that an in-app menu was tested.

## 3. Baseline and regression checks

Perform these for every candidate that requires a full build, even when no
protected path changed. They catch build-level regressions that a path
classifier cannot see.

- [ ] App installs and launches; process stays alive for at least 30 seconds.
- [ ] No crash entry for `io.github.jqssun.helium` in
      `adb logcat -b crash`.
- [ ] First-run/onboarding flow is unchanged or intentionally changed.
- [ ] New tab page, toolbar, omnibox, tab switcher and app menu render correctly.
- [ ] Address bar navigation, back/forward, reload and tab switching work.
- [ ] Downloads and file/PDF intents work.
- [ ] Light and dark themes render readable text and correct icons.
- [ ] Rotation does not lose the current tab or corrupt the toolbar.
- [ ] A second window / split-screen session opens without a crash.
- [ ] Incognito/OTR opens and closes without leaving the regular profile in an
      unexpected state.
- [ ] Uninstall/reinstall leaves no crash loop (fresh profile).

## 4. `patch.sh` section checklist

Use the section names exactly as they appear in `patch.sh`. Mark a row only when
the named surface was observed.

| Section | Protection | What to verify |
| --- | --- | --- |
| unlabeled preamble | review | Launcher/themed icon, PDF intent handling, bundled-extension staging, privacy settings entries, intent scheme guard, flag expiry, site search and desktop-Android feature overrides still behave as before. |
| `brand: display name` | protected | Launcher label, About/version surfaces, menus, settings and extension-related copy show **Flash Browser**; the external product name **Titanium Extension** is unchanged; internal `titanium` paths and the package id still work. |
| `links: stay in browser` | protected | With the GitHub, YouTube or any other verified-app-link app installed, opening that site inside Flash Browser stays in a tab and no app is launched automatically. A link tapped in another app may still open that app because Android resolves App Links before the browser. Two exemptions belong to Chromium's caller, not to this patch: a Custom Tab returning its auth result to the app that opened it, and Play Store links, still leave the browser. |
| `crbug.com` fixes after `desktop: menu` | protected (inherited) | Load unpacked from SAF, incognito back handling, tab/profile teardown, API 31 multi-profile/split-window behavior; no crash on repeated OTR open/close. |
| `accessibility` | review | With TalkBack/touch exploration on, toolbar visibility and snackbar interaction work; gestures and focus are not blocked; disabling accessibility restores the previous behavior. |
| `dev` | protected | Context menu DevTools item appears where intended; Task Manager/DevTools entries open; desktop-site context menu is unchanged; no crash when DevTools is unavailable. |
| `dev: incognito` | review | DevTools opened from an incognito tab stays in the incognito profile; from a regular tab it stays regular; no tab/profile mix-up. |
| `dev: viewport` | protected | DevTools has a mobile viewport; settings/panel headers adapt below 600 px; tabbed pane does not overflow; view-source line wrapping works. |
| `playback` | review | Audio/video playback starts, pauses and resumes; background playback behaves as intended; no regression in media controls or PiP. |
| `ext: viewport` | protected | `chrome://extensions` on a phone: cards use the narrow layout, toolbar/search fits, content padding is not clipped, scrolling works. |
| `ext: mv2` | review | A Manifest V2 extension installs and remains enabled; browser/page action metadata loads; no hard-disable warning that breaks the extension. |
| `ext: off store` | review | Install a CRX/ZIP from the extensions options page; install from Opera/Edge store domains; Chrome Web Store desktop-site allowance still works. |
| `ext: toolbar` | protected | Extension buttons appear in phone and tablet toolbars with correct spacing; the optional-button area does not clip or overlap; rotation preserves them. |
| `ext: popup` | protected | Popup anchors to the extension button or container; opening/closing does not crash; position is sensible in portrait and landscape. |
| `ext: fullscreen` | protected | On a phone, an extension popup uses the intended full-screen area rather than a tiny or clipped window; on a tablet, resizing remains correct. |
| `ext: pin` | protected | Unpinned extensions-menu button is hidden; pinned button is visible; pin/unpin updates immediately and survives restart. |
| `ext: incognito` | review | Allowed extension works in OTR; disallowed extension stays out; incognito opens as a window where configured; split-window behavior does not crash. |
| `ext: priority` | review | Extensions remain loaded and responsive while switching tabs and opening popups; no obvious jank or process kill under normal memory pressure. Process priority is not directly visible in the UI. |
| `ext: settings` | review | Extension settings/API surfaces open on Android; no unsupported-platform error; changes persist. |
| `ext: dialog` | protected | Extension install dialog still displays its warning and buttons; scrolling and tapping work; cancel and confirm behave correctly; touch filtering is not broken. |
| `ext: locale` | review | Install an unpacked extension with `_locales` from SAF; localized strings resolve; no locale validation error in the extension page or logs. |
| `ext: ntp` | protected | New-tab/home behavior respects the NTP override; no unexpected native page appears; toolbar and navigation remain usable. |
| `ext: app` | review | An app-type extension still launches where supported; no unsupported-platform error or crash. |
| `desktop: omnibox` | protected | Omnibox suggestion ordering and verbatim match on desktop-Android; site search; navigation. Compare against the pre-change build on the same device. |
| `desktop: menu` | protected | App menu shows the intended incognito/new-window entries once, in the right order; multi-window actions open the correct window type. |

If a new section is added to `patch.sh`, it must be added to
`.github/ui-protection.yml` in the same PR. The classifier falls back to
`review` for hunks before the first section header, but a new named section
without a manifest entry fails validation.

## 5. Branding and packaging checks

These paths are not `patch.sh` sections but are equally visible to users.

- [ ] Launcher icon and themed/monochrome icon are Titanium's, not stock
      Chromium's; check launcher, recents and the app info screen.
- [ ] App name and package id are correct (`io.github.jqssun.helium`).
- [ ] Splash/launch background and status/navigation bar colors are unchanged.
- [ ] `fastlane/` text and screenshots match the shipped UI; no stale Chromium
      or previous-brand screenshots.
- [ ] Bundled extension appears in `chrome://extensions` and its options page
      opens.
- [ ] `args.gn` feature changes still produce the intended product behavior;
      no protected feature silently disappears.
- [ ] `build.sh`/`common.sh` still sign and package all three expected outputs
      (arm64 APK, armv7 APK, AAB).
- [ ] A `.gclient`/`.gitmodules`/`vanadium` change is checked against the
      upstream source and pinned revision, not accepted as an opaque diff.

## 6. Manual integration of a conflicting or protected commit

Never push a conflicted automation branch. Use a dedicated human branch:

```bash
git fetch origin
git switch -c manual/upstream-<short-sha> origin/main
git cherry-pick -n <upstream-sha>
```

For each conflict:

1. Read the upstream change and the local Titanium change together. Decide which
   behavior is intended; do not simply choose "theirs" or "ours" for a UI hunk.
2. Preserve the local sed edits and branding. If upstream refactored the
   surrounding Java/C++ code, re-apply the local intent rather than pasting the
   old line blindly.
3. Check for duplicate sed expressions or contradictory feature flags.
4. Run `python .github/scripts/ui_guard.py --repo . --base origin/main --head HEAD`
   and review the hunk report.
5. Build the exact commit, install it, and complete the relevant rows above.
6. Commit with a message that identifies the upstream SHA and describes the UI
   decision.

After merging, add `Upstream-SHA: <sha>` to the review PR body (one per line)
before merging it, so the state records the commit as integrated rather than
deferred. If the commit was only partially ported, say so in the PR body; the
automation cannot infer that a subset of a commit was integrated.

## 7. Approval and evidence

A protected change is ready to merge only when:

- [ ] The diff was reviewed hunk by hunk; every protected hunk is intentional.
- [ ] A full build of the exact tree succeeded and the gate is green.
- [ ] The relevant checklist rows were tested on a real device or emulator.
- [ ] Light/dark, portrait/landscape and regular/OTR were checked where
      applicable.
- [ ] Screenshots/logs are attached to the PR or the review record.
- [ ] A maintainer added `ui-protection:approved` only after the review.

The acknowledgement label means "a maintainer reviewed these protected
changes", not "the automation may overwrite this UI". It does not excuse a
missing build, a red check, or an untested artifact.

## 8. Evidence template

Copy this into the PR or review record:

```
Build:      <commit> / tree <tree> / <artifact name and sha256>
Device:     <model>, <abi>, Android <sdk>, <width>x<height> @ <density>
Theme:      light / dark
Layout:     portrait / landscape / tablet
Profiles:   regular / incognito
Sections:   <patch.sh sections touched>
Result:     pass / fail (explain)
Evidence:   <screenshot and log paths>
Reviewer:   <name>, <date>
```

A candidate without this evidence can still be merged by a maintainer who
accepts the risk, but it must not be described as UI-tested.
