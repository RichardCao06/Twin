"""诊断 playbook：固化高频线上问题的诊断流程。

动机（见 docs/复盘-2026-06-23.md）：登录失败 / 404 / 服务不可达 等高频问题，今天靠手摸
curl/dns/kubectl 诊断。固化成一键 playbook，减少每次重复摸索。

命令在 :mod:`dws_agent.diagnose.cli`，挂载为 ``dws-agent diagnose <playbook>``。
"""
