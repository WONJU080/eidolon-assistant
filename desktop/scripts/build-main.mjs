import esbuild from 'esbuild';

const watch = process.argv.includes('--watch');
const options = {
  entryPoints: { main: 'electron/main.ts', preload: 'electron/preload.ts' },
  outdir: 'dist',
  outExtension: { '.js': '.cjs' },
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'cjs',
  external: ['electron'],
  sourcemap: true,
  logLevel: 'info',
};

if (watch) {
  const context = await esbuild.context(options);
  await context.watch();
  console.log('[build-main] watching electron/*.ts');
} else {
  await esbuild.build(options);
  console.log('[build-main] built dist/main.cjs and dist/preload.cjs');
}
