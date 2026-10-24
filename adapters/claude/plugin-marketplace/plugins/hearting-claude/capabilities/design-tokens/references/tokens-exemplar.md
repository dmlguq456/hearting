# tokens.md worked exemplar

```markdown
# Design Tokens — <name>

## Color Palette

### Brand
- `--color-brand-50`: #FFF8F3
- `--color-brand-500`: #F97316  (primary)
- `--color-brand-700`: #C2410C
- ...

**Rationale**: The user's "warm, minimal" brief calls for one orange-coral brand axis, contrasted with zinc neutrals.

### Neutral
- `--color-neutral-50`: ...
- ...

### Semantic
- `--color-success-500`: green-500
- `--color-warning-500`: amber-500
- `--color-danger-500`: red-500

## Typography

### Font Family
- `--font-sans`: 'Inter', system-ui, sans-serif
- `--font-serif`: 'Iowan Old Style', serif  (paper figures only)
- `--font-mono`: 'JetBrains Mono', monospace

### Scale
- `--text-xs`: 12px / 16px
- `--text-sm`: 14px / 20px
- `--text-base`: 16px / 24px
- `--text-lg`: 18px / 28px
- `--text-xl`: 20px / 28px
- `--text-2xl`: 24px / 32px

**Rationale**: Use a readable 16px / 1.5 body and a 1.25 modular heading scale (minor third).

## Spacing

- `--space-1`: 4px
- `--space-2`: 8px
- `--space-3`: 12px
- `--space-4`: 16px
- `--space-6`: 24px
- `--space-8`: 32px

Use an 8-point grid with a 4-point sub-unit, compatible with Tailwind defaults.

## Radius

- `--radius-sm`: 4px
- `--radius-md`: 8px
- `--radius-lg`: 12px
- `--radius-full`: 9999px

Slightly smaller than the shadcn default when supported by the user's stated or recalled preference.

## Shadow

- `--shadow-sm`: 0 1px 2px rgb(0 0 0 / 0.05)
- `--shadow-md`: 0 4px 6px -1px rgb(0 0 0 / 0.1)

## Motion

- `--ease-out`: cubic-bezier(0.16, 1, 0.3, 1)
- `--duration-fast`: 150ms
- `--duration-base`: 250ms
```
