const API = process.env.AETHER_API_URL || "http://127.0.0.1:8000";
/** Same-origin proxy so the HttpOnly session cookie and CSRF header work without CORS. */
export default {
  reactStrictMode: true,
  async rewrites() { return [{ source: "/api/v1/:path*", destination: `${API}/api/v1/:path*` }]; },
};
