# Frontend design and maintenance

## Visual direction

The interface deliberately returns to the earlier DA Document Generator visual language. A white full-width header anchors a centred workspace. Numbered sections, thin blue-gray rules, square outlined fields, a horizontal progress tracker, a compact run log and broad output actions reproduce the familiar structure without reviving the old one-pass backend.

Each launcher invocation has its own browser-session namespace and Activity list. There is no saved-workflow drawer. The configuration, review and generation surfaces use nearly the complete viewport width. Typography uses the operating system's fonts and icons come from the local SVG library.

## Colour themes

Choose **Settings → Appearance → Accent theme**.

| Theme | Accent | Soft selection background |
| --- | --- | --- |
| Blue — default | `#0b5cff` | `#eef3ff` |
| Violet | `#7047c4` | `#f3effc` |
| Pink | `#d43d92` | `#fff0f8` |

- `styles.css` owns the palette through CSS custom properties. Buttons, progress stages, focus rings, icons and selected diagram arrows share those tokens.
- Status colours remain in the blue, pink and purple family. Text, dashed arrows and badges continue to communicate their meaning without relying solely on colour.
- `applyAppearance` in `app.js` accepts only the three supported names. The theme is stored separately from graph data and pending requests under `appearance.theme` in the launcher-session browser namespace.
- Selection applies immediately and survives reloads of the same session URL. A new launcher invocation intentionally begins with fresh interface state.
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

## Validation — 2 September 2026

- Python: 176 tests ran; 175 passed and one native Windows test skipped on macOS.
- Frontend: all 25 state, request and graph-presentation tests passed.
- Markup: the removed library bindings no longer have controller references, and all remaining local icon references resolve without remote scripts.
- Browser: session isolation, analysis, review, generation, project overview, generated flowchart opening and the scoped download route were exercised using synthetic projects and an isolated store. No live model calls or source-program execution.
- Layout: the main panel measured 1,233 pixels in a 1,265-pixel viewport with no horizontal page overflow. Responsive rules stack configuration fields and workflow stages below 1,050 and 760 pixels, and move the inspector below the diagram using the existing 620-pixel breakpoint.

Native Windows execution, screen-reader behavior and forced-colour mode were not exercised on this macOS host. The implementation retains platform fonts, native controls, keyboard-focus styling, reduced-motion support and Windows forced-colour rules, but these checks are not a full accessibility certification.
