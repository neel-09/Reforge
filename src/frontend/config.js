/* Reforge frontend config.
 * API_BASE: "" when the UI is served by the FastAPI app (same origin, recommended).
 *           Set to e.g. "https://reforge-api.example.com" when hosting the UI separately
 *           (then set REFORGE_CORS on the backend to the UI's origin).
 * DEMO:     "auto" = live if /api/health answers, otherwise a scripted demo run.
 *           "on" forces the demo, "off" never falls back. URL ?demo=1 also forces it.
 * DEMO_VIDEO_URL: shown in the daily-limit pop-up (the backend's REFORGE_DEMO_VIDEO_URL wins when set).
 */
window.REFORGE_CONFIG = {
  API_BASE: "",
  DEMO: "auto",
  DEMO_VIDEO_URL: "https://youtu.be/5xdMIWb8mXc",
};
