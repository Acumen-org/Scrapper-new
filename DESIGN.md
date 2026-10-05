---
name: Bellwether
description: A restrained charcoal workspace for evidence-led GTM intelligence.
colors:
  canvas: "#151515"
  navigation: "#111111"
  surface: "#1d1d1d"
  control: "#272727"
  control-hover: "#343434"
  ink: "#f0f0f0"
  secondary: "#bdbdbd"
  muted: "#a0a0a0"
  rule: "#333333"
  control-rule: "#4a4a4a"
  primary: "#b73545"
  primary-hover: "#9f2c3b"
  primary-readable: "#ef939c"
  primary-tint: "rgba(181,46,59,.15)"
  selected-navigation: "#292020"
  action-text: "#ffffff"
  verified: "#63aa7c"
  verified-tint: "rgba(99,170,124,.13)"
  missing-evidence: "#cfa95c"
  missing-evidence-tint: "rgba(207,169,92,.12)"
typography:
  display:
    fontFamily: '"Instrument Sans", "Segoe UI", sans-serif'
    fontSize: "35px"
    fontWeight: 500
    lineHeight: 1.2
    letterSpacing: "-0.035em"
  headline:
    fontFamily: '"Instrument Sans", "Segoe UI", sans-serif'
    fontSize: "32px"
    fontWeight: 550
    lineHeight: 1.2
    letterSpacing: "-0.03em"
  title:
    fontFamily: '"Instrument Sans", "Segoe UI", sans-serif'
    fontSize: "20px"
    fontWeight: 550
    lineHeight: 1.35
    letterSpacing: "-0.015em"
  body:
    fontFamily: '"Instrument Sans", "Segoe UI", sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.55
  table:
    fontFamily: '"Instrument Sans", "Segoe UI", sans-serif'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: '"Instrument Sans", "Segoe UI", sans-serif'
    fontSize: "13px"
    fontWeight: 550
    lineHeight: 1.3
  mono:
    fontFamily: 'ui-monospace, "Cascadia Mono", Consolas, monospace'
    fontSize: "12.5px"
rounded:
  chip: "5px"
  navigation: "7px"
  control: "8px"
  compact-panel: "10px"
  panel: "12px"
  identity: "14px"
  composer: "16px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  panel: "20px"
  section: "24px"
  large: "32px"
  wide: "48px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.action-text}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
  button-secondary:
    backgroundColor: "{colors.control}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
    height: "40px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.secondary}"
    rounded: "{rounded.control}"
    padding: "9px 15px"
    height: "40px"
  input:
    backgroundColor: "{colors.canvas}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "7px 10px"
    height: "40px"
  navigation-item:
    textColor: "{colors.secondary}"
    rounded: "{rounded.navigation}"
    padding: "11px 12px"
  navigation-selected:
    backgroundColor: "{colors.selected-navigation}"
    textColor: "{colors.ink}"
  chip-verified:
    backgroundColor: "{colors.verified-tint}"
    textColor: "{colors.verified}"
    rounded: "{rounded.chip}"
    padding: "3px 7px"
  research-panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "20px"
  ai-composer:
    textColor: "{colors.ink}"
    rounded: "{rounded.composer}"
    padding: "18px 20px"
---

# Design System: Bellwether

## Overview

**Creative North Star: "The Intelligence Workspace"**

Bellwether is a serious, neutral charcoal workspace in which firm identity, evidence and available actions are easy to scan. Restrained red supplies emphasis; hierarchy comes from type, spacing, thin borders and deliberate disclosure. The interface carries substantial information without making every fact compete in the first view.

The approved identity is the open geometric B with its red signal point, paired with a sans-serif wordmark. The self-hosted Instrument Sans is intentional. Preserve the professional, restrained character confirmed in PRODUCT.md: neutral dark grey surfaces, room to read, and a quiet sign-in experience.

**Key Characteristics:**

- Neutral charcoal layers with restrained red emphasis.
- Readable identity and evidence before secondary detail.
- Flat panels, fine rules and compact rectangular controls.
- Progressive disclosure with complete routes to every workflow.
- Clear distinctions between verified, unknown and missing evidence.

This document reconciles the provisional brief with the shipped cascade: `prospect/static/app.css`, then `prospect/static/workspace.css`, plus the current rendered views. The frontmatter records reused values; isolated legacy styling is not a precedent for new work.

