import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

// The launcher picks a free backend port and passes it here.
const backendPort = process.env.VITE_BACKEND_PORT || '8000';
const backendTarget = `http://127.0.0.1:${backendPort}`;

export default defineConfig({
  plugins: [react()],
  root: path.resolve(__dirname, '.'),
  base: '/',
  build: {
    outDir: path.resolve(__dirname, '../dist'),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    host: true,
    proxy: {
      // Backend serves /api/... paths directly; do NOT strip the /api prefix.
      '/api': {
        target: backendTarget,
        changeOrigin: true,
      },
      // WebSocket event stream lives on the same backend port at /ws.
      '/ws': {
        target: `ws://127.0.0.1:${backendPort}`,
        ws: true,
        changeOrigin: true,
      },
    },
  },
});