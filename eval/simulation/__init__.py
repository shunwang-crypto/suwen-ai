"""M5 合成学生闭环有效性实验 (异构双模型: IRT 作答 / BKT 估计)。

模块划分:
- ``student``   环境侧: IRT 作答模型 + 动态学习模型 (真值 θ, 策略永不可见)
- ``policies``  智能体侧: 三种选题策略 (adaptive / random / ladder)
- ``stats``     统计检验纯函数 (Wilcoxon 符号秩 / Cohen's d / t 分布 95% CI)
- ``run_experiment``  实验 A/B runner + 报告与图表生成 CLI

设计说明与"为什么不是循环论证"的正式论证见同目录 README.md。
"""
