#!/usr/bin/env python3
"""KubeSphere 生产日志「只读」查询通道（经 ks-apiserver 的 k8s log API）。

为什么走这条：
  - kubectl 直连 k8s apiserver 走原生 RBAC，richardcao06 证书没有 pods/log 权限（被挡）。
  - ks-apiserver（KubeSphere console 代理）用「KubeSphere 登录态 token」认证，能看日志（与 UI 同源）。
  - 所以拿浏览器里的 KubeSphere token，就能 curl/HTTP 这个 log API 查日志，纯只读。

用法（你只需给 服务名 + token，找错误再加 --grep）：
  python3 scripts/ks_logs.py --service dataset --token "<浏览器copy的token>"
  python3 scripts/ks_logs.py --service dataset --token "<token>" --grep "导出失败|ILCD 导出任务执行失败|下载H5文件失败"
  python3 scripts/ks_logs.py --pod dataset-59955fdfbc-nk66l --token "<token>" --grep "Exception" --context 12

token：KubeSphere 网页 → F12/Application → Cookie 里的 `token`（约 2 小时过期）。只从命令行传入、本脚本不写盘。
只读：仅 GET pods / pods/log，绝不写。
"""
import argparse, json, re, ssl, sys, urllib.parse, urllib.request

# 该环境固定值（如换集群/项目，改这里或加 --base/--ns）
BASE = "https://k.hiqlcd.com/clusters/host/api/v1"
DEFAULT_NS = "hiqlcd-app-prod"


def _get(url, token, timeout=45):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # ks 内部证书，跳过校验（等价 curl -k）；只读查日志
    req = urllib.request.Request(url, headers={"Cookie": f"token={token}"})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def list_pods(ns, service, token):
    body = _get(f"{BASE}/namespaces/{ns}/pods?limit=500", token)
    names = [it["metadata"]["name"] for it in json.loads(body).get("items", [])]
    # 精确匹配 deployment 名：pod = <deploy>-<rsHash>-<podHash>，去掉末两段
    matched = [n for n in names if n.rsplit("-", 2)[0] == service]
    if not matched:  # 退回前缀匹配
        matched = [n for n in names if n.startswith(service + "-")]
    return matched


def get_log(ns, pod, container, tail, token, since=None):
    q = {"container": container, "tailLines": tail, "timestamps": "true", "follow": "false"}
    if since:
        q["sinceSeconds"] = since
    return _get(f"{BASE}/namespaces/{ns}/pods/{pod}/log?{urllib.parse.urlencode(q)}", token)


def main():
    ap = argparse.ArgumentParser(description="KubeSphere 生产日志只读查询（经 ks-apiserver）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--service", help="服务名(=deployment 名，如 dataset / backend / square-web-next)，自动找其所有 pod")
    g.add_argument("--pod", help="直接指定单个 pod 名")
    ap.add_argument("--token", required=True, help="KubeSphere 浏览器登录 token（约 2h 过期）")
    ap.add_argument("--grep", help="过滤正则（不给则打印各 pod 尾部）")
    ap.add_argument("--ns", default=DEFAULT_NS)
    ap.add_argument("--container", help="容器名（默认=服务名）")
    ap.add_argument("--tail", default="200000", help="拉多少行（默认 20 万，够覆盖整个 pod 生命周期）")
    ap.add_argument("--since", help="只看最近 N 秒（如 3600=最近 1 小时）")
    ap.add_argument("--context", type=int, default=0, help="命中行后追加 N 行（看异常堆栈）")
    ap.add_argument("--tail-lines-out", type=int, default=25, help="不 grep 时每个 pod 打印的尾部行数")
    a = ap.parse_args()

    if a.pod:
        pods = [a.pod]
        service = a.pod.rsplit("-", 2)[0]
    else:
        service = a.service
        try:
            pods = list_pods(a.ns, service, a.token)
        except Exception as e:
            sys.exit(f"[ERR] list pods 失败（若 401 = token 过期，去网页重新 copy）：{e}")
        if not pods:
            sys.exit(f"[ERR] ns={a.ns} 未找到服务「{service}」的 pod")
    container = a.container or service
    print(f"# ns={a.ns}  服务={service}  容器={container}  pods={pods}", file=sys.stderr)

    pat = re.compile(a.grep) if a.grep else None
    for pod in pods:
        try:
            log = get_log(a.ns, pod, container, a.tail, a.token, a.since)
        except Exception as e:
            print(f"## {pod}: 取日志失败（{e}）", file=sys.stderr)
            continue
        lines = log.split("\n")
        if pat:
            idxs = [i for i, l in enumerate(lines) if pat.search(l)]
            print(f"\n## {pod}  共 {len(lines)} 行  命中 {len(idxs)} 行")
            last = -1
            for i in idxs:
                for j in range(i, min(i + 1 + a.context, len(lines))):
                    if j <= last:
                        continue
                    print(lines[j])
                    last = j
        else:
            print(f"\n## {pod}  共 {len(lines)} 行  尾 {a.tail_lines_out} 行")
            for l in lines[-a.tail_lines_out:]:
                print(l)


if __name__ == "__main__":
    main()
