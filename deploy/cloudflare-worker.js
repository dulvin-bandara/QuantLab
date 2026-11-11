// Optional: makes QuantLab appear at https://example.com/quantlab/ when the main site is on a
// static host such as GitHub Pages. Requires the domain's DNS to be managed by Cloudflare.
//
// Setup: create a Worker with this code, set ORIGIN below to the subdomain where QuantLab
// actually runs, and add the Worker route  example.com/quantlab*  . Every other path keeps
// going to your normal site untouched.
//
// Not yet run against a live deployment; treat the first use as a test.

const ORIGIN = "https://quantlab.example.com"; // where QuantLab is really served
const PREFIX = "/quantlab";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === PREFIX) {
      return Response.redirect(url.origin + PREFIX + "/" + url.search, 301);
    }
    // Strip the prefix and forward everything else (method, headers, cookies, body) unchanged.
    const target = new URL(url.pathname.slice(PREFIX.length) + url.search, ORIGIN);
    const forwarded = new Request(target, request);
    forwarded.headers.set("X-Forwarded-Proto", "https");
    return fetch(forwarded, { redirect: "manual" });
  },
};
