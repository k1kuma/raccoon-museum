import { defineConfig } from 'astro/config';

// GitHub Pages project site: https://k1kuma.github.io/raccoon-museum/
export default defineConfig({
  site: process.env.SITE_URL || 'https://k1kuma.github.io',
  base: process.env.BASE_PATH || '/raccoon-museum',
  trailingSlash: 'always',
  build: { format: 'directory' },
});
