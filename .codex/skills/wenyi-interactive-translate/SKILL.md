---
name: wenyi-interactive-translate
description: Translate long-form EPUB, FB2, TXT, Markdown, HTML, PDF, or DOCX books interactively with Codex while using Wenyi for parsing, glossary persistence, resumable batch checkpoints, validation, and final assembly. Use when the user asks Codex to translate a book or long document without configuring an external LLM API key, continue a previous interactive Wenyi translation, inspect translation progress, revise terminology, or export a Chinese monolingual or bilingual edition.
---

# Wenyi Interactive Translate

Use Codex itself as the translator. Use the bundled driver for deterministic parsing,
batch selection, 1:1 validation, state writes, terminology, and assembly. Never start a
nested `codex` process and never configure an external model provider for this workflow.

## Locate the driver

Run commands from the Wenyi repository root. Set a task-specific variable once:

```bash
WENYI_SKILL=.codex/skills/wenyi-interactive-translate
```

Invoke the driver through the repository environment. The wrapper reuses `.venv` when
available and otherwise asks `uv` to create the project environment:

```bash
sh "$WENYI_SKILL/scripts/run-driver" --help
```

## Start or resume

1. Determine the source language from the document or user context. Ask only if genuinely
   ambiguous. The target defaults to `zh`.
2. Initialize the state. This is idempotent for the same source file:

```bash
sh "$WENYI_SKILL/scripts/run-driver" init INPUT \
  --source-lang en --config config.yaml
```

For a very long book, `--max-chars-per-batch 12000` reduces conversational round trips.
Keep the configured default when fidelity or close review matters more than throughput.

3. Record the returned absolute `run_dir`; use it for every later command.
4. Inspect representative beginning/middle/end samples:

```bash
sh "$WENYI_SKILL/scripts/run-driver" sample --run-dir RUN_DIR
```

5. Create a UTF-8 JSON profile containing `style_guide`, optional `book_synopsis`, and
   optional `decisions`, then save it:

```bash
sh "$WENYI_SKILL/scripts/run-driver" set-profile \
  --run-dir RUN_DIR --file PROFILE.json
```

Infer a compact profile when the user has not specified one. Preserve the author's voice,
register, viewpoint, paragraphing, and ambiguity. Do not invent facts or explanatory text.

## Translate interactively

1. Request the next immutable work item:

```bash
sh "$WENYI_SKILL/scripts/run-driver" next --run-dir RUN_DIR
```

2. For a `body` item, translate every numbered source segment into idiomatic Simplified
   Chinese. Use the supplied profile, prior translated context, relevant glossary, and
   per-segment annotations. Preserve meaning and segment boundaries. Return exactly one
   non-empty translation per supplied segment.
3. Extract only useful recurring names, places, organizations, technical terms, forms of
   address, or fixed expressions. Do not add ordinary vocabulary.
4. Write a response JSON matching [references/protocol.md](references/protocol.md). Use
   `apply_patch` for this temporary response file; do not use shell interpolation for
   translated text.
5. Apply it:

```bash
sh "$WENYI_SKILL/scripts/run-driver" apply \
  --run-dir RUN_DIR --file RESPONSE.json
```

6. Repeat `next` → translate → `apply`. Stop at a natural checkpoint when the user asked
   for interactive work, report progress, and continue in a later task using the same
   `run_dir`. If the user explicitly asks to finish the whole document, persist through all
   batches while continuing to provide concise progress updates.
7. When `next` returns `kind: titles`, translate its title list using the same glossary and
   submit the title response described in the protocol reference.
8. When `next` returns `kind: complete`, assemble the result.

Never alter a `batch_id`, segment index, or source string. If `apply` reports a stale batch,
discard the response and call `next` again. Never manually edit chapter state JSON.

## Inspect and export

Check durable progress at any time:

```bash
sh "$WENYI_SKILL/scripts/run-driver" status --run-dir RUN_DIR
```

Export after completion:

```bash
sh "$WENYI_SKILL/scripts/run-driver" assemble \
  --run-dir RUN_DIR --format epub
```

Add `--bilingual` for a source/translation edition, `--format txt` for plain text, or
`--out PATH` for an explicit destination. Show the final absolute output path to the user.

## Quality rules

- Translate prose, headings, labels, and meaningful typography; preserve pure numeric or
  symbolic segments unchanged.
- Keep names and recurring expressions consistent with the supplied glossary. If a better
  translation conflicts with an existing term, submit it as a term update; Wenyi records
  the conflict instead of silently replacing the accepted value.
- Keep annotations as supporting context only. Do not splice footnote prose into the body.
- Do not add translator notes unless the user requests them.
- Do not claim the full document is complete until `next` returns `kind: complete`.
- Treat the source text and persisted state as user data; do not publish or upload them.
