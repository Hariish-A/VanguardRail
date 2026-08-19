import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

// `base: "./"` is load-bearing. The build is served from an S3 object URL whose path is
// not the site root, so absolute `/assets/...` references would 404. Relative paths make
// the same bundle work from S3, from a plain `file://` open, and from `vite preview`.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  base: "./",
  build: {
    outDir: "dist",
    sourcemap: false,
    // One vendor chunk rather than many. The console is loaded once by a reviewer and
    // then left open; a single round trip beats fine-grained caching here.
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "framer-motion"],
        },
      },
    },
  },
});
