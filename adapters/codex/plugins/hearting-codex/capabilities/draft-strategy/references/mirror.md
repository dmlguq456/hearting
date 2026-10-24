## Companion Generation (`editorial/translate` unit; Conditional, Not Default)

Skip companion generation unless the user explicitly requests a second language, an external-audience contract requires one, or the existing artifact workflow already depends on a companion. A difference between the artifact language and the conversation language is not sufficient by itself.

When one of those conditions applies, dispatch the `editorial/translate` unit:

```text
Translate from {source language} to {target language}.
Source strategy: {strategy_path}
Target output: {target_path chosen from the existing project convention or explicit request}
Audience or workflow requirement: {requirement}

Preserve LaTeX commands, quotations, citations, paper titles, venue names, abbreviations, model names, datasets, metrics, paths, and identifiers when translation would reduce precision. Translate the remaining prose naturally for the target audience.
Consult a recalled writing preference only when the acting agent judges it relevant; explicit audience and project requirements take precedence.
Return only the file path, a 3–5 line summary, and one or two intentional terminology choices.
```

Do not infer a fixed source language, target language, filename suffix, or vendor-specific adapter path. Report the canonical strategy path, any required companion path, summary, and QA verdict in the conversation language.
