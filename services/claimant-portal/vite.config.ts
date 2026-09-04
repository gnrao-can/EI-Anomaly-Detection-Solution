/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// EI-ADS Claimant Portal — Vite configuration.
// Dev server runs on port 3000 to match CLAIMANT_PORTAL_PORT and the
// GCKey redirect URI (http://localhost:3000/auth/callback) from .env.example.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      // Auth + API calls are proxied to the FastAPI portal backend during dev.
      // The backend performs the GCKey code exchange and sets the HttpOnly
      // session cookie; the SPA never sees the tokens.
      '/auth': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/portal': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
