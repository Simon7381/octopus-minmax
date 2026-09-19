## v2.0.4 - v2.0.4
## What's Changed
* Stabilize the persistent browser with seed 101 and desktop hardware pins: 24 logical processors, 3840×1600 screen and DPR 1.00. Replace the old 1920×1080 viewport with 1902×1398 for both new and reused pages. Use the supported AMD Radeon GPU identity because the library has no RX 6900 XT profile.
* Attach `logs/octobot.log` and PNG screenshots to Apprise error notifications when `DEBUG=true`, including startup login failures and batched errors. Skip missing or unreadable files and upload diagnostics once per notification or batch.
* Apply Ubuntu package updates during every container build, then clean apt metadata.
* Remove unused GStreamer Bad codecs affected by CVE-2025-3887; the application continues to use Invisible Playwright's Firefox.
* Remove runtime pip, virtualenv seed wheels, installer caches and ensurepip payloads after dependency installation to eliminate their vulnerable bundled components. Rebuild the image to update dependencies.
* Retain before-and-after Trivy reports under `reports/security/v2.0.4` and exclude those reports from the image build context.

## Validation
* Local amd64 Trivy 0.74.0 scan: High findings reduced from 4 to 0; Critical findings remain 0. The final scan still reports 535 Medium and 52 Low findings; none were suppressed.
* All 66 application tests passed, including Firefox fixtures. Verified that vulnerable codecs and installer copies are absent from the final image.
* After adding debug error attachments, all 74 tests passed with the updated source mounted into the local Podman image. Attachment tests use synthetic files and mocked delivery; no external notifications were sent.
* After adding desktop hardware pins, all 75 tests passed in local Podman with the updated source. Real Firefox checks verify viewport, screen, DPR, logical CPU count, AMD renderer, locale and timezone across two persistent launches without contacting Octopus.

**Full Changelog**: https://github.com/Simon7381/octopus-minmax/compare/v2.0.3...v2.0.4

## v2.0.3 - v2.0.3
## What's Changed
* Use the Microsoft Playwright Ubuntu 24.04 (Noble) base image in production, retaining Python 3.14 and Invisible Playwright's Firefox. Browser fonts and system libraries come from the Microsoft image.
* Resolve the newest stable Noble image on every release or helper-driven local build. Match the standard Playwright package to its bundled browsers, refresh other dependencies, and pull current Python 3.14 patch images.
* Run the offline test suite before publishing Docker Hub images for amd64 and arm64. Add a shared local build helper and coverage for stable Noble tag selection.
* Extract shared website login and account verification, and warm the persistent browser session after the startup mode and dashboard notification.
* Send immediate login progress and completion notifications, masking the account number except for its last three characters. Missing credentials skip login; login failures are reported while comparisons continue.
* Generate release notes when a published release has no description, while preserving substantive existing changelog entries.

## Validation
* The final production image passed all 66 offline tests on amd64 with Python 3.14.7, Invisible Playwright 0.22.2 and Playwright 1.63.0. Startup verified the saved account, logged its masked confirmation and served the authenticated dashboard with HTTP 200.
* The Microsoft-based image passed fresh Octopus login, account verification and cookie reuse in a new container during local Podman testing. No tariff changes or external notifications were sent.

**Full Changelog**: https://github.com/Simon7381/octopus-minmax/compare/v2.0.2...v2.0.3

## v2.0.2 - v2.0.2
## What's Changed
* Initialise application logging at startup and add a `DEBUG` option to Home Assistant, Docker configuration and the web dashboard. Important events remain visible at INFO when debug logging is disabled.
* Add browser stage diagnostics, API status codes, enrolment polling attempts and exception stack locations while omitting raw API bodies, token values and credential-bearing Playwright call logs.
* Catch Invisible Playwright's own exception class so browser failures report the failed stage and operation, attempt a failure screenshot and warn when a submitted switch needs checking before retrying.
* Log notification contents immediately, including when notifications are batched, and add regression tests for logging and browser error handling.
* Improve release automation validation and preserve existing changelog notes when updating add-on metadata.

**Full Changelog**: https://github.com/Simon7381/octopus-minmax/compare/v2.0.1...v2.0.2

## v2.0.1 - v2.0.1
## What's Changed
* Update Docker Hub image references to `simon7381/octopus-minmax` for this fork.
* Update installation documentation.

**Full Changelog**: https://github.com/Simon7381/octopus-minmax/compare/v2.0.0...v2.0.1

## v2.0.0 - v2.0.0
## What's Changed
* Add an invisible-playwright website fallback for Go, Agile and Cosy tariff switches when API initiation fails, including immediate fallback for permission error `KT-CT-1111`. Fixed Go remains API-only.
* Add Octopus website login credentials to the add-on options and dashboard, preserving saved passwords when the dashboard password field is left blank.
* Verify the signed-in account, reuse matching pending enrolments, and confirm new enrolments for the exact target product to avoid duplicate or incorrect switches. Skip API agreement acceptance when the website has already completed it.
* Add `TEST_PLAYWRIGHT` mode to check website login and signup forms without submitting tariff switches, with per-tariff results and failure screenshots. Interactive CAPTCHA challenges stop browser automation.
* Bundle invisible-playwright's patched Firefox and browser dependencies in the Python 3.12 container, with updated Docker and Podman setup instructions and automated browser, switching and configuration tests.

