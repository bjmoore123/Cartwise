// tests/matcher_parity.js — run the browser matcher (frontend/app.js) against the
// shared fixture and compare with the Python scores. Used by test_matcher_parity.py;
// can also be run directly:  node tests/matcher_parity.js
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'frontend', 'app.js'), 'utf8');
const start = app.indexOf('// ── CartWise Matcher (JS port');
const end = app.indexOf('// ── End CartWise Matcher');
if (start < 0 || end < 0) { console.error('matcher markers not found in app.js'); process.exit(2); }
const { cwMatchScore } = new Function(app.slice(start, end) + '; return { cwMatchScore };')();
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'matcher_parity.json'), 'utf8'));
const diffs = [];
for (const c of cases) {
  const js = cwMatchScore(c.item, c.deal);
  if (js !== c.py) diffs.push({ item: c.item, title: c.deal.title, py: c.py, js });
}
console.log(JSON.stringify({ total: cases.length, diffs: diffs.length, examples: diffs.slice(0, 10) }));
process.exit(diffs.length ? 1 : 0);
