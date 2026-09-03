const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = path.resolve(__dirname, '..');
const formatterPath = pathToFileURL(
  path.join(root, 'surfaces', 'practice_scope_display.ts'),
).href;

async function main() {
  const { practiceScopeDisplayPath } = await import(formatterPath);
  const translations = input.translations || {};
  const translate = (key, fallback) => (
    Object.prototype.hasOwnProperty.call(translations, key) ? translations[key] : fallback
  );
  const renderedPath = practiceScopeDisplayPath(input.scope, translate);
  if (!renderedPath) throw new Error('Hosted practice scope path was not rendered');
  process.stdout.write(JSON.stringify({ path: renderedPath }));
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
