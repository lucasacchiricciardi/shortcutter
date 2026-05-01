# Shortcutter site

Static site for [shortcutter](https://github.com/lucasacchiricciardi/shortcutter), built with Astro and deployed to GitHub Pages.

Live at: <https://lucasacchiricciardi.github.io/shortcutter>

## Local development

```bash
cd site
npm install
npm run dev
```

Site runs at <http://localhost:4321/shortcutter>.

## Build

```bash
npm run build
npm run preview
```

Output goes to `dist/`.

## Deployment

Pushed to `main` → GitHub Action builds and deploys automatically. The workflow lives at `.github/workflows/deploy.yml` in the repo root.

The action triggers when files under `site/`, `docs/`, the root README, or the workflow itself change.

## Adding content

### A new docs page

1. Create a markdown file under `src/content/docs/`
2. Add frontmatter: `title`, `description`, `order`, `updated`
3. Push — the page appears at `/docs/<filename>` automatically

### A new homepage section

Edit `src/pages/index.astro` directly. Section blocks live inside the `<BaseLayout>` and use scoped styles.

## Stack

- [Astro 5](https://astro.build/) for the static site generator
- [@astrojs/sitemap](https://docs.astro.build/en/guides/integrations-guide/sitemap/) for SEO
- Plain CSS with custom properties — no framework, no Tailwind, no JS runtime

## Design notes

- Light/dark mode follows OS preference, no toggle
- Sans-serif system stack, no custom fonts (zero blocking requests)
- Single column, max width 720px for content, 960px for layout
- All content under 100 KB total page weight
