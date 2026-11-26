---
field: code
makes: working software
---
# code

Code is the work of making software that runs.

## Fundamentals (not dropped)

- A change that makes things better is approved. — Approve a change once it clearly improves the health of the code, even if it is not perfect. [1–4]
- Change in small pieces. — The smaller the change, the faster and more carefully it is reviewed, and the fewer bugs it carries. [1–4]
- Tests come with the change. — Tests for a change are delivered in the same change as the code they cover. [1–4]
- A review after an incident does not blame. — The review fixes the system and the process, not the person. [1–4]

## Not decided here

These differ from person to person and team to team, and this page leaves them open: the order of the work, the tools, how deep a review or a test goes, how large a change must be before a design document is written, whether decisions are kept in a separate record, how often work is merged into the main line, how releases are rolled out, and what a team counts as "done".

## Boundaries with other fields

- Checking a finished build against an agreed design overlaps with review and tests here, but the design itself belongs to `design`.
- Measuring a model or an experiment, and interpreting the result, belongs to `lab`. Code is the software that runs it.
- The text of a paper or a report belongs to `docs`, even when it is about code.

## Sources

The numbers after each line are the sources the field survey cites for this group of lines as a whole. Only lines that two independent searches confirmed, at least one from the original text, are listed. In "post-incident review" the sense is the review after an outage or failure, as in the postmortem source.

1. Google Engineering Practices, The Standard of Code Review — https://google.github.io/eng-practices/review/reviewer/standard.html (survey cards S1, V1)
2. Google Engineering Practices, Small CLs — https://google.github.io/eng-practices/review/developer/small-cls.html (card S2)
3. Google SRE Book, Postmortem Culture — https://sre.google/sre-book/postmortem-culture/ (cards S3, V3)
4. DORA capabilities (working in small batches, streamlining change approval) — https://dora.dev/capabilities/ (cards S5, V2)

## To change this page

If something here is wrong or missing, please tell us in an issue or a pull request on the Hearting repository. Anyone who works in this field is welcome to. There is no form to fill in and no separate channel.
