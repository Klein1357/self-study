// 常驻签名服务：读一行 JSON，回一行 JSON
const readline = require('readline');
const fs = require('fs');
const { createEnv } = require('./browser_env.js');

const env = createEnv();
env.run(fs.readFileSync('target_sign.js', 'utf8'));

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let out;
  try {
    const req = JSON.parse(line);
    const sign = env.run(
      `buildSign(${JSON.stringify(String(req.id))}, ${Number(req.ts)})`
    );
    out = { ok: true, id: req.id, ts: req.ts, sign: sign };
  } catch (e) {
    out = { ok: false, error: e.message };
  }
  process.stdout.write(JSON.stringify(out) + '\n');
});
