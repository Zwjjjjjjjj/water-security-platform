/**
 * Diagnose the screen scaling behaviour.
 * Usage: node tools/diag-scale.mjs [baseUrl]
 */
import { chromium } from 'playwright';

const BASE = process.argv[2] ?? 'http://localhost:5175';

const browser = await chromium.launch({
  executablePath: '/root/.cache/ms-playwright/chromium-1255/chrome-linux64/chrome',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
});

for (const vp of [
  { width: 1920, height: 1080 },
  { width: 1600, height: 900 },
  { width: 2560, height: 1440 },
]) {
  const page = await browser.newPage({ viewport: vp });
  await page.goto(`${BASE}/#/screen`, { waitUntil: 'networkidle', timeout: 45000 }).catch(() => {});
  await page.waitForTimeout(3500);
  const d = await page.evaluate(() => {
    const s = document.getElementById('screen');
    const body = document.body;
    const grid = document.querySelector('.ws-grid');
    const gs = grid ? getComputedStyle(grid) : null;
    return {
      innerW: innerWidth,
      innerH: innerHeight,
      bodyClientW: body.clientWidth,
      bodyClientH: body.clientHeight,
      screenTransform: s ? s.style.transform : null,
      screenRect: s ? s.getBoundingClientRect().toJSON() : null,
      gridCols: gs ? gs.gridTemplateColumns : null,
      gridRows: gs ? gs.gridTemplateRows : null,
      gridRect: grid ? grid.getBoundingClientRect().toJSON() : null,
    };
  });
  console.log(`\n=== viewport ${vp.width}x${vp.height} ===`);
  console.log(JSON.stringify(d, null, 2));
  await page.close();
}

await browser.close();
