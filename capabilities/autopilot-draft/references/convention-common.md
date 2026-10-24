# §Common — Rules for every form

> Apply these rules to every autopilot-draft form: `paper`, `presentation`, and `doc`. They take precedence as the shared baseline for the form-specific conventions.

- **Four-step paragraph-cohesion pre-check** — Before writing paste-ready or body prose, check (a) duplicated substance, (b) the paragraph axis such as motivation → design → formalization or claim → evidence → caveat, (c) cross-section redundancy, and (d) whether the change is EDIT, REPLACE, INSERT, or DROP. Prefer EDIT or REPLACE over INSERT.
- **Anchors** — Base cross-references on stable identifiers: section, label, paragraph, slide title, or page number. Do not hard-code line numbers because they drift during editing. Use `anchor: L###` only as a secondary hint.
- **Acronyms** — Expand an acronym once at first use, then use the abbreviation. Introduce new acronyms once in the abstract or opening.
- **Avoid LLM-flavored wording** — Prefer plain language over terms such as *instantiation*, *operator*, *load-bearing*, or *via gradient withholding* when a simpler phrase is accurate.
- **Final editorial pass** — Run one final `editorial/polish` unit pass on every user-facing Markdown artifact for natural phrasing, consistent notation, and readable sentence rhythm through the active runtime adapter.
- **Language selection** — Follow the mode × genre primary-language table in `owner-execution.md` Step 4.1. User-facing guidance follows the user's communication language. Artifact language follows its audience and venue. Create a language companion only when explicitly requested, required by an external audience, or already required by the artifact workflow; a language mismatch alone is not a trigger.
