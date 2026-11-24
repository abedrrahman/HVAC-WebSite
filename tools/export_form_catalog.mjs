// Writes core/report_form_catalog.json: the standard report boxes of every equipment type, exactly as the phone app builds them.
// Run after the app's checklists change:  node tools/export_form_catalog.mjs D:\hvac-maintenance-app
// Rows of a compressor, circuit or fan carry "#" where the app puts the number (compressor_#_amps, "Compressor # Amps").
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const app = path.resolve(process.argv[2] || 'D:/hvac-maintenance-app');
const require = createRequire(path.join(app, 'package.json'));
const ts = require('typescript');
const out = fs.mkdtempSync(path.join(os.tmpdir(), 'form-catalog-'));
const files = ['types/index.ts', 'utils/chillerFields.ts', 'constants/company.ts', 'constants/checklists.ts'];
for (const f of files) {
  const src = fs.readFileSync(path.join(app, f), 'utf8');
  let js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  js = js.replace(/require\("@\/([^"]+)"\)/g, (_, p) => `require(${JSON.stringify(path.join(out, p).split(path.sep).join('/'))})`);
  fs.mkdirSync(path.dirname(path.join(out, f)), { recursive: true });
  fs.writeFileSync(path.join(out, f.replace(/\.ts$/, '.js')), js);
}
const c = require(path.join(out, 'constants/checklists.js'));
const TYPES = ['ahu', 'chiller', 'fcu', 'split', 'rtu', 'dehumidifier', 'ccu', 'vrf', 'cooling_tower', 'boiler'];
const cfg = (phase, n) => ({ phase, compressors: n, circuits: n, fans: { supply: n, extract: n, return: n } });
const FAMILY = /^(compressor|circuit|supply_fan|extract_fan|return_fan)_(\d)_/;
// The label of row n=2 differs from n=1 only where the number sits: that place becomes "#".
const pattern = (one, two) => { let s = ''; for (let i = 0; i < one.length; i++) s += one[i] === two[i] ? one[i] : (one[i] === '1' && two[i] === '2' ? '#' : one[i]); return s; };
const catalog = {};
for (const type of TYPES) {
  const rows = [], seen = new Map();
  for (const phase of ['3Ph', '1Ph']) {
    const ones = c.buildChecklist(type, cfg(phase, 1)), twos = new Map(c.buildChecklist(type, cfg(phase, 2)).map((f) => [f.key, f]));
    let before = null; // a row only one phase has goes right after the row it follows in that phase, not at the end
    for (const f of ones) {
      const m = FAMILY.exec(f.key);
      const key = m ? f.key.replace(FAMILY, `${m[1]}_#_`) : f.key;
      const twin = m ? twos.get(f.key.replace(FAMILY, `${m[1]}_2_`)) : null;
      const row = { key, label_en: twin ? pattern(f.labelEn, twin.labelEn) : f.labelEn, label_ar: twin ? pattern(f.labelAr, twin.labelAr) : f.labelAr, group: m ? m[1] : 'unit' };
      if (!seen.has(key)) { seen.set(key, { row, phases: new Set([phase]) }); rows.splice(before ? rows.indexOf(before) + 1 : rows.length, 0, row); }
      else seen.get(key).phases.add(phase);
      before = seen.get(key).row;
    }
  }
  for (const { row, phases } of seen.values()) if (phases.size === 1) row.phase = [...phases][0];
  catalog[type] = rows;
}
const dest = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'core', 'report_form_catalog.json');
fs.writeFileSync(dest, JSON.stringify({ source: 'hvac-maintenance-app constants/checklists.ts', types: catalog }, null, 1));
fs.rmSync(out, { recursive: true, force: true });
console.log('written', dest, Object.fromEntries(Object.entries(catalog).map(([k, v]) => [k, v.length])));
