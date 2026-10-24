import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  optimizeDeps: { entries: ['index.html'] },
  server: { port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8787', '/physics': { target: 'http://127.0.0.1:8793', ws: true, changeOrigin: true } } },
  build: { chunkSizeWarningLimit: 650, rollupOptions: { output: { manualChunks: { three: ['three'] } } } },
});
