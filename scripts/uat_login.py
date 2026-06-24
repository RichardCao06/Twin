#!/usr/bin/env python3
"""uat 编辑器（dataset-web）合规登录脚本：读注入凭证 → 建登录态（Playwright storageState）。

思考/执行分离：登录这种"输密码认证"的敏感动作由本确定性脚本执行——凭证从环境变量注入、
绝不写进代码、不落库；Claude 只加载产出的 storageState 去跑 E2E 验收与判断，全程不接触
明文密码。与 Phase0 代发（Executor/dws-shim 持 token、Claude 不碰 token）同一安全模型。

用法（凭证经 ~/.claude/dws-agent/uat.env 注入，600、不入 git）：
    source ~/.claude/dws-agent/uat.env
    python3 scripts/uat_login.py --verify --out /tmp/uat_A.json --label A

输出：Playwright storageState JSON——cookie `user` 内含 accessToken；前端加载后会自行用该
token 调权限接口补全 TenantId 等。喂给 Playwright（addCookies + goto）即为已登录态。

登录链路事实（dataset-web 源码静态分析，首次真登录时以实测校准）：
  POST {base}{sso}/auth/login   body {username,password,grantType:"PASSWORD"}
  → 响应 data 含 accessToken / userId；前端把整个 data JSON 存进 cookie `user`(path=/)
  → 业务请求拦截器从 cookie.user 取 accessToken 放 Authorization 头、userId 放 userId 头
  → 权限初始化 GET {sso}/user/info/current?productCode=hiq_editor
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def _request(url, method="GET", payload=None, headers=None, timeout=20):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": "<non-json error body>"}


def login(base, sso_prefix, user, password):
    url = base.rstrip("/") + sso_prefix + "/auth/login"
    status, body = _request(url, "POST",
                            {"username": user, "password": password, "grantType": "PASSWORD"})
    if status != 200 or not isinstance(body, dict):
        raise SystemExit("登录失败 HTTP=%s body=%s" % (status, str(body)[:300]))
    if body.get("code") not in (0, 200, None):
        raise SystemExit("登录被拒 code=%s msg=%s" % (body.get("code"), body.get("msg")))
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    token = data.get("accessToken")
    if not token:
        raise SystemExit("登录响应里没拿到 accessToken；data keys=%s"
                         % (list(data.keys()) if isinstance(data, dict) else type(data)))
    return data, token


def build_storage_state(data, base):
    """构造 Playwright storageState：cookie `user`=登录 data 的 JSON（含 accessToken）。

    TenantId（localStorage）不在此预设——前端加载后会用 token 调权限接口自动补全。
    """
    parts = urlsplit(base)
    host = parts.hostname
    origin = "%s://%s" % (parts.scheme, host)
    expires = int(time.time()) + 3 * 24 * 3600  # accessToken 有效期 3 天（uat 清单 3b）
    return {
        "cookies": [{
            "name": "user",
            "value": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            "domain": host, "path": "/", "expires": expires,
            "httpOnly": False, "secure": base.startswith("https"), "sameSite": "Lax",
        }],
        "origins": [{"origin": origin, "localStorage": []}],
    }


def verify_token(base, sso_prefix, token, user_id, product_code):
    url = base.rstrip("/") + sso_prefix + "/user/info/current?productCode=" + product_code
    headers = {"Authorization": token}
    if user_id:
        headers["userId"] = str(user_id)
    status, body = _request(url, "GET", headers=headers)
    ok = status == 200 and isinstance(body, dict) and body.get("code") in (0, 200, None)
    return ok, status, (body.get("code") if isinstance(body, dict) else "?")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="uat 编辑器合规登录 → 输出 Playwright storageState（密码只从环境读、不进 argv）")
    ap.add_argument("--base", default=os.environ.get("UAT_EDITOR_BASE", "https://editor2.hiqdat.dev"))
    ap.add_argument("--sso-prefix", default=os.environ.get("UAT_SSO_PREFIX", "/api/sso"))
    ap.add_argument("--product-code", default=os.environ.get("UAT_PRODUCT_CODE", "hiq_editor"))
    ap.add_argument("--out", help="storageState 输出文件（默认 stdout）；含登录态，放 /tmp、用完删")
    ap.add_argument("--verify", action="store_true", help="登录后调权限接口确认 token 有效")
    ap.add_argument("--label", default="", help="会话标签（顶下线测试区分 A/B，仅打日志）")
    args = ap.parse_args(argv)

    user = os.environ.get("UAT_TEST_USER")
    password = os.environ.get("UAT_TEST_PASSWORD")
    if not user or not password:
        raise SystemExit("缺凭证：经环境变量 UAT_TEST_USER / UAT_TEST_PASSWORD 注入"
                         "（见 ~/.claude/dws-agent/uat.env，600、不入 git）")

    data, token = login(args.base, args.sso_prefix, user, password)
    tag = (" [%s]" % args.label) if args.label else ""
    # 只打长度、不打 token 内容——避免登录态泄进日志。
    print("✅ 登录成功%s：user=%s，accessToken 已获取（%d 字）"
          % (tag, user, len(token)), file=sys.stderr)

    if args.verify:
        uid = data.get("userId") or (data.get("user") or {}).get("id")
        ok, st, code = verify_token(args.base, args.sso_prefix, token, uid, args.product_code)
        print("   token 校验：%s（HTTP=%s code=%s）"
              % ("有效" if ok else "无效", st, code), file=sys.stderr)

    out = json.dumps(build_storage_state(data, args.base), ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        print("   storageState → %s（含 token，用完删）" % args.out, file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
