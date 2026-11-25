import {cp, mkdir, readFile, writeFile} from 'node:fs/promises';
const assets = {};
for (const [file, mime] of [
  ['index.html', 'text/html; charset=utf-8'],
  ['app.js', 'text/javascript; charset=utf-8'],
  ['style.css', 'text/css; charset=utf-8'],
  ['monitor-icon.png', 'image/png'],
]) {
  const path = file === 'index.html' ? '/' : '/static/' + file;
  assets[path] = {mime, base64: (await readFile(new URL('./web/' + file, import.meta.url))).toString('base64')};
}
await mkdir('dist/server', {recursive:true});
await mkdir('dist/.openai', {recursive:true});
await cp('.openai/hosting.json', 'dist/.openai/hosting.json');
await cp('drizzle', 'dist/drizzle', {recursive:true});
const code = await readFile('worker/index.js', 'utf8');
const auth = await readFile('worker/auth.js', 'utf8');
await writeFile('dist/server/index.js', 'const assets=' + JSON.stringify(assets) + ';\n' + auth + '\n' + code);
