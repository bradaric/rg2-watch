#!/usr/bin/env node
/* Real Chromium/DataTables redraw benchmark for a generated operator page directory. */
const http = require("http");
const fs = require("fs");
const path = require("path");

const root = path.resolve(process.argv[2] || "");
const executablePath = process.env.CHROMIUM || "/usr/bin/chromium";
const playwrightPath = process.env.PLAYWRIGHT_CORE || "playwright-core";
if (!root || !fs.existsSync(path.join(root, "index.html"))) {
  console.error("usage: PLAYWRIGHT_CORE=/path/to/playwright-core node benchmark_operator_browser.js PAGE_DIR");
  process.exit(2);
}
const {chromium} = require(playwrightPath);
const mime = {".html": "text/html", ".json": "application/json", ".js": "text/javascript",
  ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml"};
const server = http.createServer((request, response) => {
  const clean = decodeURIComponent(request.url.split("?")[0]);
  if (clean.startsWith("/operator-")) {
    response.writeHead(404);
    return response.end();
  }
  const file = path.join(root, clean === "/" ? "index.html" : clean);
  if (!file.startsWith(root + path.sep)) {
    response.writeHead(403);
    return response.end();
  }
  fs.readFile(file, (error, data) => {
    if (error) {
      response.writeHead(404);
      return response.end();
    }
    response.writeHead(200, {"Content-Type": mime[path.extname(file)] || "application/octet-stream",
      "Cache-Control": "no-store"});
    response.end(data);
  });
});

(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({executablePath, headless: true,
    args: ["--no-sandbox", "--disable-gpu"]});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  await page.goto(`http://127.0.0.1:${server.address().port}/`,
    {waitUntil: "domcontentloaded", timeout: 120000});
  await page.waitForFunction(() => typeof applyFeedGroups === "function" && table && rows.length,
    null, {timeout: 120000});
  const result = await page.evaluate(async () => {
    const output = {}, samples = 30;
    const allTags = [...new Set(rows.map(groupTag))].slice(0, 53);
    const percentile = (values, fraction) => [...values].sort((a, b) => a - b)[
      Math.max(0, Math.ceil(values.length * fraction) - 1)];
    for (const count of [1, 10, 53]) {
      const tags = allTags.slice(0, count), sync = [], painted = [];
      for (let sample = 0; sample < samples; sample++) {
        const revision = 2000000 + count * 1000 + sample;
        const groups = tags.map(tag => {
          const source = rows.filter(row => groupTag(row) === tag);
          return {kind: source[0].group_kind, key: source[0].group_key,
            rows: source.map(row => ({...row, changed_revision: revision,
              benchmark_sample: sample}))};
        });
        const started = performance.now();
        applyFeedGroups(groups, []);
        sync.push(performance.now() - started);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        painted.push(performance.now() - started);
      }
      output[count] = {samples, sync_p50_ms: percentile(sync, .5),
        sync_p95_ms: percentile(sync, .95), next_paint_p50_ms: percentile(painted, .5),
        next_paint_p95_ms: percentile(painted, .95)};
    }
    return {user_agent: navigator.userAgent, rows: rows.length,
      viewport: [innerWidth, innerHeight], redraw: output};
  });
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
  server.close();
})().catch(error => {
  console.error(error);
  server.close();
  process.exit(1);
});
