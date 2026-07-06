"""``dws-agent diagnose``：高频线上问题的诊断 playbook。

    dws-agent diagnose service --host backend2.hiqdat.dev    # 可达性：DNS + 多路径 HTTP + 判断 404 来源

只读探测（DNS + curl），固化今天「backend 404 / 服务不可达」的诊断套路。设计见 docs/复盘-2026-06-23.md。
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys

COMMON_PATHS = ["/", "/login", "/index.html", "/health", "/actuator/health", "/api/sso/auth/login"]


def _curl_bin() -> str:
    return shutil.which("curl") or "/usr/bin/curl"


def _probe(url: str, timeout: int = 8) -> dict:
    """返回 {code, server, router}（HTTP 状态 + server 头 + x-router-code 头）。"""
    try:
        proc = subprocess.run(
            [_curl_bin(), "-s", "-o", "/dev/null",
             "-w", "%{http_code}|%header{server}|%header{x-router-code}",
             "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5, check=False)
        parts = (proc.stdout or "").strip().split("|")
        return {"code": parts[0] if parts else "000",
                "server": parts[1] if len(parts) > 1 else "",
                "router": parts[2] if len(parts) > 2 else ""}
    except Exception:
        return {"code": "000", "server": "", "router": ""}


def cmd_diag_service(args) -> int:
    host = args.host
    print("=== 可达性诊断：%s ===" % host)

    # 1) DNS
    try:
        ip = socket.gethostbyname(host)
        print("  DNS：%s → %s" % (host, ip))
    except Exception as exc:
        print("  ❌ DNS 解析失败：%s —— 域名不存在/解析问题，先查 DNS。" % exc)
        return 0

    # 2) 多路径 HTTP
    paths = [p.strip() for p in args.paths.split(",")] if args.paths else COMMON_PATHS
    print("  路径探测（https://%s<path>）：" % host)
    any_reachable = False
    gateway_404 = False
    for path in paths:
        r = _probe("https://%s%s" % (host, path))
        code, server, router = r["code"], r["server"], r["router"]
        if code[:1] in ("2", "3"):
            any_reachable = True
        if code == "404" and (server in ("elb", "apisix") or server.lower().startswith("apisix") or router):
            gateway_404 = True
        extra = ("server=%s" % server) + (" router-code=%s" % router if router else "")
        flag = " ✓" if code[:1] in ("2", "3") else ""
        print("    %-26s %s  (%s)%s" % (path, code, extra, flag))

    # 3) 判断
    print()
    if any_reachable:
        print("  判断：有路径可达 → 服务在线;个别 404 多为「未配置该路由」或前端 SPA 路径,通常正常。")
    elif gateway_404:
        print("  判断：全部 404 且来自网关/ELB（server=elb/apisix、有 x-router-code）")
        print("        → 多半是**网关/负载均衡的路由未匹配该 host**（不是应用层 404）。")
        print("        建议：① 对比能访问的同类域名（如 backend1）的网关路由配置;")
        print("              ② 确认该 host 是否真的配了转发规则 / 后端服务是否注册。")
    else:
        print("  判断：全部不可达/非网关 404 → 查后端服务是否在线、端口、证书,或网关到后端的转发。")
    return 0


def register_diagnose(subparsers) -> None:
    """把 ``diagnose`` 命令组挂到 ``dws-agent`` 的 add_subparsers 上（懒加载、非致命）。"""
    p = subparsers.add_parser(
        "diagnose",
        help="诊断 playbook：可达性等高频线上问题一键诊断（只读）",
        description="固化高频线上问题的诊断套路（DNS/HTTP 探测、判断 404 来源等）。",
    )
    sub = p.add_subparsers(dest="diag_command", required=True)
    p_svc = sub.add_parser("service", help="服务可达性：DNS + 多路径 HTTP + 判断 404 来源")
    p_svc.add_argument("--host", required=True, help="域名，如 backend2.hiqdat.dev")
    p_svc.add_argument("--paths", help="逗号分隔的探测路径（默认一组常见路径）")
    p_svc.set_defaults(func=cmd_diag_service)
