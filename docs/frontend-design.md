# Frontend design and maintenance

## Visual direction

The interface deliberately returns to the earlier DA Document Generator visual language. A white full-width header anchors a centred workspace. Numbered sections, thin blue-gray rules, square outlined fields, a horizontal progress tracker, a compact run log and broad output actions reproduce the familiar structure without reviving the old one-pass backend.

The saved-workflow library is an on-demand drawer on every screen size. This preserves the wide configuration and graph surfaces while keeping durable drafts, recovery actions and settings one click away. Ordinary panels avoid gradients, blur and ornamental shadows. Typography uses the operating system's fonts and icons come from the local SVG library.

## Colour themes

Choose **App settings → Appearance → Accent theme**.

| Theme | Accent | Soft selection background |
| --- | --- | --- |
| Blue — default | `#0b5cff` | `#eef3ff` |
| Violet | `#7047c4` | `#f3effc` |
| Graphite | `#414957` | `#edf0f4` |

- `styles.css` owns the palette through CSS custom properties. Buttons, progress stages, focus rings, icons and selected diagram arrows share those tokens.
- Success, warning and error colours stay consistent across themes. Text, dashed arrows and badges continue to communicate their meaning without relying solely on colour.
- `applyAppearance` in `app.js` accepts only the three supported names. The theme is stored separately from drafts and pending requests, under `appearance.theme` in the existing browser-storage namespace.
- Selection applies immediately, survives reloads in the same browser profile/address and synchronizes across tabs. A blocked storage write leaves the selected appearance active for the current page and explains that it cannot be saved.
- The preference affects the application interface. It does not alter graph data, enable AI, submit jobs or restyle previously generated standalone reports.

## Icons and code comments

All interface icons are local SVG symbols in `index.html`, reused through `<use>` references. Decorative icons are hidden from assistive technology; their containing buttons, fields and tabs keep text or accessible labels. No remote icon font, script, image service or build dependency was added.

The HTML is expanded into readable nested markup. Comments follow the project's existing pattern: a short purpose statement followed by detailed bullet points explaining behaviour, ownership, constraints and failure handling. The stylesheet is organized by visual area, including a dedicated restoration section whose bullet comments explain how the historical layout maps to the current workflow. The controller comments explain appearance, progress, navigation, recovery, revision reconciliation, inspection and generation. Diagram comments explicitly distinguish presentation changes from graph mutations.

When editing the interface:

- Preserve existing element IDs and data attributes unless the controller bindings change with them.
- Keep theme state outside graph sessions and request-recovery records.
- Keep full file paths in details and tooltips, with filenames on graph cards.
- Keep provider access and interrupted-operation retries as explicit user actions.
- Use named colour tokens for new selected/focused controls instead of hard-coded accent colours.

## Validation — 1 September 2026

- Backend: 171 tests discovered; 170 passed and one native Windows test skipped on macOS.
- Frontend: all 25 existing state, request and graph-presentation tests passed.
- Markup: all 128 original IDs retained, no duplicate IDs, 62 valid local icon references and no remote scripts.
- Browser: analysis, review, generation, the saved-workflow drawer, generated flowchart opening and the attachment download route were exercised using a synthetic project and isolated store. No live model calls or source-program execution.
- Layout: the restored analysis, review and generation screens were visually checked at the normal desktop width with no horizontal page overflow. Responsive rules stack configuration fields and workflow stages below 1,050 and 760 pixels, and move the inspector below the diagram using the existing 620-pixel breakpoint.

Native Windows execution, screen-reader behavior and forced-colour mode were not exercised on this macOS host. The implementation retains platform fonts, native controls, keyboard-focus styling, reduced-motion support and Windows forced-colour rules, but these checks are not a full accessibility certification.
