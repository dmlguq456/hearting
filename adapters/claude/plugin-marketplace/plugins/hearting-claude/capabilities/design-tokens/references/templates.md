# Token File Templates

Create either or both files as appropriate for the stack.

## Option A: tokens.css (CSS variables)

`<project_root>/styles/tokens.css` or `<project_root>/app/tokens.css`:

```css
:root {
  --color-brand-500: #F97316;
  --color-neutral-50: #FAFAFA;
  /* ... */
}

@media (prefers-color-scheme: dark) {
  :root {
    --color-neutral-50: #18181B;
    /* dark-mode adaptation */
  }
}
```

## Option B: tailwind.config.ts

`<project_root>/tailwind.config.ts`:

```ts
import type { Config } from 'tailwindcss'

const config: Config = {
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#FFF8F3',
          500: '#F97316',
          // ...
        },
      },
      fontFamily: { /* ... */ },
      spacing: { /* ... */ },
    },
  },
}
export default config
```
