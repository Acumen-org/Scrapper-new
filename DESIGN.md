---
name: Bellwether
description: A quiet charcoal workspace for evidence-led GTM intelligence.
colors:
  canvas: "#151515"
  navigation: "#111111"
  surface: "#1c1c1c"
  field: "#222222"
  control: "#292929"
  control-hover: "#363636"
  ink: "#f0f0f0"
  secondary: "#bdbdbd"
  muted: "#a0a0a0"
  dim: "#999999"
  rule: "rgba(255,255,255,.07)"
  control-rule: "rgba(255,255,255,.13)"
  focus-rule: "rgba(255,255,255,.2)"
  primary: "#b73545"
  primary-hover: "#9f2c3b"
  primary-readable: "#ef939c"
  primary-tint: "rgba(230,72,80,.13)"
  primary-rule: "rgba(240,107,114,.35)"
  action-text: "#ffffff"
  verified: "#45c08a"
  verified-tint: "rgba(69,192,138,.12)"
  verified-rule: "rgba(69,192,138,.32)"
  missing-evidence: "#e8ac4a"
  missing-evidence-tint: "rgba(232,172,74,.11)"
  missing-evidence-rule: "rgba(232,172,74,.32)"
typography:
  display:
    fontFamily: "\"Instrument Sans\", \"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    fontSize: "38px"
    fontWeight: 600
    letterSpacing: "-0.035em"
  headline:
    fontFamily: "\"Instrument Sans\", \"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 600
    lineHeight: 1.18
    letterSpacing: "-0.028em"
  title:
    fontFamily: "\"Instrument Sans\", \"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.015em"
  body:
    fontFamily: "\"Instrument Sans\", \"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  table:
    fontFamily: "\"Instrument Sans\", \"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    fontSize: "13.5px"
    lineHeight: 1.55
  label:
    fontFamily: "\"Instrument Sans\", \"Segoe UI Variable Text\", \"Segoe UI\", -apple-system, BlinkMacSystemFont, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1
  mono:
    fontFamily: "ui-monospace, \"Cascadia Mono\", Consolas, monospace"
    fontSize: "12.5px"
rounded:
  chip: "6px"
  control: "8px"
  compact-panel: "10px"
  panel: "12px"
  dialog-panel: "14px"
  large: "16px"
  composer: "20px"
  pill: "99px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  panel: "20px"
  section: "24px"
  large: "32px"
  page: "40px"
  wide: "48px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.action-text}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "36px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.control}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "36px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.secondary}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "36px"
  input:
    backgroundColor: "{colors.field}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "36px"
  navigation-item:
    textColor: "{colors.secondary}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "34px"
  navigation-selected:
    backgroundColor: "rgba(255,255,255,.075)"
    textColor: "{colors.ink}"
  workspace-tab:
    textColor: "{colors.muted}"
    padding: "0 0 14px"
    height: "46px"
  chip-verified:
    backgroundColor: "{colors.verified-tint}"
    textColor: "{colors.verified}"
    rounded: "{rounded.chip}"
    padding: "0 8px"
    height: "22px"
  chip-published:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.chip}"
    padding: "0 8px"
    height: "22px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "20px"
  fit-summary:
    textColor: "{colors.ink}"
    padding: "15px 0"
  microsoft-sign-in:
    backgroundColor: "#f4f4f5"
    textColor: "{colors.canvas}"
    rounded: "{rounded.control}"
    height: "48px"
    width: "100%"
---

# Design System: Bellwether

## Overview

**Creative North Star: "The Intelligence Workspace"**

Bellwether is a serious, neutral charcoal workspace in which firm identity, evidence and available actions are easy to scan. Restrained red supplies emphasis; hierarchy comes from type, space, fine rules and named views. Summary rows keep the first view quiet while complete research and operational detail remain reachable.

The October 2026 refinement keeps self-hosted Instrument Sans, solid surfaces and a professional sign-in experience. The shipped identity is the red rounded-square mark with three stacked white chevrons in `prospect/static/mark.svg`. Reuse that asset rather than rebuilding it.

**Key Characteristics:**

- Solid charcoal surfaces with restrained red emphasis.
- Plain summary rows and generous separation between evidence groups.
- Named, directly addressable views for every major workflow.
- Explicit contact provenance and score uncertainty.
- Compact controls, visible keyboard focus and responsive layouts.

