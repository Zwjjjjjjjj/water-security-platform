/**
 * Capture screenshots of the es-big-screen dashboard views.
 *
 * The app switches pages through the header nav buttons (they set store.mode),
 * not through URL parameters, so we drive the UI instead of navigating.
 *
 * Usage: node tools/capture-screens.mjs [baseUrl] [outDir]
 */
import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const BASE = process.argv[2] ?? 'http://localhost:5175';
const OUT = resolve(process.argv[3] ?? '/tmp/screens');

/** Nav button label -> output file name. */
const VIEWS = [
  { label: '综合态势', name: '01-overview' },
  { label: '水面监视', name: '02-surface' },
  { label: '水下监视', name: '03-underwater' },
  { label: '系统管理', name: '04-system' },
];

mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: '/root/.cache/ms-playwright/chromium-1255/chrome-linux64/chrome',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });

const errors = [];
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(`[console] ${m.text()}`);
});
page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message}`));

await page.goto(`${BASE}/#/screen`, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
await page.waitForTimeout(5000);

for (const v of VIEWS) {
  const btn = page.getByRole('button', { name: v.label, exact: true }).first();
  const count = await btn.count();
  if (count > 0) {
    await btn.click({ timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(4500); // let the 3D scene / charts settle
  } else {
    console.log(`!! button not found: ${v.label}`);
  }
  const file = `${OUT}/${v.name}.png`;
  await page.screenshot({ path: file });
  const mode = await page.evaluate(() => document.querySelector('#screen')?.className ?? '');
  console.log(`saved ${file}   [${mode}]`);
}

const diag = await page.evaluate(() => ({
  title: document.title,
  url: location.href,
  svgs: document.querySelectorAll('svg').length,
  canvases: document.querySelectorAll('canvas').length,
  images: document.querySelectorAll('img').length,
  text: document.body.innerText.replace(/\s+/g, ' ').slice(0, 400),
}));
console.log('\n--- diagnostics ---');
console.log(JSON.stringify(diag, null, 2));
console.log('\n--- errors ---');
console.log(errors.length ? errors.slice(0, 20).join('\n') : '(none)');

await browser.close();
