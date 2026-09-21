// 前端资源打包脚本（esbuild）。
//
// 不用 Vite：这里只有一个入口和一张样式表，Vite 的依赖树（约 350 MB）
// 换不到什么收益，而 esbuild 自带的开发服务器就够用了。
//
//   node ui/build.mjs           生产构建到 ui/dist
//   node ui/build.mjs --watch   起开发服务器（127.0.0.1:1420），改动即重建

import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import * as esbuild from 'esbuild';

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(here, 'src');
const distDir = resolve(here, 'dist');
const watch = process.argv.includes('--watch');
const port = 1420;

/** @type {import('esbuild').BuildOptions} */
const options = {
  absWorkingDir: srcDir,
  entryPoints: ['main.ts'],
  bundle: true,
  outfile: resolve(distDir, 'main.js'),
  format: 'esm',
  target: ['es2022'],
  sourcemap: watch ? 'inline' : false,
  minify: !watch,
  logLevel: 'info',
};

mkdirSync(distDir, { recursive: true });
copyFileSync(resolve(here, 'index.html'), resolve(distDir, 'index.html'));

if (watch) {
  const context = await esbuild.context(options);
  await context.watch();
  const server = await context.serve({ host: '127.0.0.1', port, servedir: distDir });
  console.log(`前端开发服务器: http://127.0.0.1:${server.port}`);
} else {
  await esbuild.build(options);
  console.log('前端已构建到 ui/dist');
}