Refreshed on 2026-10-08 from the active `prospect/static/app.css`, `app.js`, `home_view.py`, `firm_view.py`, `webapp.py` and final desktop/mobile renders in `data/quiet-review/final`. `workspace.css` is unused legacy material and is not part of the cascade.

## Colors

The palette is neutral charcoal with one red interaction accent and evidence-specific status colors. Frontmatter preserves the source's exact hex and rgba values.

### Primary

- **Signal Red:** Filled primary actions and selected-tab or sidebar markers.
- **Primary Hover:** The darker action state.
- **Readable Red:** Links, focus outlines, active icons and adverse status text.
- **Primary Tint and Rule:** Subtle action, focus and adverse-state boundaries.

### Neutral

- **Canvas and Navigation:** The page and its darker persistent navigation.
- **Surface, Field, Control and Control Hover:** Solid tonal steps for containers, form fields and controls.
- **Ink, Secondary, Muted and Dim:** Main content, supporting text, metadata and lower-priority context.
- **Rule, Control Rule and Focus Rule:** Fine section dividers and progressively stronger control boundaries.
- **Action Text:** White text on red actions.

Green marks verified contacts and positive recorded states; amber marks missing coverage or risk. Both pair text with a restrained tint and border. Product identity dots use their existing data-defined colors without recoloring major surfaces.

**The Signal Rule.** Use red for actions, selection or meaningful status; keep large backgrounds neutral.

**The Evidence Rule.** Pair semantic color with explicit labels and values. Distinguish verified from published contacts, and missing evidence from known facts.

## Typography

**Display and Body Font:** Self-hosted Instrument Sans, variable weights 400–700, with the fallback stack in frontmatter.

**Label/Mono Font:** Instrument Sans for controls; the mono stack is limited to identifiers and technical values.

One sans-serif voice connects navigation, research, tables and conversation. Headings use moderate weight and tight spacing; numbers use tabular figures where alignment matters.

### Hierarchy

- **Display:** Global AI welcome, reduced to 29px on small screens.
- **Headline:** General page identity, reduced to 25px on small screens. Firm titles retain the same desktop size with a lighter weight (550), and wrap long names.
- **Title:** General section headings. Home and firm overview sections use 18px at weight 550.
- **Body:** General interface and reading text.
- **Table:** Compact evidence rows with vertical breathing room.
- **Label:** Standard actions. Primary buttons strengthen the weight to 600; field labels are smaller (12px).
- **Numeric summary:** Home totals use 25px; firm headline values and fit summaries use 24px at weight 550.

**The One Voice Rule.** Keep Instrument Sans across headings, body and controls; express hierarchy with size, weight and spacing.

## Layout

Desktop uses a fixed sidebar (224px) and a centered page (1440px maximum), with narrow (1120px) and wide (1680px) variants. Standard page insets are 40px 40px 72px. Home and firm overview sections use a 1.25:1 column ratio with a 48px gap, tightened to 32px below 1200px.

Home has four views: Overview, Product lists, Your workspace and Data coverage. Its overview begins with plain linked totals and two content columns. Product and coverage detail remain in their own views. Firm identity and three headline facts sit above seven views: Overview, People & contacts, Product fit, Activity, Assets & funds, Research and Workspace. Firm summaries use open rows and section rules; detailed people views may use bordered cards.

Below 980px the sidebar becomes an expandable Menu under a mobile header; page insets become 22px 16px 64px. Below 760px overview and workspace columns stack, the Home search fills the width and product summaries reflow. Below 600px fit statuses and reasons continue under their product name; evidence tables scroll inside their own region. Tab strips scroll horizontally and reveal the selected tab.

The global AI conversation is centered in a 780px maximum column. Firm AI opens contextually in a right-hand dialog, 500px wide and constrained to the viewport. Sign-in is one centered 420px maximum panel with a mark, heading, Microsoft action and an optional password disclosure.

**The Named Views Rule.** Reduce simultaneous detail through clearly named views without removing supported workflows or their data.

## Elevation & Depth

Ordinary surfaces are flat and solid. Home summaries and firm overview rows sit directly on the canvas; other panels use tonal changes and fine borders. Decorative glass, blur and glow are absent. Input focus rings and small status outlines communicate state and are not ambient decoration.

