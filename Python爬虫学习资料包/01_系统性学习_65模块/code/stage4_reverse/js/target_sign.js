/**
 * 模拟一个「被混淆的目标站签名函数」。
 *
 * 特点（模拟真实目标站）：
 *   - 用到了浏览器才有的 API：window、navigator、document、location
 *   - 用到了 CryptoJS 风格的 md5
 *   - 有环境检测：如果 window/navigator 不存在就直接抛错或返回假值
 *
 * 这类代码直接丢进 Node 会立刻报错，这就是「补环境」要解决的问题。
 */

function _0x4f2a(_0x1) {
  // 模拟混淆：字符串数组
  var _0x2b = ['\x73\x69\x67\x6e', '\x74\x73', '\x63\x6c\x69\x65\x6e\x74', '\x26'];
  return _0x2b[_0x1];
}

/**
 * 环境指纹 —— 目标站用来检测「你是不是真的浏览器」
 */
function getEnvFingerprint() {
  // ★ 这些 API 在纯 Node 里全部是 undefined，会直接 TypeError
  var ua = navigator.userAgent;
  var lang = navigator.language;
  var plat = navigator.platform;
  var w = window.innerWidth;
  var h = window.innerHeight;
  var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  var plugins = navigator.plugins.length;

  // 检测 webdriver（Playwright/Selenium 默认会暴露）
  var isBot = !!navigator.webdriver;

  return {
    ua: ua,
    lang: lang,
    platform: plat,
    screen: w + 'x' + h,
    timezone: tz,
    pluginCount: plugins,
    isBot: isBot,
  };
}

/**
 * 真实签名函数 —— 我们要复刻的就是它
 * sign = md5(ts + id + client + envHash + secret)
 */
function buildSign(id, ts) {
  var env = getEnvFingerprint();
  var data = String(ts) + String(id) + 'web' + env.timezone + 's3cr3t_k3y_2026';

  // 目标站用 CryptoJS 做 md5
  return CryptoJS.MD5(data).toString();
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { buildSign: buildSign, getEnvFingerprint: getEnvFingerprint };
}
