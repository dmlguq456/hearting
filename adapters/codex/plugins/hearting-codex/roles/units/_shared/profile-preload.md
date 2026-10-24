# Shared Fragment: Cross-Project Profile Preload

> Referenced by units that consume cross-project user profiles. Replaces the
> near-identical preload paragraph previously restated across ~9 unit files.
> Each unit names its own aspect list inline; this fragment owns only the
> shared mechanics.

At the start of the task, load every aspect the referencing unit names and
treat each body as the working default:

```
python3 <agent-home>/tools/memory/mem.py profile <aspect>
```

- **Precedence:** project-local conventions (e.g.
  `<artifact-root>/analysis_project/code/experiment_conventions.md`) take
  precedence over conflicting cross-project defaults; a current-turn user
  instruction overrides everything.
- **Failure rule:** if a profile read fails or the aspect is missing, state
  that in the return and proceed without it — never guess or invent profile
  content.
- **Updates:** profile bodies change only through `/analyze-user` or
  `mem profile-append` (see `core/MEMORY.md §7.6`); units never write
  profiles.
