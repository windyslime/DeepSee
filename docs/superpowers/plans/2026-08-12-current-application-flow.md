# DeepSee Current Application Flow Implementation Plan

> **For agentic workers:** Execute these two tasks inline. The deliverable is documentation only and does not change runtime behavior.

**Goal:** Produce an editable and directly previewable end-to-end diagram of the currently implemented DeepSee Web and local gateway flow.

**Architecture:** A small deterministic generator owns the diagram geometry and emits Obsidian Excalidraw, standard Excalidraw, and SVG from the same element model. The SVG is rendered to PNG for visual inspection, while structural checks validate the Excalidraw schema and required flow labels.

**Tech Stack:** JavaScript, Excalidraw JSON v2, SVG, macOS image rendering tools.

## Global Constraints

The diagram must distinguish the product journey from engineering details, use only currently implemented paths, keep all Excalidraw text at 14 px or larger with `fontFamily: 5`, and show planned Tauri or Codewhale work only by omission.

---

### Task 1: Generate the flow diagram assets

**Files:**

- Create: `docs/diagrams/generate-current-application-flow.mjs`
- Create: `DeepSee当前应用全流程.flowchart.md`
- Create: `DeepSee当前应用全流程.flowchart.excalidraw`
- Create: `DeepSee当前应用全流程.flowchart.svg`
- Create: `DeepSee当前应用全流程.flowchart.png`

**Interfaces:**

- Consumes: the approved design in `docs/superpowers/specs/2026-08-12-current-application-flow-design.md`.
- Produces: equivalent editable and preview formats generated from one geometry model.

- [ ] Build four horizontal lanes for the user/Web, gateway, vision/reasoning, and response/observability stages.
- [ ] Draw the product mainline, text/image branch, cache branch, response loop, error rail, and trace rail.
- [ ] Emit valid Obsidian and standard Excalidraw structures plus an SVG preview.
- [ ] Render the SVG to PNG.

### Task 2: Verify structure and visual output

**Files:**

- Verify: `DeepSee当前应用全流程.flowchart.md`
- Verify: `DeepSee当前应用全流程.flowchart.excalidraw`
- Verify: `DeepSee当前应用全流程.flowchart.png`

**Interfaces:**

- Consumes: Task 1 artifacts.
- Produces: evidence that the files are parseable, complete, readable, and non-overlapping.

- [ ] Parse both JSON representations and assert unique IDs, required fields, minimum font sizes, and absence of unsupported element fields.
- [ ] Assert that the diagram includes the pure-text path, image path, image safety, vision modes, cache, DeepSeek, SSE, UI rendering, errors, and trace metadata.
- [ ] Inspect the PNG at full canvas scale and revise any overlaps, clipped text, ambiguous arrows, or low-contrast regions.
- [ ] Run `git diff --check` and report the exact deliverable paths.