## Colors

The palette is achromatic in its surfaces, with red for emphasis and limited semantic green and amber.

### Primary

- **Signal Red:** Filled primary actions and active tab underlines.
- **Pressed Red:** Hover treatment for primary actions.
- **Readable Red:** Hovered links, focus outlines, selected navigation icons and adverse status text.
- **Red Tint:** Selection and adverse status backgrounds; inherited from the shared stylesheet.
- **Selected Navigation:** A quiet red-charcoal fill supporting the current destination.

### Neutral

- **Canvas and Navigation:** Darkest layers separating the workspace from persistent navigation.
- **Surface, Control and Control Hover:** Successive raised tones for panels and interactive elements.
- **Ink, Secondary and Muted:** Primary content, supporting text and metadata. Essential identities and actions use Ink.
- **Rule and Control Rule:** Hairline section boundaries and stronger input/control edges.
- **Action Text:** White text on filled primary actions.

Green and its tint identify verified contacts or positive lead state. Amber and its tint identify risk, unknown coverage or missing evidence. These are semantic statuses, not additional decorative accents. The logo retains the literal colors in `prospect/static/mark.svg`; use the asset rather than rebuilding it from palette tokens.

**The Signal Rule.** Use red for an action, selected state or meaningful status; keep large backgrounds neutral.

**The Evidence Rule.** Pair semantic color with explicit labels and values. Missing evidence remains visibly different from a known negative or a verified fact.

## Typography

**Display and Body Font:** Instrument Sans, self-hosted as a variable font with Segoe UI and sans-serif fallbacks.

**Label/Mono Font:** The same sans-serif for interface labels; the mono stack is limited to identifiers and technical values.

**Character:** One measured sans-serif voice connects navigation, research, tables and conversation. Moderate weights and tightened headings establish hierarchy without a separate decorative display face.

### Hierarchy

- **Display:** The centered AI welcome. It steps down at narrow widths rather than dominating the screen.
- **Headline:** General page headings. Firm identity uses a smaller contextual heading so long legal names wrap within the profile.
- **Title:** Section headings separating evidence groups.
- **Body:** Reading copy and general interface text. Firm overview prose uses a relaxed line-height and a measure of approximately 65 characters.
- **Table:** Compact data rows with greater vertical breathing room; firm names remain larger and more prominent than metadata.
- **Label:** Standard action text. Supporting navigation and field labels remain sentence case.

Tabular numerals align scores, counts and numeric columns. Primary buttons use a stronger weight (600) than ordinary control labels. Compact metadata is secondary and must not carry a critical action by itself.

**The One Voice Rule.** Keep Instrument Sans across headings, body and controls; express hierarchy with size, weight and spacing.

## Layout

Desktop uses a fixed navigation column (216px) and a centered content area with a standard maximum width (1460px), or a wide research/table maximum (1660px). Standard desktop page insets are (42px 48px 72px). Space follows the frontmatter rhythm, with measured component-specific gaps where needed.

Firm research uses an identity column (248px) and a flexible main area separated by a gap (36px). The widest layout expands the column and gutter; intermediate widths tighten both. Context remains stable while five research destinations expose deeper evidence. Management fields and detailed research use disclosures.

At widths up to (980px), navigation becomes an expandable Menu under the mobile header. At (700px), firm identity stacks above the research tabs; the identity summary becomes compact, with its management section collapsed. The overview places contact intelligence before product fit, then the latest filing or signal. At (480px), fit summaries stack vertically and action groups wrap. Research tabs and large tables scroll within their own region rather than forcing the page wider.

Home uses a split overview and separate tab destinations. AI starts around a centered conversation panel with a maximum width (760px); after the first message, the conversation grows while the composer remains available. Sign-in is one centered panel with a maximum width (432px), comfortable internal padding and a restrained brand mark.

**The Progressive Disclosure Rule.** Keep identity, current evidence and the next action visible; put secondary filters, sources and management fields behind a clearly named control.

## Elevation & Depth

Core workspace surfaces are flat. Tonal steps and fine borders separate panels; the sign-in panel and firm monogram have no shadow. A dimmed backdrop and a stronger boundary establish the on-demand AI sheet. Shadows remain functional on temporary overlays such as command search and feedback, not on every card.

### Shadow Vocabulary

