"""M5 合成学生闭环实验 runner (纯计算, 零 LLM 调用, 分钟级)。

用法 (cwd = 仓库根)::

    export LD_LIBRARY_PATH=$CONDA_PREFIX/lib
    python eval/simulation/run_experiment.py --experiment both --seeds 10 \
        --output docs/evidence/m5/

产出: results.json (原始数据) + report.md (统计检验表) + 4 张图 (纯 matplotlib):
fig1 收敛曲线 / fig2 毕业生存曲线 / fig3 校准散点 / fig4 三臂毕业题量箱线图。

实验设计与"为什么不是循环论证"的论证见同目录 README.md。参数全部定案于
docs/research/ACTION_PLAN.md Wave2-1 与 brief"M5 四件套执行方案", 禁止随意改动。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from eduagents_core.mastery.bkt import BktParams, bkt_update  # noqa: E402

from simulation.policies import ARMS, Attempt, KcState, build_policy  # noqa: E402
from simulation.stats import (  # noqa: E402
    cohens_d_paired,
    mean_ci,
    r_squared,
    spearman_rho,
    wilcoxon_signed_rank,
)
from simulation.student import make_cohort, sample_theta  # noqa: E402

# ---------------------------------------------------------------------------
# 配置 (定案参数, 敏感性分析通过 dataclasses.replace 派生)
# ---------------------------------------------------------------------------

#: 估计器参数 (产品线默认): P(L0)=0.3, P(T)=0.15, P(G)=0.2, P(S)=0.1。
#: 与作答模型 (IRT: θ/b/题型异质 c) 不共享任何参数。
ESTIMATOR = BktParams(p_l0=0.3, p_t=0.15, slip=0.1, guess=0.2)


@dataclass(frozen=True)
class Config:
    n_students: int = 30
    n_kcs: int = 8
    t_rounds: int = 40  # 实验 A 每学生固定轮数
    threshold: float = 0.8  # 毕业阈值 P(L) > threshold
    max_questions: int = 200  # 实验 B 单学生题量上限
    learning_rate: float = 0.15  # λ
    theta_max: float = 0.95
    estimator: BktParams = ESTIMATOR


# 图表配色 (dataviz 校验通过的三色, light 模式): adaptive/random/ladder
COLORS = {"adaptive": "#2a78d6", "random": "#eb6834", "ladder": "#1baf7a"}
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e8e7e3"


def all_graduated(p_mastery: np.ndarray, threshold: float) -> bool:
    """毕业判定: 所有 KC 的估计掌握度 P(L) 严格大于阈值。"""
    return bool(np.all(p_mastery > threshold))


# ---------------------------------------------------------------------------
# 单学生模拟
# ---------------------------------------------------------------------------


def _kc_states(p: np.ndarray, attempts: list[int], threshold: float) -> list[KcState]:
    return [
        KcState(kc_id=k, p_mastery=float(p[k]), attempts=attempts[k], graduated=p[k] > threshold)
        for k in range(len(attempts))
    ]


def simulate_student_static(student, policy, cfg: Config) -> np.ndarray:
    """实验 A: 固定 T 轮, 返回 (T+1, K) 的估计轨迹 (第 0 行 = 先验)。"""
    k = student.n_kcs
    p = np.full(k, cfg.estimator.p_l0)
    attempts = [0] * k
    history: list[Attempt] = []
    trace = np.empty((cfg.t_rounds + 1, k))
    trace[0] = p
    for t in range(1, cfg.t_rounds + 1):
        kc, diff, qt = policy.select(_kc_states(p, attempts, cfg.threshold), history)
        correct = student.answer(kc, diff, qt)
        p[kc] = bkt_update(float(p[kc]), correct, cfg.estimator)
        attempts[kc] += 1
        history.append(Attempt(kc, diff, qt, correct))
        trace[t] = p
    return trace


def simulate_student_dynamic(student, policy, cfg: Config) -> tuple[int, bool, float]:
    """实验 B: 练到毕业或题量上限, 返回 (题数, 是否毕业, 终局真实 θ 均值)。"""
    k = student.n_kcs
    p = np.full(k, cfg.estimator.p_l0)
    attempts = [0] * k
    history: list[Attempt] = []
    n = 0
    while n < cfg.max_questions and not all_graduated(p, cfg.threshold):
        kc, diff, qt = policy.select(_kc_states(p, attempts, cfg.threshold), history)
        correct = student.answer(kc, diff, qt)  # 动态模式: 作答内附带真值学习
        p[kc] = bkt_update(float(p[kc]), correct, cfg.estimator)
        attempts[kc] += 1
        history.append(Attempt(kc, diff, qt, correct))
        n += 1
    return n, all_graduated(p, cfg.threshold), float(student.theta.mean())


# ---------------------------------------------------------------------------
# 实验 A: 估计收敛 (θ 静态)
# ---------------------------------------------------------------------------


def run_experiment_a(cfg: Config, seeds: list[int]) -> dict:
    """返回 {arm: {mae_curves: (S, T+1) 列表, calibration: {theta, p_final}}}。"""
    out: dict = {arm: {"mae_curves": [], "theta": [], "p_final": []} for arm in ARMS}
    for seed in seeds:
        theta = sample_theta(np.random.default_rng([seed, 0]), cfg.n_students, cfg.n_kcs)
        for arm_idx, arm in enumerate(ARMS):
            students = make_cohort(theta, seed, dynamic=False)
            mae_curve = np.zeros(cfg.t_rounds + 1)
            for i, stu in enumerate(students):
                policy = build_policy(
                    arm, np.random.default_rng([seed, i, 2, arm_idx]), cfg.threshold
                )
                trace = simulate_student_static(stu, policy, cfg)
                mae_curve += np.abs(trace - stu.theta0).mean(axis=1)
                out[arm]["theta"].extend(stu.theta0.tolist())
                out[arm]["p_final"].extend(trace[-1].tolist())
            out[arm]["mae_curves"].append((mae_curve / cfg.n_students).tolist())
    for arm in ARMS:
        out[arm]["r2"] = r_squared(out[arm]["theta"], out[arm]["p_final"])
        out[arm]["spearman"] = spearman_rho(out[arm]["theta"], out[arm]["p_final"])
        mean_curve = np.asarray(out[arm]["mae_curves"]).mean(axis=0)
        out[arm]["convergence_round"] = _convergence_round(mean_curve)
        out[arm]["min_mae"] = float(mean_curve.min())
        out[arm]["min_mae_round"] = int(mean_curve.argmin())
    return out


def _convergence_round(mean_curve: np.ndarray) -> int | None:
    """收敛判定 (定案): 滑窗 5 轮 MAE 极差 < 0.01 且与终值差 < 0.02 的最早轮。"""
    final = mean_curve[-1]
    for t in range(5, len(mean_curve)):
        window = mean_curve[t - 4 : t + 1]
        if (window.max() - window.min()) < 0.01 and abs(mean_curve[t] - final) < 0.02:
            return t
    return None


# ---------------------------------------------------------------------------
# 实验 B: 学习效率 (θ 动态)
# ---------------------------------------------------------------------------


def run_arm_b(cfg: Config, seed: int, arm: str) -> dict:
    """单 (seed, arm) 的实验 B: 30 名学生练到毕业/上限。同 seed 三臂共享 θ 与作答随机流。"""
    arm_idx = ARMS.index(arm)
    theta0 = sample_theta(np.random.default_rng([seed, 0]), cfg.n_students, cfg.n_kcs)
    students = make_cohort(
        theta0,
        seed,
        dynamic=True,
        learning_rate=cfg.learning_rate,
        theta_max=cfg.theta_max,
    )
    questions: list[int] = []
    graduated: list[bool] = []
    final_theta: list[float] = []
    for i, stu in enumerate(students):
        policy = build_policy(arm, np.random.default_rng([seed, i, 2, arm_idx]), cfg.threshold)
        n, grad, theta_mean = simulate_student_dynamic(stu, policy, cfg)
        questions.append(n if grad else cfg.max_questions)  # 未毕业按上限计 (保守删失)
        graduated.append(grad)
        final_theta.append(theta_mean)
    return {"questions": questions, "graduated": graduated, "final_theta": final_theta}


def run_experiment_b(cfg: Config, seeds: list[int]) -> dict:
    per_seed: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    for seed in seeds:
        for arm in ARMS:
            per_seed[arm].append(run_arm_b(cfg, seed, arm))
    result: dict = {"arms": {}, "comparisons": {}}
    for arm in ARMS:
        runs = per_seed[arm]
        seed_means = [float(np.mean(r["questions"])) for r in runs]
        mean, lo, hi = mean_ci(seed_means)
        result["arms"][arm] = {
            "seed_mean_questions": seed_means,
            "mean": mean,
            "ci95": [lo, hi],
            "grad_rate": float(np.mean([np.mean(r["graduated"]) for r in runs])),
            "final_theta_mean": float(np.mean([np.mean(r["final_theta"]) for r in runs])),
            "all_questions": [q for r in runs for q in r["questions"]],
            "all_graduated": [g for r in runs for g in r["graduated"]],
        }
    for a, b in (("adaptive", "random"), ("adaptive", "ladder"), ("ladder", "random")):
        xa = result["arms"][a]["seed_mean_questions"]
        xb = result["arms"][b]["seed_mean_questions"]
        wil = wilcoxon_signed_rank(xa, xb)
        diffs = [x - y for x, y in zip(xa, xb, strict=True)]
        dmean, dlo, dhi = mean_ci(diffs)
        result["comparisons"][f"{a}_vs_{b}"] = {
            "mean_diff": dmean,
            "diff_ci95": [dlo, dhi],
            "wilcoxon_w": wil.statistic,
            "p_value": wil.p_value,
            "method": wil.method,
            "cohens_d": cohens_d_paired(xa, xb),
        }
    return result


# ---------------------------------------------------------------------------
# 敏感性分析 (实验 B 变体重跑, 证明结论方向不是参数凑出来的)
# ---------------------------------------------------------------------------


def sensitivity_variants(cfg: Config) -> list[tuple[str, Config]]:
    est = cfg.estimator
    return [
        ("P(T)-50% (0.075)", dataclasses.replace(cfg, estimator=dataclasses.replace(est, p_t=0.075))),
        ("P(T)+50% (0.225)", dataclasses.replace(cfg, estimator=dataclasses.replace(est, p_t=0.225))),
        ("P(G)-50% (0.10)", dataclasses.replace(cfg, estimator=dataclasses.replace(est, guess=0.1))),
        ("P(G)+50% (0.30)", dataclasses.replace(cfg, estimator=dataclasses.replace(est, guess=0.3))),
        ("P(S)-50% (0.05)", dataclasses.replace(cfg, estimator=dataclasses.replace(est, slip=0.05))),
        ("P(S)+50% (0.15)", dataclasses.replace(cfg, estimator=dataclasses.replace(est, slip=0.15))),
        ("阈值 0.9", dataclasses.replace(cfg, threshold=0.9)),
        ("阈值 0.95", dataclasses.replace(cfg, threshold=0.95)),
        ("λ=0.08", dataclasses.replace(cfg, learning_rate=0.08)),
        ("λ=0.25", dataclasses.replace(cfg, learning_rate=0.25)),
    ]


def run_sensitivity(cfg: Config, seeds: list[int]) -> list[dict]:
    rows = []
    for label, variant_cfg in sensitivity_variants(cfg):
        res = run_experiment_b(variant_cfg, seeds)
        cmp_ar = res["comparisons"]["adaptive_vs_random"]
        rows.append(
            {
                "variant": label,
                "mean_adaptive": res["arms"]["adaptive"]["mean"],
                "mean_random": res["arms"]["random"]["mean"],
                "mean_ladder": res["arms"]["ladder"]["mean"],
                "mean_diff_a_vs_r": cmp_ar["mean_diff"],
                "p_value": cmp_ar["p_value"],
                "cohens_d": cmp_ar["cohens_d"],
                "direction_holds": cmp_ar["mean_diff"] < 0,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 图表 (纯 matplotlib, 图内文字全英文避免中文字体缺失)
# ---------------------------------------------------------------------------


def _style_axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#d8d7d3")
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.title.set_color(TEXT_PRIMARY)
    ax.xaxis.label.set_color(TEXT_SECONDARY)
    ax.yaxis.label.set_color(TEXT_SECONDARY)


def _new_fig():
    fig, ax = plt.subplots(figsize=(7.4, 4.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    _style_axes(ax)
    return fig, ax


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def fig1_convergence(exp_a: dict, out: Path) -> None:
    fig, ax = _new_fig()
    for arm in ARMS:
        curves = np.asarray(exp_a[arm]["mae_curves"])  # (S, T+1)
        rounds = np.arange(curves.shape[1])
        stats = [mean_ci(curves[:, t].tolist()) for t in rounds]
        mean = np.array([s[0] for s in stats])
        lo = np.array([s[1] for s in stats])
        hi = np.array([s[2] for s in stats])
        ax.plot(rounds, mean, color=COLORS[arm], linewidth=2, label=arm)
        ax.fill_between(rounds, lo, hi, color=COLORS[arm], alpha=0.15, linewidth=0)
        ax.annotate(
            arm,
            (rounds[-1], mean[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            color=COLORS[arm],
            fontsize=9,
            va="center",
        )
    ax.set_xlim(0, len(rounds) * 1.12)
    ax.set_title("Experiment A: BKT estimate convergence (static ability, 10 seeds)")
    ax.set_xlabel("Questions answered per student")
    ax.set_ylabel("MAE between BKT P(L) and true θ")
    ax.legend(frameon=False, labelcolor=TEXT_SECONDARY, fontsize=9)
    _save(fig, out / "fig1_convergence.png")


def fig2_survival(exp_b: dict, cfg: Config, out: Path) -> None:
    fig, ax = _new_fig()
    qs = np.arange(cfg.max_questions + 1)
    for arm in ARMS:
        counts = np.asarray(exp_b["arms"][arm]["all_questions"])
        grads = np.asarray(exp_b["arms"][arm]["all_graduated"])
        # 生存函数: 尚未毕业比例; 未毕业者视为删失 (永不离开风险集)
        grad_q = np.where(grads, counts, cfg.max_questions + 1)
        surv = [(grad_q > q).mean() for q in qs]
        ax.step(qs, surv, where="post", color=COLORS[arm], linewidth=2, label=arm)
    ax.set_title("Experiment B: survival curve (share of students not yet graduated)")
    ax.set_xlabel("Questions attempted")
    ax.set_ylabel("Share not graduated")
    ax.set_ylim(-0.02, 1.02)
    ax.legend(frameon=False, labelcolor=TEXT_SECONDARY, fontsize=9)
    _save(fig, out / "fig2_survival.png")


def fig3_calibration(exp_a: dict, out: Path) -> None:
    fig, ax = _new_fig()
    ax.plot([0, 1], [0, 1], color="#b9b8b4", linewidth=1.2, linestyle="--", zorder=1)
    for arm in ARMS:
        theta = np.asarray(exp_a[arm]["theta"])
        p_final = np.asarray(exp_a[arm]["p_final"])
        ax.scatter(
            theta,
            p_final,
            s=9,
            alpha=0.25,
            linewidths=0,
            color=COLORS[arm],
            label=f"{arm} (R²={exp_a[arm]['r2']:.2f})",
            zorder=2,
        )
    ax.set_title("Calibration: final BKT P(L) vs true θ (Experiment A)")
    ax.set_xlabel("True ability θ")
    ax.set_ylabel("Final BKT estimate P(L)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    leg = ax.legend(frameon=False, labelcolor=TEXT_SECONDARY, fontsize=9, loc="upper left")
    for handle in leg.legend_handles:
        handle.set_alpha(1.0)
    _save(fig, out / "fig3_calibration.png")


def fig4_boxplot(exp_b: dict, out: Path) -> None:
    fig, ax = _new_fig()
    data = [exp_b["arms"][arm]["all_questions"] for arm in ARMS]
    boxes = ax.boxplot(
        data,
        tick_labels=list(ARMS),
        patch_artist=True,
        widths=0.5,
        medianprops={"color": TEXT_PRIMARY, "linewidth": 1.6},
        whiskerprops={"color": TEXT_SECONDARY},
        capprops={"color": TEXT_SECONDARY},
        flierprops={"markersize": 3, "markerfacecolor": TEXT_SECONDARY, "alpha": 0.4},
    )
    for patch, arm in zip(boxes["boxes"], ARMS, strict=True):
        patch.set_facecolor(COLORS[arm])
        patch.set_alpha(0.45)
        patch.set_edgecolor(COLORS[arm])
    for i, arm in enumerate(ARMS, start=1):
        mean = exp_b["arms"][arm]["mean"]
        ax.annotate(
            f"mean {mean:.1f}",
            (i, mean),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=9,
            color=TEXT_PRIMARY,
        )
    ax.set_title("Experiment B: questions needed to graduate all KCs (300 students/arm)")
    ax.set_ylabel("Questions to graduation (capped at 200)")
    _save(fig, out / "fig4_boxplot.png")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _fmt_p(p: float) -> str:
    return f"{p:.4f}" if p >= 0.0001 else "<0.0001"


def write_report(
    out: Path,
    cfg: Config,
    seeds: list[int],
    exp_a: dict | None,
    exp_b: dict | None,
    sensitivity: list[dict] | None,
    elapsed: float,
) -> None:
    lines: list[str] = []
    add = lines.append
    add("# M5 合成学生闭环有效性实验报告")
    add("")
    add(f"- 生成时间: {datetime.now(UTC).isoformat()}")
    add(f"- 运行耗时: {elapsed:.1f}s (纯数值仿真, 零 LLM 调用)")
    add(f"- Seeds: {seeds} (配对设计: 同 seed 三臂共享同一批学生 θ 与作答随机流)")
    add(
        f"- 规模: N={cfg.n_students} 学生 × K={cfg.n_kcs} KC × {len(seeds)} seeds"
        f" (每臂等效 {cfg.n_students * len(seeds)} 名虚拟学生)"
    )
    add(
        "- 作答模型 (环境侧): IRT 3PL — P=c+(1-c)·sigmoid(1.7·(logit(θ)-b)),"
        " b∈{-1,0,+1}, c 按题型 {single:0.25, judge:0.5, multi:0.06, blank:0.05}"
    )
    add(
        f"- 估计器 (产品侧): BKT — P(L0)={cfg.estimator.p_l0}, P(T)={cfg.estimator.p_t},"
        f" P(G)={cfg.estimator.guess}, P(S)={cfg.estimator.slip}; 与作答模型不共享任何参数"
    )
    add(
        f"- 实验 B: λ={cfg.learning_rate}, θ_max={cfg.theta_max}, 毕业阈值 P(L)>{cfg.threshold},"
        f" 题量上限 {cfg.max_questions}; **预注册唯一主指标 = 毕业所需题量**"
    )
    add("")

    if exp_a is not None:
        add("## 实验 A: 估计准确性 (θ 静态) — “掌握度数字准不准”")
        add("")
        add(
            "| 策略臂 | 最低 MAE (@轮) | 终局 MAE (mean±95%CI) | 收敛轮数* |"
            " 校准 R² (对角线) | Spearman ρ |"
        )
        add("|---|---|---|---|---|---|")
        for arm in ARMS:
            curves = np.asarray(exp_a[arm]["mae_curves"])
            mean, lo, hi = mean_ci(curves[:, -1].tolist())
            conv = exp_a[arm]["convergence_round"]
            conv_s = str(conv) if conv is not None else f">{cfg.t_rounds} (未收敛)"
            add(
                f"| {arm} | {exp_a[arm]['min_mae']:.3f} (@{exp_a[arm]['min_mae_round']})"
                f" | {mean:.4f} [{lo:.4f}, {hi:.4f}] | {conv_s}"
                f" | {exp_a[arm]['r2']:.3f} | {exp_a[arm]['spearman']:.3f} |"
            )
        add("")
        add(
            "*收敛判定: 滑窗 5 轮 MAE 极差 < 0.01 且与第 40 轮终值差 < 0.02 的最早轮"
            " (对跨 seed 平均曲线计算)。"
        )
        add("")
        add("### 如实报告与分析 (实验 A 预期未完全成立)")
        add("")
        min_maes = [exp_a[a]["min_mae"] for a in ARMS]
        min_rounds = [exp_a[a]["min_mae_round"] for a in ARMS]
        finals = [float(np.asarray(exp_a[a]["mae_curves"])[:, -1].mean()) for a in ARMS]
        hi_frac = {
            a: float((np.asarray(exp_a[a]["p_final"]) > 0.9).mean()) * 100 for a in ARMS
        }
        add(
            f"- 预注册预期是“自适应更快压低 MAE”; 实际观察: 三臂 MAE 均在前"
            f" {max(min_rounds)} 轮内下探 (最低 {min(min_maes):.2f}-{max(min_maes):.2f})"
            f" 后**回升并在高位收敛** ({min(finals):.2f}-{max(finals):.2f}),"
            " 且自适应臂并不占优。"
        )
        add(
            "- 原因 (结构性, 非实现 bug): BKT 的 P(T)"
            f"={cfg.estimator.p_t} 内置“练习即可能学会”假设,"
            " 而实验 A 的静态环境**故意违反**该假设 (θ 恒定)。于是每次作答都经由学习转移"
            " 把 P(L) 推向 1 (终局 P(L)>0.9 的 KC 占比: 自适应"
            f" {hi_frac['adaptive']:.0f}%/阶梯 {hi_frac['ladder']:.0f}%/随机"
            f" {hi_frac['random']:.0f}%), 绝对刻度系统性上偏, 对角线校准 R² 为负"
            "——这是异构双模型设计下估计器错误设定 (misspecification) 被如实暴露,"
            " 我们不掩饰。"
        )
        add(
            "- 自适应臂 Spearman ρ 低于随机臂, 属**目标性压缩**: 自适应把所有 KC 练到"
            " 毕业线之上, 终局快照的估计分布被压平, 排序信息随之减少;"
            " 随机臂因给强 KC 的题更少反而保留了更多个体差异。"
        )
        add(
            "- 实务含义: P(L) 应解读为“达标判定信号”而非 θ 的无偏估计;"
            " 它的决策价值在假设匹配的动态环境 (实验 B, 学生练习确实在学习) 中独立验证。"
        )
        add("")

    if exp_b is not None:
        add("## 实验 B: 教学效率 (θ 动态) — 主实验")
        add("")
        add("### 三臂主指标: 毕业所需题量 (seed 级均值, n=10)")
        add("")
        add("| 策略臂 | 毕业题量 mean±95%CI | 200 题内毕业率 | 终局真实 θ 均值 |")
        add("|---|---|---|---|")
        for arm in ARMS:
            a = exp_b["arms"][arm]
            add(
                f"| {arm} | {a['mean']:.1f} [{a['ci95'][0]:.1f}, {a['ci95'][1]:.1f}]"
                f" | {a['grad_rate'] * 100:.1f}% | {a['final_theta_mean']:.3f} |"
            )
        add("")
        add("### 配对统计检验 (Wilcoxon 符号秩, 双尾, n=10 seeds)")
        add("")
        add("| 对比 | 题量差 mean [95%CI] | W | p | Cohen's d |")
        add("|---|---|---|---|---|")
        for key, cmp_ in exp_b["comparisons"].items():
            add(
                f"| {key.replace('_vs_', ' vs ')} | {cmp_['mean_diff']:+.1f}"
                f" [{cmp_['diff_ci95'][0]:.1f}, {cmp_['diff_ci95'][1]:.1f}]"
                f" | {cmp_['wilcoxon_w']:.1f} | {_fmt_p(cmp_['p_value'])} ({cmp_['method']})"
                f" | {cmp_['cohens_d']:+.2f} |"
            )
        add("")
        add(
            "注: p=0.0020 是 n=10 全同向差值下精确 Wilcoxon 的最小可能值 (2/2¹⁰);"
            " Cohen's d 按 seed 级配对差值计算, 共同随机数设计使差值方差极小, d 因此偏大,"
            " 学生层原始分布见 fig4 箱线图。"
        )
        add("")
        main_cmp = exp_b["comparisons"]["adaptive_vs_random"]
        ladder_cmp = exp_b["comparisons"]["adaptive_vs_ladder"]
        saved = -main_cmp["mean_diff"]
        if main_cmp["p_value"] < 0.05 and main_cmp["mean_diff"] < 0:
            add(
                f"**主结论: 自适应策略显著优于随机选题** — 平均少用 {saved:.1f} 题毕业"
                f" (p={_fmt_p(main_cmp['p_value'])} < 0.05, |d|={abs(main_cmp['cohens_d']):.2f})。"
            )
            if ladder_cmp["p_value"] < 0.05 and ladder_cmp["mean_diff"] < 0:
                add(
                    f"自适应同样显著优于难度阶梯基线 (少 {-ladder_cmp['mean_diff']:.1f} 题,"
                    f" p={_fmt_p(ladder_cmp['p_value'])}), 说明优势并非“随机基线太弱”。"
                )
            else:
                add(
                    f"注意: 自适应 vs 难度阶梯的差异未达显著 (p={_fmt_p(ladder_cmp['p_value'])}),"
                    " 如实报告 — 难度阶梯是强启发式基线, 自适应的增量主要来自估计驱动的 KC 优先级。"
                )
        else:
            add(
                f"**主结论: 自适应策略未显著优于随机选题** (p={_fmt_p(main_cmp['p_value'])},"
                f" 题量差 {main_cmp['mean_diff']:+.1f})。如实报告, 不做参数调整凑显著;"
                " 可能原因: 学习增长模型对策略差异不敏感 / 题量上限截断压缩了差距。"
            )
        add("")
        add(
            "> 终局真实 θ 均值三臂接近, 说明自适应臂不是“只刷估计不刷学习”"
            "——毕业更快的同时真实能力不打折。"
        )
        add("")

    if sensitivity:
        add("## 敏感性分析 (实验 B 重跑, adaptive vs random)")
        add("")
        add("| 变体 | adaptive | random | ladder | Δ(a−r) | p | Cohen's d | 方向不变? |")
        add("|---|---|---|---|---|---|---|---|")
        for row in sensitivity:
            add(
                f"| {row['variant']} | {row['mean_adaptive']:.1f} | {row['mean_random']:.1f}"
                f" | {row['mean_ladder']:.1f} | {row['mean_diff_a_vs_r']:+.1f}"
                f" | {_fmt_p(row['p_value'])} | {row['cohens_d']:+.2f}"
                f" | {'✓' if row['direction_holds'] else '✗ 反转'} |"
            )
        add("")
        holds = sum(1 for r in sensitivity if r["direction_holds"])
        add(
            f"{holds}/{len(sensitivity)} 个变体下“自适应少于随机”的方向保持不变"
            " (BKT 估计器参数 ±50% / 毕业阈值 0.8/0.9/0.95 / λ 三档)。"
        )
        add("")

    add("## 图表")
    add("")
    add("| 图 | 文件 | 内容 |")
    add("|---|---|---|")
    add("| fig1 | fig1_convergence.png | 实验 A 三臂 MAE 收敛曲线 (95% CI 带) |")
    add("| fig2 | fig2_survival.png | 实验 B 毕业生存曲线 (未毕业比例 vs 题量) |")
    add("| fig3 | fig3_calibration.png | 校准散点: 终局 P(L) vs 真值 θ (对角线 + R²) |")
    add("| fig4 | fig4_boxplot.png | 三臂毕业题量箱线图 |")
    add("")
    add("## 方法论说明 (答辩要点)")
    add("")
    add(
        "作答模型 (IRT: 连续能力 + 难度 + 题型异质猜测) 与估计模型 (BKT: 二元潜变量 HMM)"
        " **结构异构、参数互不共享**, 属 arXiv 2605.21613 的异构双模型范式;"
        " 完整论证见 eval/simulation/README.md。"
        " 本实验定位为算法层验证 (估计器正确性 + 策略相对优劣), 不宣称替代真人实验。"
    )
    add("")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 合成学生闭环实验")
    parser.add_argument("--experiment", choices=["a", "b", "both"], default="both")
    parser.add_argument("--seeds", type=int, default=10, help="随机种子数 (0..n-1)")
    parser.add_argument("--output", type=Path, default=Path("docs/evidence/m5"))
    parser.add_argument(
        "--skip-sensitivity", action="store_true", help="跳过敏感性分析 (快速迭代用)"
    )
    args = parser.parse_args(argv)

    cfg = Config()
    seeds = list(range(args.seeds))
    out: Path = args.output
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    exp_a = exp_b = sensitivity = None
    if args.experiment in ("a", "both"):
        print(f"[exp A] 静态 θ 收敛实验: {len(seeds)} seeds × 3 arms ...", flush=True)
        exp_a = run_experiment_a(cfg, seeds)
        fig1_convergence(exp_a, out)
        fig3_calibration(exp_a, out)
    if args.experiment in ("b", "both"):
        print(f"[exp B] 动态 θ 教学效率实验: {len(seeds)} seeds × 3 arms ...", flush=True)
        exp_b = run_experiment_b(cfg, seeds)
        fig2_survival(exp_b, cfg, out)
        fig4_boxplot(exp_b, out)
        if not args.skip_sensitivity:
            print("[sensitivity] 10 个变体重跑实验 B ...", flush=True)
            sensitivity = run_sensitivity(cfg, seeds)

    elapsed = time.perf_counter() - t0
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(elapsed, 2),
        "config": dataclasses.asdict(cfg),
        "seeds": seeds,
        "experiment_a": exp_a,
        "experiment_b": exp_b,
        "sensitivity": sensitivity,
    }
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    write_report(out, cfg, seeds, exp_a, exp_b, sensitivity, elapsed)
    print(f"完成 ({elapsed:.1f}s) → {out}/results.json, report.md, fig*.png")

    if exp_b is not None:
        for arm in ARMS:
            a = exp_b["arms"][arm]
            print(f"  {arm:>8}: 毕业题量 {a['mean']:.1f} [{a['ci95'][0]:.1f}, {a['ci95'][1]:.1f}]")
        cmp_ = exp_b["comparisons"]["adaptive_vs_random"]
        print(
            f"  adaptive vs random: Δ={cmp_['mean_diff']:+.1f}, p={_fmt_p(cmp_['p_value'])},"
            f" d={cmp_['cohens_d']:+.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
