import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 개발 서버: http://localhost:5173  /  백엔드: http://localhost:8000 (src/api.js 의 API_BASE)
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173 },
});