## Breaking Changes
* Container and Home Assistant add-on support is now limited to `amd64` and `aarch64` (64-bit); 32-bit ARM targets are no longer supported.

**Full Changelog**: https://github.com/Simon7381/octopus-minmax/compare/v1.0.9...v2.0.0

## v1.0.9 - v1.0.9
## What's Changed
* Update Addon Configuration to v1.0.8 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/163
* Incorrect port number in readme.md by @Morph-Ed in https://github.com/eelmafia/octopus-minmax/pull/165
* Add new tariff option by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/177

## New Contributors
* @Morph-Ed made their first contribution in https://github.com/eelmafia/octopus-minmax/pull/165

**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.8...v1.0.9

## v1.0.8 - v1.0.8
## What's Changed
* Update Addon Configuration to v1.0.7 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/159
* Update SWITCH_THRESHOLD type to int in config.yaml (fixes #160) by @DJBenson in https://github.com/eelmafia/octopus-minmax/pull/161
* Log notifications regardless regardless if apprise exists by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/162


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.7...v1.0.8

## v1.0.7 - v1.0.7
## What's Changed
* Update Addon Configuration to v1.0.6 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/157
* Fix indents by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/158


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.6...v1.0.7

## v1.0.6 - v1.0.6
## What's Changed
* Update Addon Configuration to v1.0.5 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/148
* Restore legacy:true to config.yaml (fixes #141) by @DJBenson in https://github.com/eelmafia/octopus-minmax/pull/154
* Update notification_service.py by @DJBenson in https://github.com/eelmafia/octopus-minmax/pull/156


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.5...v1.0.6

## v1.0.5 - v1.0.5
## What's Changed
* Update Addon Configuration to v1.0.4 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/146
* Ingress fixes by @DJBenson in https://github.com/eelmafia/octopus-minmax/pull/147


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.4...v1.0.5

## v1.0.4 - v1.0.4
## What's Changed
* Update Addon Configuration to v1.0.3 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/143
* Add missing authentication fields by @DJBenson in https://github.com/eelmafia/octopus-minmax/pull/145


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.3...v1.0.4

## v1.0.3 - v1.0.3
## What's Changed
* Update Addon Configuration to v1.0.1 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/138
* Update Addon Configuration to v1.0.2 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/139
* Fix web dashboard paths in HA by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/142


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.2...v1.0.3

## v1.0.2 - v1.0.2
**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.1...v1.0.2
## v1.0.1 - v1.0.1
## What's Changed
* Update Addon Configuration to v1.0.0 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/136
* Fix path in workflow script by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/137


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v1.0.0...v1.0.1

## v1.0.0 - v1.0.0
## What's Changed
* Refactor code and add web UI by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/128


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.8.5...v1.0.0

## v0.8.5 - v0.8.5
## What's Changed
* Improve cosy regex to avoid fixed tariff by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/119


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.8.4...v0.8.5

## v0.8.4 - v0.8.4
## What's Changed
* Update Addon Configuration to v0.8.3 by @github-actions[bot] in https://github.com/eelmafia/octopus-minmax/pull/112
* Fix switch threshold type by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/115


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.8.3...v0.8.4

## v0.8.3 - v0.8.3
## What's Changed
* Add config entry/environment variable for switch threshold, defaults to 2p by @DJBenson in https://github.com/eelmafia/octopus-minmax/pull/111

## New Contributors
* @DJBenson made their first contribution in https://github.com/eelmafia/octopus-minmax/pull/111

**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.8.2...v0.8.3

## v0.8.2 - v0.8.2
## What's Changed
* Update tariff.py by @adyoull in https://github.com/eelmafia/octopus-minmax/pull/99


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.8.1...v0.8.2

## v0.8.1 - v0.8.1
## What's Changed
* Replace gql with requests by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/88

## New Contributors

**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.8.0...v0.8.1

## v0.8.0 - v0.8.0
## What's Changed
* Update README.md by @adyoull in https://github.com/eelmafia/octopus-minmax/pull/75
* Add HA Addon Config by @joeShuff in https://github.com/eelmafia/octopus-minmax/pull/76
* Add option to batch notifications by @lilongwe in https://github.com/eelmafia/octopus-minmax/pull/77
* Fix timeout errors by @eelmafia in https://github.com/eelmafia/octopus-minmax/pull/84


**Full Changelog**: https://github.com/eelmafia/octopus-minmax/compare/v0.7.2...v0.8.0

## Initial Release

This is the initial release of Octopus MinMax Bot
