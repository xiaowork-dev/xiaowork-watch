final result: passed

# v0.6.1 Compact service directory — design QA

Date: 2026-10-05. Scope: the selected second design applied to the existing monitoring frontend, including public and administrator views, website/VPS/latency-target lists, existing details and forms, and light/dark/system themes.

## Source and comparison

- Authoritative source: `docs/design/v0.6.1/selected-reference.png`, the second displayed ideation result (Compact service directory). The supplied VPS screenshot grounded the exploration; it was not substituted for the user's selected option.
- Source native size: 1724 × 912. Uniformly normalized to 1440 × 762 for a same-viewport desktop comparison; the browser capture is 1440 × 762 at one screenshot pixel per CSS pixel.
- Source and implementation were opened together in `day-final-comparison.png`. The toolbar and row details were separately inspected in `day-final-toolbar.png` and `day-final-rows.png`.
- The final main table at this viewport is x=248, y=92, width=1164, height=508. The implementation preserves the 64px full-width header, 220px sidebar, 28px main gutter, internal toolbar, thin table border, alternating rows, and restrained monochrome actions.
- No photographic or illustrative assets are present in the selected UI. The editable text wordmark and existing library icons remain native frontend elements. The terminal action uses the closest boxed terminal icon.

## Findings resolved

| Priority | Finding | Correction | Evidence |
| --- | --- | --- | --- |
| P2 | Desktop names, addresses, and metadata were too small relative to the selected reference. | Increased name/address/region/header/footer typography and adjusted row text hierarchy. | `day-final-comparison.png`, `day-final-rows.png` |
| P2 | The operation heading did not align with the start of the action controls. | Aligned the last fleet heading and row actions to the left. | `day-final-toolbar.png`, `day-final-rows.png` |
| P2 | Adjacent target/heartbeat headings were cramped at tablet widths. | Adjusted column proportions, tablet header size, and configuration wrapping for 761–1100px. | `tablet-public-night-final.jpg` |
| P2 | Target search omitted the port from a displayed TCP endpoint. | Added the existing formatted endpoint to searchable fields, including bracketed IPv6 with a port. | Full TCP endpoint and `formatEndpoint` verification |

No unresolved P0, P1, or P2 findings remain. Minor native font rendering differences and a few pixels of vertical geometry are P3 follow-up polish only.

## Browser verification

All browser checks used the existing local app through the Codex in-app browser. The app was connected to an isolated backend and disposable SQLite test database; illustrative addresses and measurement fixtures exist only in local QA. They are not production defaults or release contents.

- Desktop administrator VPS list at 1440 × 762: online, offline, and pending installation rows; status summary; search; refresh; actions; and source comparison passed.
- Search accepts case differences and surrounding whitespace, matches VPS region and website address, preserves filtering after refresh, and offers a working clear action when no results match. Summary counts continue to describe the complete list.
- Administrator login, VPS edit/save, enable/disable, and installation guide passed against the existing API. A new disposable VPS was saved and automatically opened its installation guide, then removed from the disposable database. Delete confirmation was opened and cancelled.
- VPS name opens the existing route and actual detail response. TCP connection results, ICMP packet loss, failed measurements, and historical records remain distinct.
- Website names open the existing detection history directly. Public and administrator detail views retain a working refresh action; administrator manual checks and editing remain available.
- Latency-target search and the TCP/ICMP add form passed. Switching protocol reveals or hides the TCP port appropriately.
- Public website, VPS, and latency-target routes are accessible without login. Administrator mutation controls are hidden; public navigation and details remain usable. The backend authorization boundary is unchanged.
- Empty website, VPS, and latency-target lists display the appropriate empty state with zero counts. Installation defaults remain empty.
- Dark mode, light mode, and system selection render correctly. Theme preference survives a reload. Existing automated theme tests cover system changes, cross-tab updates, and inaccessible storage.
- Responsive checks passed at 320 × 800, 390 × 844, and 800 × 900, with no document-level horizontal overflow. Mobile forms, installation instructions, details, login, navigation, and confirmation dialogs remain usable.
- Responsive sizes above are the requested viewport overrides. Native in-app screenshot dimensions reflect available content and scrollbars: small view 305 × 756; phone lists 375 × 763; phone dialogs 390 × 793; tablet views 800 × 793. Desktop comparison captures are exactly 1440 × 762.
- Final browser warning/error logs: none.

## Evidence

- `docs/design/v0.6.1/day-final.jpg`: source-equivalent desktop administrator view.
- `docs/design/v0.6.1/day-final-comparison.png`, `day-final-toolbar.png`, `day-final-rows.png`: full and focused reference comparisons.
- `docs/design/v0.6.1/night-final.jpg`: desktop administrator dark theme.
- `docs/design/v0.6.1/mobile-night.jpg`, `edit-mobile-night.jpg`, `target-form-mobile-night.jpg`, `login-mobile-night.jpg`: mobile dark list, edit, TCP form, and empty-credential login.
- `docs/design/v0.6.1/mobile-public-day.jpg`, `small-public-night.jpg`, `tablet-public-night-final.jpg`: public phone, small-screen, and tablet views.
- `docs/design/v0.6.1/empty-public-day.jpg`: empty installation state.
- `docs/design/v0.6.1/public-web-day.jpg`, `public-vps-day.jpg`: desktop public views.
- `docs/design/v0.6.1/target-endpoint-search.jpg`: case-insensitive full TCP endpoint search.

## Code and release scope

The existing 25 frontend, 15 theme, and 7 selection checks passed (47 total). Vue single-file component compilation and CSS parsing passed. The production build and Git whitespace check passed. Release delivery also runs the existing Linux backend, agent, updater, installer, and Node 22 build checks.

This version changes the frontend and its documentation only. The production backend, agents, installation/update system, API authorization, and data schema (2) are unchanged. The source design shows three row actions; the implementation retains the existing fourth enable/disable action. A concise explanatory line preserves the heartbeat/offline and VPS-to-target measurement semantics.

QA screenshots contain local illustrative records. Database files, probe tokens, credentials, and test fixture scripts are excluded from the repository and installation package.
