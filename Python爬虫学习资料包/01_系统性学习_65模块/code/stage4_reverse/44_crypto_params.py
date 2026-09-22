"""阶段 4 · 第 5 课：加密参数深度分析（MD5 / AES / RSA）

本课 100% 可实测 —— pycryptodome 3.23.0 与 cryptography 50.0.1 均已安装。

本课目标
--------
逆向到加密参数后，你要能在 Python 里『一比一复刻』出同样的结果。
复刻成功的唯一标准是：本地算出的 sign == 抓包看到的 sign。

覆盖内容
  1. MD5 及其变体（加盐 / 排序 / 大小写 / 多次哈希）
  2. AES 的四种关键参数（模式 / 填充 / IV / 密钥/输出编码），
     其中任何一项猜错，结果就完全不同 —— 本课用 8 种组合实测给你看
  3. RSA 公钥加密（爬虫中最常见的 RSA 用法）
  4. 签名对齐验证器：本地算 vs 目标值逐位比对

运行：python3 44_crypto_params.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Literal

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SEP = "=" * 72


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


# ==========================================================================
# 一、MD5 家族：爬虫签名的主力
# ==========================================================================


def md5_hash(data: str) -> str:
    """标准 MD5，返回 32 位小写十六进制。

    Args:
        data: 待哈希字符串。

    Returns:
        32 位小写 hex 字符串。
    """
    return hashlib.md5(data.encode("utf-8")).hexdigest()


def md5_upper(data: str) -> str:
    """MD5 大写变体（服务端有时要求大写）。"""
    return md5_hash(data).upper()


def md5_16(data: str) -> str:
    """16 位 MD5（截取 32 位的第 8-24 个字符）。

    ▸ 很多老系统叫它『加密』，实际只是 32 位去头去尾。
    """
    return md5_hash(data)[8:24]


def md5_double(data: str) -> str:
    """二次 MD5：md5(md5(x))。"""
    return md5_hash(md5_hash(data))


def build_sorted_sign(params: dict[str, Any], secret: str = "") -> str:
    """最常见的企业级签名：按 key 升序拼接 + 末尾加盐。

    这是国内接口最流行的签名方式，记住这个模板能覆盖大量场景。

    Args:
        params: 业务参数。
        secret: 盐值。

    Returns:
        MD5 签名。
    """
    items = sorted((k, v) for k, v in params.items() if v is not None and v != "")
    raw = "&".join(f"{k}={v}" for k, v in items)
    if secret:
        raw += f"&key={secret}"
    return md5_hash(raw)


def build_hmac_sign(params: dict[str, Any], secret: str) -> str:
    """HMAC-MD5 签名（比裸 MD5 更安全，服务端不需要存原文）。"""
    items = sorted((k, v) for k, v in params.items())
    raw = "&".join(f"{k}={v}" for k, v in items)
    return hmac.new(secret.encode(), raw.encode(), hashlib.md5).hexdigest()


# ==========================================================================
# 二、AES：参数最易猜错的地方
# ==========================================================================

AesMode = Literal["ECB", "CBC", "CFB", "OFB", "CTR"]
AesPad = Literal["pkcs7", "zero", "none"]


@dataclass
class AesSpec:
    """AES 参数全集 —— 任何一个不对，密文就完全不一样。"""

    key: bytes
    mode: AesMode = "CBC"
    iv: bytes | None = None
    pad: AesPad = "pkcs7"
    out: Literal["hex", "b64"] = "hex"

    def describe(self) -> str:
        iv_s = self.iv.hex() if self.iv else "(无)"
        return f"AES-{len(self.key) * 8}/{self.mode}/PKCS7? key={self.key.hex()[:16]}… iv={iv_s} out={self.out}"


def aes_encrypt(plain: str, spec: AesSpec) -> str:
    """按给定的参数组合加密，用于逐项对比验证。

    Args:
        plain: 明文。
        spec: AES 参数组合。

    Returns:
        hex 或 base64 编码的密文。

    Raises:
        ValueError: 模式不支持或 IV 缺失/长度错误。
    """
    data = plain.encode("utf-8")
    if spec.mode == "ECB":
        cipher = AES.new(spec.key, AES.MODE_ECB)
        mode_obj = cipher
        iv = None
    else:
        if spec.iv is None:
            raise ValueError(f"{spec.mode} 模式必须提供 IV")
        if len(spec.iv) != 16:
            raise ValueError(f"IV 长度必须是 16 字节，当前 {len(spec.iv)}")
        mode_enum = {
            "CBC": AES.MODE_CBC,
            "CFB": AES.MODE_CFB,
            "OFB": AES.MODE_OFB,
            "CTR": AES.MODE_CTR,
        }[spec.mode]
        if spec.mode == "CTR":
            from Crypto.Util import Counter

            ctr = Counter.new(128, initial_value=int.from_bytes(spec.iv, "big"))
            mode_obj = AES.new(spec.key, mode_enum, counter=ctr)
        else:
            mode_obj = AES.new(spec.key, mode_enum, iv=spec.iv)
        iv = spec.iv
    _ = iv

    if spec.pad == "pkcs7":
        # CBC/ECB 需要对齐到 16 字节；流模式本来就不需要
        if spec.mode in ("ECB", "CBC"):
            data = pad(data, AES.block_size)
    elif spec.pad == "zero":
        if spec.mode in ("ECB", "CBC"):
            data = data + b"\x00" * (-len(data) % 16)
    else:
        if spec.mode in ("ECB", "CBC") and len(data) % 16:
            raise ValueError("pad='none' 但明文长度未对齐 16 字节")

    ct = mode_obj.encrypt(data)
    return ct.hex() if spec.out == "hex" else base64.b64encode(ct).decode()


def aes_decrypt(ct: str, spec: AesSpec) -> str:
    """对应的解密，用于验证互通。

    Args:
        ct: hex 或 base64 密文。
        spec: 与加密一致参数组合。

    Returns:
        解密后的明文。
    """
    raw = bytes.fromhex(ct) if spec.out == "hex" else base64.b64decode(ct)
    if spec.mode == "ECB":
        obj = AES.new(spec.key, AES.MODE_ECB)
    elif spec.mode == "CBC":
        obj = AES.new(spec.key, AES.MODE_CBC, iv=spec.iv)
    elif spec.mode == "CFB":
        obj = AES.new(spec.key, AES.MODE_CFB, iv=spec.iv)
    elif spec.mode == "OFB":
        obj = AES.new(spec.key, AES.MODE_OFB, iv=spec.iv)
    else:
        from Crypto.Util import Counter

        ctr = Counter.new(128, initial_value=int.from_bytes(spec.iv or b"", "big"))
        obj = AES.new(spec.key, AES.MODE_CTR, counter=ctr)
    pt = obj.decrypt(raw)
    if spec.pad == "pkcs7" and spec.mode in ("ECB", "CBC"):
        pt = unpad(pt, AES.block_size)
    elif spec.pad == "zero":
        pt = pt.rstrip(b"\x00")
    return pt.decode("utf-8", errors="replace")


def aes_gcm_encrypt(plain: str, key: bytes, nonce: bytes) -> tuple[str, str]:
    """AES-GCM：现代接口常用，带认证标签。

    Args:
        plain: 明文。
        key: 32 字节密钥。
        nonce: 12 字节随机数（同一密钥下绝不能重复）。

    Returns:
        (密文 hex, tag hex)。
    """
    ct = AESGCM(key).encrypt(nonce, plain.encode(), None)
    return ct[:-16].hex(), ct[-16:].hex()


# ==========================================================================
# 三、RSA：爬虫里基本只用『公钥加密』
# ==========================================================================


def rsa_demo() -> dict[str, Any]:
    """生成一对 RSA 密钥，演示最常见的爬虫 RSA 用法。

    ▸ 爬虫场景：登录页把密码用服务端下发的**公钥**加密后提交。
      你只需要公钥 → 用 PKCS#1 v1.5 或 OAEP 加密即可，
      **不需要私钥**（也拿不到）。

    Returns:
        包含公钥/私钥 PEM 与加密结果的字典。
    """
    key = RSA.generate(2048)
    pub_pem = key.publickey().export_key().decode()
    priv_pem = key.export_key().decode()

    password = "MyP@ssw0rd_2026"
    cipher = PKCS1_v1_5.new(key.publickey())
    ct = cipher.encrypt(password.encode())
    ct_b64 = base64.b64encode(ct).decode()

    # 服务端视角解密验证
    sentinel = b"__DECRYPT_FAILED__"
    plain = PKCS1_v1_5.new(key).decrypt(base64.b64decode(ct_b64), sentinel)

    return {
        "pub_pem_head": pub_pem.splitlines()[1][:48] + "…",
        "priv_pem_head": priv_pem.splitlines()[1][:48] + "…",
        "plain": password,
        "ct_len": len(ct),
        "ct_b64_head": ct_b64[:60] + "…",
        "decrypted": plain.decode(),
        "ok": plain.decode() == password,
    }


def extract_rsa_pubkey_from_js(js_modulus_hex: str, exponent: int = 65537) -> str:
    """从 JS 里常见的 n/e 还原出 PEM 公钥。

    ▸ 很多站点在 JS 里硬编码 `setPublicKey("A1B2C3…", "10001")`，
      第一个是模数 n（hex），第二个是指数 e（hex，10001 = 65537）。
      拿到这两个值就能在 Python 里还原公钥。

    Args:
        js_modulus_hex: 模数 n 的十六进制字符串。
        exponent: 指数 e 的十进制值。

    Returns:
        PEM 格式公钥字符串。
    """
    n = int(js_modulus_hex, 16)
    pub = RSA.construct((n, exponent))
    return pub.export_key().decode()


# ==========================================================================
# 四、签名对齐验证器
# ==========================================================================


@dataclass
class AlignResult:
    """一次对齐验证的结果。"""

    name: str
    local: str
    remote: str

    @property
    def ok(self) -> bool:
        return self.local == self.remote

    def render(self) -> str:
        flag = "✓ 一致" if self.ok else "✗ 不一致"
        lines = [f"    {flag}  [{self.name}]"]
        lines.append(f"        本地: {self.local}")
        lines.append(f"        远端: {self.remote}")
        if not self.ok:
            diff = first_diff(self.local, self.remote)
            lines.append(f"        首个不同位置: 第 {diff + 1} 个字符")
        return "\n".join(lines)


def first_diff(a: str, b: str) -> int:
    """返回两个字符串第一个不同的下标，完全相同返回 -1。"""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return -1 if len(a) == len(b) else min(len(a), len(b))


# ==========================================================================
# 实验区
# ==========================================================================

def exp1_md5_family() -> None:
    title("【实验 1】MD5 家族 —— 变体只差一点，结果天差地别")
    base = "id=1001&ts=1699999999"

    rows = [
        ("标准 32 位小写", md5_hash(base)),
        ("32 位大写", md5_upper(base)),
        ("16 位（截取 8:24）", md5_16(base)),
        ("二次哈希 md5(md5(x))", md5_double(base)),
        ("加盐（固定 salt）", md5_hash(base + "&key=abc123")),
        ("加盐（前置 salt）", md5_hash("abc123" + base)),
    ]
    print(f"    原始串：{base}\n")
    print(f"    {'变体':<22}{'结果'}")
    print("    " + "-" * 90)
    for name, val in rows:
        print(f"    {name:<22}{val}")

    sub("关键对比：加密 vs 哈希")
    print("    MD5 是**哈希**（单向不可逆），不是加密。")
    print("    服务端存的是哈希值，比对时也是比哈希 —— 所以你不需要『解密』，")
    print("    只需要保证『同样的输入得到同样的哈希』。这就是签名复刻的全部要求。")

    sub("企业级排序签名（覆盖国内大部分接口）")
    params = {"userId": 10086, "page": 1, "size": 20, "nonce": "abc"}
    print(f"    参数：{params}")
    print(f"    排序后拼接（不含盐）：{build_sorted_sign(params, '')}")
    print(f"    排序后拼接（含盐 secret='s3cr3t'）：{build_sorted_sign(params, 's3cr3t')}")
    print("\n    ▸ 拼接口诀：**过滤空值 → 按 key 升序 → k=v 用 & 连接 → 尾部接 &key=盐**")
    print("      这 4 步能覆盖 60%+ 的国内 API 签名。")

    sub("HMAC-MD5（更安全，服务端不存原文）")
    print(f"    结果：{build_hmac_sign(params, 's3cr3t')}")
    print("    ▸ 区别：HMAC 把 key 作为哈希算法的参数，不是简单拼接。")
    print("      一个坑：拼接顺序仍是签名的关键，必须先确认服务端的排序规则。")


def exp2_aes_params() -> None:
    title("【实验 2】AES 参数矩阵 —— 猜错一个就全错（8 种组合实测）")
    key = b"0123456789abcdef"   # 16 字节 → AES-128
    iv = b"abcdef0123456789"    # 16 字节
    plain = '{"userId":10086,"pwd":"e10adc3949ba59abbe56e057f20f883e"}'
    print(f"    密钥 key = {key.decode()!r}（16 字节 → AES-128）")
    print(f"    初始向量 iv = {iv.decode()!r}（16 字节）")
    print(f"    明文 = {plain}\n")

    cases: list[tuple[str, AesSpec]] = [
        ("CBC + PKCS7 + hex", AesSpec(key=key, mode="CBC", iv=iv, pad="pkcs7", out="hex")),
        ("CBC + PKCS7 + base64", AesSpec(key=key, mode="CBC", iv=iv, pad="pkcs7", out="b64")),
        ("CBC + zero 填充", AesSpec(key=key, mode="CBC", iv=iv, pad="zero", out="hex")),
        ("ECB + PKCS7", AesSpec(key=key, mode="ECB", pad="pkcs7", out="hex")),
        ("CFB（流模式，无需填充）", AesSpec(key=key, mode="CFB", iv=iv, pad="none", out="hex")),
        ("OFB（流模式）", AesSpec(key=key, mode="OFB", iv=iv, pad="none", out="hex")),
        ("CTR（流模式）", AesSpec(key=key, mode="CTR", iv=iv, pad="none", out="hex")),
        ("CBC + 另一个 IV", AesSpec(key=key, mode="CBC", iv=b"9999999999999999", pad="pkcs7", out="hex")),
    ]

    results: dict[str, str] = {}
    print(f"    {'参数组合':<26}{'密文（前 48 字符）'}")
    print("    " + "-" * 90)
    for name, spec in cases:
        ct = aes_encrypt(plain, spec)
        results[name] = ct
        print(f"    {name:<26}{ct[:48]}…")

    sub("验证：密文长度差异说明了什么")
    for name, ct in results.items():
        out = "base64" if "base64" in name else "hex"
        raw_len = len(base64.b64decode(ct)) if out == "base64" else len(bytes.fromhex(ct))
        print(f"    {name:<26}密文长度 = {raw_len} 字节"
              f"{'  ← 补足到 16 的倍数（分组填充）' if name.startswith(('CBC', 'ECB')) else '  ← 流模式等长'}")

    sub("互通性验证（加密 → 解密必须还原）")
    for name, spec in cases[:3]:
        ct = aes_encrypt(plain, spec)
        back = aes_decrypt(ct, spec)
        ok = "✓" if back == plain else "✗"
        print(f"    {ok} {name:<26}还原成功")

    sub("★ 逆向启示：AES 参数排查顺序")
    print("""    1. 看密文长度 —— 若是流模式（CFB/OFB/CTR）则长度=明文长度
       若是分组模式（CBC/ECB）则长度是 16 的倍数
    2. 搜 JS 关键字：CryptoJS.AES.encrypt / createCipheriv / mode: / padding:
    3. 常见默认值依次排查：
         mode   默认 CBC（也有 ECB）
         padding 默认 Pkcs7（JS 里 CryptoJS.pad.Pkcs7）
         iv      常硬编码在 JS 里，或由时间戳派生
         out     CryptoJS 默认输出 base64 字符串
    4. 有 CryptoJS 特征就直接上 45 课『补环境』跑真实 JS，别猜了""")

    sub("AES-GCM（现代接口）")
    gkey = b"0123456789abcdef0123456789abcdef"  # 32 字节
    nonce = b"unique_nonce"  # 12 字节
    ct_hex, tag = aes_gcm_encrypt(plain, gkey, nonce)
    print(f"    密文 = {ct_hex[:48]}…")
    print(f"    tag  = {tag}")
    print("    ▸ GCM 自带完整性校验（tag），改一个 bit 解密就失败。")
    print("      爬虫要点：nonce 绝不能重复，且通常随请求随机生成后随密文一起发。")


def exp3_rsa() -> None:
    title("【实验 3】RSA —— 爬虫只需要公钥")
    print("    正在生成 2048 位密钥对（约 1 秒）…")
    r = rsa_demo()
    print(f"\n    公钥（PEM 首行）: {r['pub_pem_head']}")
    print(f"    私钥（PEM 首行）: {r['priv_pem_head']}")
    print("\n    ▸ 注意：爬虫场景下你**只拿得到公钥**，拿不到私钥。")
    print("      公钥加密 → 服务端用私钥解密，这是不可逆的单向设计，")
    print("      所以 RSA 参数不需要『逆向出原文』，只需要『复刻加密过程』。")

    print(f"\n    明文密码: {r['plain']}")
    print(f"    密文长度: {r['ct_len']} 字节（2048 位密钥 = 256 字节）")
    print(f"    密文(b64): {r['ct_b64_head']}")
    print(f"    服务端解密: {r['decrypted']}")
    print(f"    验证: {'✓ 一致' if r['ok'] else '✗ 不一致'}")

    sub("RSA 在 JS 里的三种常见写法与对应 Python 方案")
    print("""    ┌──────────────────────────┬────────────────────────────────┐
    │ JS 写法                   │ Python 复刻方案                 │
    ├──────────────────────────┼────────────────────────────────┤
    │ JSEncrypt (PKCS#1 v1.5)  │ PKCS1_v1_5.new(pub).encrypt()   │
    │ jsencrypt + OAEP         │ PKCS1_OAEP.new(pub).encrypt()   │
    │ setPublicKey(n, e)       │ RSA.construct((int(n,16), e))   │
    └──────────────────────────┴────────────────────────────────┘""")

    sub("★ 关键陷阱：RSA 加密结果每次都不一样！")
    print("""    PKCS#1 v1.5 和 OAEP 都带**随机填充**，所以同样的明文
    每次加密出来的密文都不同。这带来两个后果：

      ✗ 不能用『密文相等』来判断算法是否正确
      ✓ 应该用『服务端能否解密成功』来验证
      ✓ 或者本地加密后用私钥解密，比对明文（仅当你有私钥，测试时可用）

    调试建议：本地用 RSA.generate() 生成一对密钥自测，
    确认加密-解密链路通畅后，再换成目标站的真实公钥。""")


def exp4_alignment() -> None:
    title("【实验 4】签名对齐验证器 —— 逐位比对定位差异")
    SECRET = "s3cr3t_k3y"
    params = {"userId": "10086", "page": "1", "ts": "1699999999"}

    # 假设这是抓包拿到的服务端签名（正确值）
    remote_sign = build_sorted_sign(params, SECRET)

    # 模拟几种常见的『猜错』情形
    tests = [
        ("正确：升序 + 盐在尾部", build_sorted_sign(params, SECRET)),
        ("错误：没有盐", build_sorted_sign(params, "")),
        ("错误：盐在前部", md5_hash(SECRET + "&" + "&".join(f"{k}={v}" for k, v in sorted(params.items())))),
        ("错误：未排序（原顺序）", md5_hash("&".join(f"{k}={v}" for k, v in params.items()) + f"&key={SECRET}")),
        ("错误：大写输出", build_sorted_sign(params, SECRET).upper()),
        ("错误：16 位截断", build_sorted_sign(params, SECRET)[8:24]),
    ]

    print(f"    目标站签名: {remote_sign}\n")
    for name, local in tests:
        res = AlignResult(name=name, local=local, remote=remote_sign)
        print(res.render())

    sub("★ 对齐失败时的排查清单")
    print("""    1. 字符集 —— 中文参数是 utf-8 还是 gbk？URL 编码了吗？
    2. 空值处理 —— 服务端是跳过空值，还是保留 k= ？
    3. 数字类型 —— 10086 vs "10086"（JSON 里数字，拼串时是否带引号）
    4. 时间戳位数 —— 秒（10 位）vs 毫秒（13 位）
    5. 排序规则 —— 升序 / 降序 / 按插入顺序 / 大小写敏感排序
       ▸ JS 的 sort() 默认按 UTF-16 码位，Python 的 sorted() 也按码位，
         但对**大写字母**的处理两者一致（都排在小写前），这点可放心
    6. 盐的位置与格式 —— key= 前缀？直接拼接？放在最前？
    7. 多次哈希 —— 有些站点 md5(md5(x))
    8. 输出格式 —— 小写 / 大写 / 16 位""")

    sub("实用技巧：用二分法定位差异")
    print("""    如果本地与远端签名**前 20 位相同但后面不同**，说明算法对但
    某个参数值不同 —— 优先怀疑时间戳/nonce。
    如果**第 1 位就不同**，说明算法本身错了 —— 优先怀疑排序和盐。

    本课的对齐验证器输出的『首个不同位置』就是干这个用的。""")


def main() -> None:
    print(SEP)
    print("阶段 4 · 第 5 课：加密参数深度分析")
    print(SEP)
    print("""
逆向最后一步是让 Python 算出和目标站一模一样的值。
本课用真实加密库（pycryptodome / cryptography）实测四种主流算法。
""")
    exp1_md5_family()
    exp2_aes_params()
    exp3_rsa()
    exp4_alignment()

    title("本课小结")
    print("""
  ✓ MD5 是哈希不是加密 —— 你只需保证『同输入同输出』
  ✓ 排序拼接签名模板：过滤空值 → 升序 → k=v 连接 → 尾部接盐
  ✓ AES 四要素：模式 / 填充 / IV / 输出编码，猜错一个就全错
  ✓ 看密文长度能快速判断分组模式 vs 流模式
  ✓ RSA 只需要公钥；带随机填充，密文每次不同，别用密文比对
  ✓ 对齐验证器 + 手动二分法 是排查差异的标准动作

  下一课（45）：JS 补环境 —— 当算法是 AES+nonce 且无法枚举时，
              如何用 Node.js 直接执行目标 JS 片段。
""")


if __name__ == "__main__":
    main()
