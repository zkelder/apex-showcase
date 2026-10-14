import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `vite build --mode demo` produces the static, read-only synthetic demonstration:
// relative asset paths (hostable under any sub-path) and API calls answered from
// pre-generated JSON in `public/demo-data` instead of a live backend.
export default defineConfig(({ mode }) => ({
  base: mode === "demo" ? "./" : "/",
  plugins: [react()],
  define: mode === "demo" ? { "import.meta.env.VITE_APEX_DEMO": JSON.stringify("1") } : {},
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
}));
