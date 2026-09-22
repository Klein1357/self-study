/**
 * 补环境沙箱（browser-env）
 *
 * 作用：在纯 Node 进程里，把浏览器专有的全局对象「补」出来，
 *       让目标站的 JS 代码以为自己跑在 Chrome 里。
 *
 * 这不是为了伪造身份，而是为了**复用目标站自己的算法**。
 * 逆向的终极简化：与其用 Python 复刻一段复杂的 JS 加密逻辑，
 * 不如直接让那一段 JS 跑起来。
 *
 * 用法：
 *   const { createEnv } = require('./browser_env.js');
 *   const env = createEnv();          // 补环境
 *   env.run(code);                    // 在补好的环境里跑目标代码
 */

'use strict';

const vm = require('vm');

/** 常见桌面 Chrome 的 UA 模板 */
const UA_CHROME =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

/**
 * 造一个假的 Navigator 对象。
 * @returns {object} 形如浏览器的 navigator
 */
function makeNavigator() {
  const plugins = [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
  ];
  return {
    userAgent: UA_CHROME,
    appName: 'Netscape',
    appVersion: '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    platform: 'Win32',
    language: 'zh-CN',
    languages: ['zh-CN', 'zh', 'en'],
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    vendor: 'Google Inc.',
    productSub: '20030107',
    onLine: true,
    cookieEnabled: true,
    webdriver: false, // ★ 关键：显式声明「我不是自动化工具」
    plugins: plugins,
    mimeTypes: [],
    // 常见的检测钩子：目标站会调用它们看返回是否合理
    javaEnabled: () => false,
    getBattery: () => Promise.resolve({ charging: true, level: 0.98 }),
    sendBeacon: () => true,
  };
}

/**
 * 造一个假的 window（含 screen / location / document 等）。
 * @returns {object} 形如浏览器的 window
 */
function makeWindow() {
  const win = {
    innerWidth: 1920,
    innerHeight: 937,          // 1920x1080 屏减去浏览器 chrome 后的可视高度
    outerWidth: 1920,
    outerHeight: 1040,
    screenX: 0,
    screenY: 0,
    pageXOffset: 0,
    pageYOffset: 0,
    devicePixelRatio: 1,
    screen: {
      width: 1920,
      height: 1080,
      availWidth: 1920,
      availHeight: 1040,
      colorDepth: 24,
      pixelDepth: 24,
    },
    location: {
      href: 'https://example.com/books?page=1',
      protocol: 'https:',
      host: 'example.com',
      hostname: 'example.com',
      port: '',
      pathname: '/books',
      search: '?page=1',
      hash: '',
      origin: 'https://example.com',
    },
    document: {
      referrer: 'https://example.com/',
      title: 'Book Store',
      cookie: '',
      readyState: 'complete',
      documentElement: { clientWidth: 1920, clientHeight: 937 },
    },
    // 目标站常用它做异步打点，缺了会报错
    setTimeout: setTimeout,
    setInterval: setInterval,
    clearTimeout: clearTimeout,
    clearInterval: clearInterval,
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 16),
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    // 属性探测：目标站有时会摸这些
    chrome: { runtime: {} },
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
  };
  return win;
}

/** 极简 localStorage 实现 */
function makeStorage() {
  const store = new Map();
  return {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
    get length() {
      return store.size;
    },
    key: (i) => Array.from(store.keys())[i] ?? null,
  };
}

/**
 * 一个足够用的 MD5 实现（用于替代 CryptoJS.MD5）。
 *
 * ▸ 真实场景请直接 `npm i crypto-js` 并把真正的 CryptoJS 挂进沙箱，
 *   这里手写一份是为了让本课程零依赖可跑。
 *
 * @returns {Function} 接收字符串/WordArray，返回带 toString 的对象
 */
function makeMd5() {
  function md5hex(str) {
    // 使用 Node 内置 crypto 实现，避免手写 200 行
    return require('crypto').createHash('md5').update(str, 'utf8').digest('hex');
  }
  return function MD5(message) {
    const text = typeof message === 'string' ? message : String(message);
    const hex = md5hex(text);
    // CryptoJS 风格：返回值有 toString()，也有 words 等属性
    return {
      toString: () => hex,
      valueOf: () => hex,
      words: [],
      sigBytes: hex.length / 2,
    };
  };
}

/**
 * 创建补好环境的沙箱上下文。
 *
 * @param {object} [overrides] 想额外覆盖的全局变量
 * @returns {{ context: object, run: Function, get: Function }}
 */
function createEnv(overrides = {}) {
  const sandbox = {};
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;          // ★ 关键：window 指向自身
  sandbox.self = sandbox;
  sandbox.navigator = makeNavigator();
  sandbox.document = makeWindow().document;
  sandbox.location = makeWindow().location;
  // 把 window 的全部属性摊到全局（浏览器里 window.x 等价于 x）
  for (const [k, v] of Object.entries(makeWindow())) {
    if (!(k in sandbox)) sandbox[k] = v;
  }
  sandbox.screen = makeWindow().screen;
  sandbox.CryptoJS = { MD5: makeMd5() };
  sandbox.console = console;
  sandbox.JSON = JSON;
  sandbox.Math = Math;
  sandbox.Date = Date;
  sandbox.Intl = Intl;               // ★ 时区检测依赖它
  sandbox.parseInt = parseInt;
  sandbox.parseFloat = parseFloat;
  sandbox.String = String;
  sandbox.Number = Number;
  sandbox.Object = Object;
  sandbox.Array = Array;
  sandbox.Error = Error;
  sandbox.RegExp = RegExp;
  sandbox.encodeURIComponent = encodeURIComponent;
  sandbox.decodeURIComponent = decodeURIComponent;
  sandbox.btoa = sandbox.btoa;
  sandbox.atob = sandbox.atob;

  Object.assign(sandbox, overrides);

  const context = vm.createContext(sandbox);

  /**
   * 在沙箱里执行 JS 代码，返回最后表达式的结果。
   * @param {string} code 要执行的 JS 源码
   * @returns {*} 执行结果
   */
  function run(code) {
    return vm.runInContext(code, context, { timeout: 5000 });
  }

  /**
   * 取沙箱里的某个变量。
   * @param {string} name 变量名
   * @returns {*} 变量值
   */
  function get(name) {
    return vm.runInContext(name, context, { timeout: 5000 });
  }

  return { context, run, get, sandbox };
}

module.exports = {
  createEnv,
  makeNavigator,
  makeWindow,
  makeMd5,
  UA_CHROME,
};
