const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = path.resolve(__dirname, '..');
const sourcePath = path.join(root, 'surfaces', 'study_panel.tsx');
const source = fs.readFileSync(sourcePath, 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    esModuleInterop: true,
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
  fileName: sourcePath,
}).outputText.replace(/\bimport\.meta\b/g, '({})');

let nullStateCount = 0;
const pluginUi = {
  useEffect: () => undefined,
  useRef: (initialValue) => ({ current: initialValue }),
  useState: (initialValue) => {
    let value = typeof initialValue === 'function' ? initialValue() : initialValue;
    if (initialValue === null && ++nullStateCount === 2) {
      value = input.scope;
    }
    return [value, () => undefined];
  },
};
const jsx = (type, props, key) => ({ type, props: props || {}, key });
const jsxRuntime = { Fragment: Symbol('Fragment'), jsx, jsxs: jsx };
const dependencyStub = new Proxy({}, {
  get: (_target, property) => {
    const name = String(property);
    if (name === 'StudyDocumentError') return class StudyDocumentError extends Error {};
    if (name.endsWith('_KINDS')) return [];
    if (name.startsWith('STUDY_DOCUMENT_')) return 0;
    return () => undefined;
  },
});
const localRequire = (request) => {
  if (request === '@neko/plugin-ui') return pluginUi;
  if (request.endsWith('/jsx-runtime')) return jsxRuntime;
  if (request.startsWith('./')) return dependencyStub;
  return require(request);
};

const loaded = { exports: {} };
new Function('require', 'module', 'exports', '__filename', '__dirname', compiled)(
  localRequire,
  loaded,
  loaded.exports,
  sourcePath,
  path.dirname(sourcePath),
);
const StudyPanel = loaded.exports.default;
const translations = input.translations || {};
const tree = StudyPanel({
  api: {},
  host: { origin: '' },
  locale: 'zh-CN',
  plugin: { id: 'study-companion' },
  t: (key) => Object.prototype.hasOwnProperty.call(translations, key) ? translations[key] : key,
});

function children(node) {
  const value = node && node.props ? node.props.children : [];
  return Array.isArray(value) ? value : [value];
}

function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(text).join('');
  return text(children(node));
}

function findScopePath(node) {
  if (!node || typeof node !== 'object') return '';
  const directChildren = children(node);
  if (node.type === 'div') {
    const label = directChildren.find((child) => child && child.type === 'span');
    const value = directChildren.find((child) => child && child.type === 'strong');
    if (text(label) === 'Practice scope' && value) return text(value);
  }
  for (const child of directChildren) {
    const found = findScopePath(child);
    if (found) return found;
  }
  return '';
}

const renderedPath = findScopePath(tree);
if (!renderedPath) throw new Error('Hosted practice scope path was not rendered');
process.stdout.write(JSON.stringify({ path: renderedPath }));
