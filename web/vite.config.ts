import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { apiProxy } from "./src/devProxy.ts";

export default defineConfig({
  plugins: [react()],
  server: { proxy: apiProxy("http://127.0.0.1:8765") },
});
