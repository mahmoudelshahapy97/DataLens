import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: { '@': path.resolve(HERE, 'src') },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
});
