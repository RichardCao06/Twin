"""分析型能力（Analysis Layer of Orchestration）.

汇集 dws-agent 里"读取数据后产出诊断/影响面报告"的分析型 CLI:

- diagnose_cli.py（原 diagnose/cli.py）—— 高频线上问题的诊断 playbook
- impact_cli.py（原 impact/cli.py）—— 改共享组件前的影响面体检

这两个都是"编排层的一种应用形态"（读→分析→报告），归到一起
避免每个业务 CLI 独立成 top-level 子包。
"""
