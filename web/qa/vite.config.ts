import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
export default defineConfig({ root: fileURLToPath(new URL('.', import.meta.url)), plugins: [react()], base: './', build: { outDir: '../../../user-management-qa-dist', emptyOutDir: true, rollupOptions: { input: fileURLToPath(new URL('user-management.html', import.meta.url)) } } });