- **Command overlay:** A broad soft shadow isolates the command palette from its backdrop; exact CSS is recorded in the sidecar.
- **Feedback:** A smaller soft shadow lifts transient feedback above page content; exact CSS is recorded in the sidecar.

**The Flat Workspace Rule.** Establish depth through tone and borders; reserve floating elevation for temporary overlays.

## Shapes

Controls are compact rounded rectangles. Navigation uses a slightly tighter radius than fields and buttons; panels have softer corners, with the identity panel and composer one step softer again. Chips are small rounded rectangles rather than large pills. One-pixel borders are structural; selected tabs use a two-pixel underline. Circular geometry is reserved for small avatars, status points and the small AI activity indicator.

The geometric B is the binding identity asset. Icons are simple inline strokes and must remain visually subordinate to their labels.

## Components

### Buttons

Compact and deliberate. Standard controls have a minimum height (40px); primary actions use Signal Red and white text, secondary actions use a neutral control fill and visible border, and ghost actions use a transparent fill. Compact contextual actions can use the existing smaller variant (32px). Primary hover deepens red; secondary hover raises the neutral tone. Keyboard focus uses a readable red outline (2px) with an offset (3px). Disabled buttons reduce opacity and lose the pointer cursor.

### Chips

Small state labels with text that explains their meaning. Verified, adverse and risky states use semantic tint-and-text pairs. Unknown, queued and unverified states use a neutral outlined treatment. Do not make an unverified address look like an available contact.

### Cards / Containers

Research disclosures and highlight panels use Surface, a thin Rule border and the panel radius. Interior padding generally follows the panel or section spacing steps. Identity and home containers use the identity radius. Avoid subdividing every sentence into another card; section rules can group evidence directly on the canvas.

### Inputs / Fields

Dark canvas fill, Control Rule border and the control radius. The input focus border turns red with a subtle two-pixel tint ring. Labels stay visible and left aligned. Advanced filters disclose below the essential search controls. Form rows wrap instead of compressing fields past usability.

### Navigation

Primary workflow links have equal prominence. The active destination uses a quiet red-charcoal fill, stronger text and a readable red icon. Product lists form a separately collapsible group. Research and home tabs use a thin red underline, with ordinary sentence-case labels and horizontal overflow on small screens.

### Evidence Tables

Rows foreground the firm or person, a small set of decision-relevant values, and a clearly named route to detail. Tables use a Surface background, fine horizontal dividers, rounded outer corners and generous row spacing. Outer cell edges retain an inset (20px). Supporting evidence expands inline; actions and additional fields disclose as needed. Preserve sources, exports, uncertainty and keyboard access.

### Fit and Coverage

Fit summaries combine a prominent score, a known-coverage track and an explicit missing-factor label. Solid neutral fill represents known coverage; amber hatching makes the unknown remainder visually distinct. The score does not replace evidence or erase uncertainty.

### Conversation

The global composer is a wide, softly rounded neutral field with a clear send action and small status indicator. Its surrounding panel centers before conversation begins. A firmer neutral outline indicates focus. Firm conversation opens in a right-hand dialog sheet (480px maximum, constrained to the viewport), keeping AI available on demand without occupying a permanent research column.

State changes use short color/background transitions (typically 120ms). Transient feedback may use a brief entrance (200ms). Reduced-motion preferences disable animation and transitions and restore immediate scrolling.

## Do's and Don'ts

### Do:

- **Do** keep major surfaces neutral charcoal and let red identify purposeful interaction.
- **Do** retain the geometric B asset and self-hosted Instrument Sans.
- **Do** disclose secondary detail while keeping every supported workflow reachable.
- **Do** pair scores and contact states with visible evidence or uncertainty.
- **Do** preserve visible keyboard focus and respect reduced-motion preferences.
- **Do** reflow identity and action groups before reducing text or squeezing columns.

### Don't:

- **Don't** introduce blue-tinted surfaces, theatrical login graphics, halos or oversized AI ornaments.
- **Don't** use decorative eyebrow labels, gratuitous glyph icons or a separate system display face.
- **Don't** replace unknown values with invented data or present inferred email addresses as usable contacts.
- **Don't** spread shadows, accent fills or nested cards across routine workspace content.
- **Don't** use residual legacy CSS or isolated view-specific defects as rules for new screens.
