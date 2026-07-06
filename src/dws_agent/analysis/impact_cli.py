"""``dws-agent impact``：改共享组件前的影响面体检。

    dws-agent impact --component sso        # 预设：SSO 的实现方 + 所有接入方 + 风险
    dws-agent impact --pattern is-concurrent # 通用：grep 所有仓，列命中方

确定性 grep（跨 ~/Workspace 各仓），比语义检索准。设计见 docs/复盘-2026-06-23.md。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, List

DEFAULT_ROOT = os.path.expanduser("~/Workspace")
_EXCLUDES = ["--exclude-dir=node_modules", "--exclude-dir=.git", "--exclude-dir=target",
             "--exclude-dir=dist", "--exclude-dir=build", "--exclude-dir=.next",
             "--exclude-dir=.claude", "--exclude-dir=.history"]  # 排除 worker worktree 副本 / 编辑器本地历史
# 只扫代码 + 配置文件，跳过二进制/锁文件/资源，避免大仓 grep 过慢
_INCLUDES = ["--include=*.java", "--include=*.yml", "--include=*.yaml", "--include=*.ts",
             "--include=*.tsx", "--include=*.js", "--include=*.jsx", "--include=*.vue",
             "--include=*.json", "--include=*.properties", "--include=*.xml",
             "--include=*.env", "--include=.env*", "--include=*.md"]

# 预设共享组件：改它要看「谁依赖 + 风险」。impl=实现/含该组件；consumer=接入/消费方。
SHARED_COMPONENTS: Dict[str, dict] = {
    "sso": {
        "desc": "SSO 登录鉴权（dataset-sso）",
        "impl": [r"sa-token", r"is-concurrent", r"StpUtil", r"SaLoginModel", r"LoginDeviceResolver"],
        "consumer": [r"/api/sso", r"ssoUrl", r"SsoApiService", r"X-Product-Code", r"productCode"],
        "risk": "改登录行为（is-concurrent / device / token 校验）会影响所有接入 SSO 的系统；"
                "尤其单设备登录（is-concurrent=false）若不按 client 区分 device，会跨系统互踢（参 feedback#24）。",
    },
    "redis": {
        "desc": "Redis（会话/缓存；注意 Sa-Token alone-redis 是否与业务 Redis 共享）",
        "impl": [r"alone-redis", r"spring\.redis", r"RedisTemplate", r"[Rr]edisson"],
        "consumer": [r"REDIS_HOST", r"SATOKEN_REDIS", r"redis://"],
        "risk": "多个服务若连同一 Redis 同库 + 同 key 前缀，会话/键可能串台；改 key 策略/清库/改 token-name 前先确认隔离。",
    },
    "kafka": {
        "desc": "Kafka 事件消息",
        "impl": [r"\bkafka\b", r"KafkaTemplate", r"@KafkaListener"],
        "consumer": [r"KAFKA_URL", r"bootstrap-servers", r"[Tt]opic"],
        "risk": "改 topic 名/分区/消费组会影响所有生产/消费方；注意 topic 是否按环境加前缀。",
    },
    "gateway": {
        "desc": "API 网关（APISIX）路由",
        "impl": [r"apisix", r"x-router-code"],
        "consumer": [r"/api/backend", r"/api/sso", r"/api/dataset", r"/api/data-convertor"],
        "risk": "改路由/host 规则会影响对应前端的可达性（404）；改前确认各前端的 API 前缀映射。",
    },
}


def _list_repos(root: str) -> List[str]:
    out: List[str] = []
    try:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if os.path.isdir(p) and not name.startswith("."):
                out.append(p)
    except OSError:
        pass
    return out


def _grep_repo(repo: str, patterns: List[str]) -> List[str]:
    """返回 repo 内命中任一 pattern 的文件（相对仓路径）。"""
    pat = "|".join("(%s)" % p for p in patterns)
    argv = ["grep", "-rlE", *_INCLUDES, *_EXCLUDES, pat, repo]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=90, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return []
    files = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return [os.path.relpath(f, repo) for f in files]


def scan(patterns: List[str], root: str = DEFAULT_ROOT) -> Dict[str, List[str]]:
    """跨 root 各仓 grep patterns，返回 {repo_name: [files]}（只含有命中的仓）。"""
    result: Dict[str, List[str]] = {}
    for repo in _list_repos(root):
        files = _grep_repo(repo, patterns)
        if files:
            result[os.path.basename(repo)] = files
    return result


def _print_hits(hits: Dict[str, List[str]]) -> None:
    if not hits:
        print("  (无命中)")
        return
    for repo in sorted(hits):
        files = hits[repo]
        print("  - %-24s %d 处" % (repo, len(files)))
        for f in files[:4]:
            print("      %s" % f)
        if len(files) > 4:
            print("      … (+%d)" % (len(files) - 4))


def cmd_impact(args) -> int:
    root = args.root or DEFAULT_ROOT
    if not os.path.isdir(root):
        print("扫描根不存在：%s" % root, file=sys.stderr)
        return 2

    if args.component:
        comp = SHARED_COMPONENTS.get(args.component)
        print("=== 影响面体检：%s ===" % comp["desc"])
        print("  扫描根：%s" % root)
        impl = scan(comp["impl"], root)
        cons = scan(comp["consumer"], root)
        print("\n【实现 / 含该组件的仓】")
        _print_hits(impl)
        print("\n【接入 / 依赖方（消费此组件）】")
        _print_hits(cons)
        affected = sorted(set(impl) | set(cons))
        print("\n⚠️ 影响面：改动「%s」涉及 %d 个仓：%s"
              % (comp["desc"], len(affected), ", ".join(affected) or "(无)"))
        print("   风险：%s" % comp["risk"])
        print("\n（确定性 grep 结果，仅供枚举依赖方；具体行为仍以实测为准）")
        return 0

    # --pattern 通用
    print("=== 影响面体检：grep '%s' across %s ===" % (args.pattern, root))
    hits = scan([args.pattern], root)
    _print_hits(hits)
    affected = sorted(hits)
    print("\n影响面：%d 个仓命中：%s" % (len(affected), ", ".join(affected) or "(无)"))
    return 0


def register_impact(subparsers) -> None:
    """把 ``impact`` 命令挂到 ``dws-agent`` 的 add_subparsers 上（懒加载、非致命）。"""
    p = subparsers.add_parser(
        "impact",
        help="影响面体检：改共享组件前枚举依赖方 + 风险提示（确定性 grep ~/Workspace）",
        description=("改 SSO/Redis/Kafka/网关 等共享组件前，先枚举所有依赖方 + 跨系统风险，"
                     "避免类似 feedback#24 的互踢副作用上线后才被发现。"),
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--component", choices=list(SHARED_COMPONENTS),
                   help="预设共享组件（sso/redis/kafka/gateway）")
    g.add_argument("--pattern", help="通用：grep 这个正则，跨 ~/Workspace 各仓列命中方")
    p.add_argument("--root", help="扫描根（默认 ~/Workspace）")
    p.set_defaults(func=cmd_impact)
