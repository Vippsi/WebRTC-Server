import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    host: true,
    port: 54781,

    allowedHosts: ['roku.vippsi.dev'],
    // Optional but often needed when proxied over HTTPS:
    hmr: {
      host: 'roku.vippsi.dev',
      protocol: 'wss',
    },
  },

  preview: {
    host: true,
    port: 54781,
  },
});
