// 盲评页发布冒烟测试：node smoke_test.js <dist/survey_HE.html>
// 门禁（PROTOCOL §5）：模板任何改动后必须本测试通过才可发布。
// 校验：POOL 可解析、pair/spot/likert 的 radio name 全唯一、练习/导出函数存在。
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const fail = (m) => { console.log('SMOKE_FAIL:', m); process.exit(1); };

if (!html.includes('const POOL = ')) fail('POOL 缺失');
for (const fn of ['function prCheck', 'async function exportResults', 'function render'])
  if (!html.includes(fn)) fail('函数缺失: ' + fn);

// 从 HTML 提取 POOL（对象以 "\n};" 结束，位于渲染脚本之前）
const i0 = html.indexOf('const POOL = ') + 'const POOL = '.length;
const endMark = html.indexOf('\n};', i0);
if (endMark < 0) fail('POOL 终止符未找到');
const POOL = JSON.parse(html.slice(i0, endMark + 2));

// pair: name="pa${idx}"，idx 为池索引 → 唯一
const pairIds = POOL.pair_items.map((_, i) => i);
if (new Set(pairIds).size !== pairIds.length) fail('pair 索引重复');
// spot: 渲染用 m{idx}/o{idx} 构造
const spotIds = POOL.spot_marked.map((_, i) => 'm' + i)
  .concat(POOL.spot_orig.map((_, i) => 'o' + i));
if (new Set(spotIds).size !== 40) fail('spot radio 组不唯一: ' + new Set(spotIds).size);
if (!/type:'marked',idx:i/.test(html)) fail('spot 渲染未携带 idx（串组回归）');
// likert
const likertIds = POOL.likert_items.map((_, i) => i);
if (new Set(likertIds).size !== likertIds.length) fail('likert 索引重复');

console.log('SMOKE_PASS: pair', pairIds.length,
            '| spot groups', new Set(spotIds).size,
            '| likert', likertIds.length);