One shared soft shadow lifts command search, floating filters, bulk actions and transient feedback. A small shadow identifies a selected segmented control. The AI drawer uses a solid surface, border and dimmed backdrop. Exact shadow and focus values live in the sidecar.

**The Flat Workspace Rule.** Separate ordinary content through tone, space and rules; reserve strong elevation for temporary overlays.

## Shapes

Standard controls are rounded rectangles; panels soften the corners one step further. Chips use small rectangular corners, while filter pills and compact avatars use circular rounding. Tabs are square-ended text controls with a two-pixel selected underline. Row summaries have no enclosing card shape.

The mark uses its shipped SVG geometry. Supporting icons are inline strokes, subordinate to labels.

## Components

### Buttons

Standard buttons are 36px high with neutral fill; primary actions use Signal Red and white text, and ghost actions use a transparent fill. Small contextual actions use 28px and large actions 44px. Hover changes tone and border; pressing shifts a button by one pixel. Focus uses a two-pixel Readable Red outline with a two-pixel offset. Disabled buttons reduce opacity.

### Chips

State labels are 22px high. Verified contacts use green text, tint and border. Published addresses retain a distinct textual badge; ordinary unverified or unknown states use a neutral outline, while recorded risky or adverse states keep their respective semantic colors. Contact provenance is not replaced by visual confidence.

### Cards / Containers

General panels use Surface, a fine Rule border, the panel radius and 20px padding. Home totals, product summaries and firm overview groups instead use open rows and thin separators. Detailed people cards keep their containment. Native disclosures expose secondary evidence under a specific summary label.

### Inputs / Fields

Fields use the raised Field fill, a Control Rule border and the control radius. Standard inputs are 36px high. Focus strengthens the border and adds a three-pixel red tint ring. Visible labels describe form controls; compact search and conversation inputs carry accessible labels. Advanced filters stay behind a named control.

### Navigation

Workflow links have comparable visual prominence. The active sidebar item has a neutral translucent fill, readable red icon and a short red edge marker. Product lists, exploration and data tools remain accessible in the sidebar.

Home and firm tabs use a red underline and text emphasis. They expose one named panel at a time, support Left/Right and Home/End keys, maintain selection in the URL hash and respond to browser history. On narrow screens the selected tab is revealed within the horizontal strip.

### Evidence and Fit

Tables retain compact data rows, fine dividers and local horizontal scrolling. Overview fit rows combine a prominent score with explicit known-data coverage or eligibility text. Detailed scoring keeps missing factors visible; coverage tracks use solid fill for known data and amber hatching for the unknown remainder. Preserve the distinction between zero, missing data and ineligible status.

### Conversation

The global neutral composer has a softly rounded outline and a circular send action. The firm composer lives in its contextual dialog, leaving research space available until opened. Opening, closing and focus behavior use native dialog semantics.

### Sign-in

The centered solid panel leads with the shipped mark and a simple Sign in heading. Microsoft uses a high-contrast light button, 48px high; password sign-in is a secondary disclosure when Microsoft is available. Error text remains near the form.

Control color and border transitions usually take 120ms; disclosure and composer state transitions use 150ms, and transient feedback enters over 200ms. Reduced-motion preferences disable animations and transitions and restore immediate scrolling.

## Do's and Don'ts

### Do:

- **Do** keep major surfaces solid charcoal and use red for purposeful emphasis.
- **Do** retain the shipped mark asset and self-hosted Instrument Sans.
- **Do** keep every GTM workflow accessible through visible navigation or a named view.
- **Do** label verified and published email distinctly and keep missing score evidence explicit.
- **Do** use concise headings and show supporting explanation where it helps a decision.
- **Do** preserve keyboard focus, hash navigation, responsive reflow and reduced-motion support.

### Don't:

- **Don't** add glass, backdrop blur, decorative glow, blue-tinted surfaces or theatrical login graphics.
- **Don't** turn every fact into a card or add introductory copy that repeats a heading.
- **Don't** introduce decorative eyebrows, glyph-based icon substitutes or a separate display font.
- **Don't** hide uncertainty behind a score or style published email as verified.
- **Don't** treat unused stylesheets, old screenshots or residual legacy declarations as the system for new work.
