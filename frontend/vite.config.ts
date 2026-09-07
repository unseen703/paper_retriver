import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Port 5173 is not incidental: it is the single origin the backend's CORS
// allowlist permits (app/main.py DEV_ORIGINS). Changing it here without
// changing that list produces a dev server whose every request is blocked by
// the browser, with the backend logging nothing at all.
//
// Requests go straight to the API rather than through a Vite proxy. A proxy
// would make the same requests same-origin and quietly stop exercising the
// CORS configuration, so the first time it were wrong would be in a
// deployment nobody is proxying.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true },
});
