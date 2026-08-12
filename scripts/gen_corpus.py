"""合成《高等数学讲义》演示语料 + 检索黄金集 v1。

生成物 (相对仓库根):
- corpus/calculus.md          — 主线演示教材 (5 章 x >=3 小节, 公式/表格/长段落)
- eval/golden_set/v1.jsonl    — >=12 个问题, relevant_headings 对准语料真实标题

用法: python scripts/gen_corpus.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

VERSION = "v1-2026-07-26"
REPO_ROOT = Path(__file__).resolve().parents[1]

CORPUS_MD = r"""<!-- corpus_version: v1-2026-07-26 -->

# 高等数学讲义

本讲义面向理工科一年级学生, 覆盖一元微积分主干内容: 极限与连续、导数与微分、
微分中值定理、泰勒公式与定积分。每章配有定义、定理、典型例题与常用公式表,
可作为课程主教材与答疑知识库语料使用。

## 第一章 极限与连续

极限是整个微积分的地基: 导数是差商的极限, 定积分是黎曼和的极限。本章先给出
数列极限的精确定义, 再推广到函数极限, 最后讨论无穷小量的比较与函数的连续性。

### 1.1 数列的极限

**定义 (ε-N 语言)**: 设 $\{a_n\}$ 为一数列, $A$ 为常数。若对任意给定的
$\varepsilon > 0$, 总存在正整数 $N$, 使得当 $n > N$ 时恒有
$|a_n - A| < \varepsilon$, 则称数列 $\{a_n\}$ 收敛于 $A$, 记作
$\lim_{n\to\infty} a_n = A$。若这样的常数 $A$ 不存在, 则称数列发散。

这一定义的关键在于 "任意给定" 与 "总存在" 的次序: $\varepsilon$ 刻画的是
逼近的精度要求, 它是先给定的、任意小的; $N$ 是对该精度的回应, 允许依赖于
$\varepsilon$。例如对 $a_n = 1/n$, 要使 $|1/n - 0| < \varepsilon$, 只需取
$N = \lceil 1/\varepsilon \rceil$, 故 $\lim_{n\to\infty} 1/n = 0$。

收敛数列有三条基本性质: 极限唯一; 收敛数列必有界; 保号性 —— 若极限为正,
则从某项起所有项均为正。此外, **单调有界准则**指出: 单调递增且有上界
(或单调递减且有下界) 的数列必收敛。利用该准则可以证明数列
$\left(1 + \frac{1}{n}\right)^n$ 单调递增且有上界 3, 从而收敛, 其极限
定义为自然常数 $e \approx 2.71828$:

$$
\lim_{n\to\infty} \left(1 + \frac{1}{n}\right)^{n} = e
$$

**夹逼准则**: 若从某项起 $b_n \le a_n \le c_n$, 且
$\lim_{n\to\infty} b_n = \lim_{n\to\infty} c_n = A$, 则
$\lim_{n\to\infty} a_n = A$。夹逼准则是求含 $n$ 项和式极限的常用工具, 例如
$\lim_{n\to\infty} \sum_{i=1}^{n} \frac{n}{n^2 + i} = 1$ 即可由两侧放缩得到。

### 1.2 函数的极限与两个重要极限

将 ε-N 定义中的 "$n > N$" 换成 "$0 < |x - x_0| < \delta$", 便得到函数极限的
ε-δ 定义: 若对任意 $\varepsilon > 0$, 存在 $\delta > 0$, 使得当
$0 < |x - x_0| < \delta$ 时恒有 $|f(x) - A| < \varepsilon$, 则称
$\lim_{x\to x_0} f(x) = A$。注意条件中 $0 < |x - x_0|$ 表明极限与 $f$ 在
$x_0$ 处是否有定义、取值如何均无关。左极限 $f(x_0^-)$ 与右极限 $f(x_0^+)$
分别只考察 $x_0$ 左侧与右侧的趋近; 极限存在当且仅当左右极限都存在且相等。

微积分中有两个必须熟记的重要极限。第一个重要极限:

$$
\lim_{x\to 0} \frac{\sin x}{x} = 1
$$

其证明用夹逼准则: 当 $0 < x < \pi/2$ 时, 由单位圆中三角形与扇形的面积比较
可得 $\sin x < x < \tan x$, 除以 $\sin x$ 并取倒数即得
$\cos x < \frac{\sin x}{x} < 1$, 令 $x \to 0^+$ 并利用偶函数性质即证。

第二个重要极限:

$$
\lim_{x\to\infty} \left(1 + \frac{1}{x}\right)^{x} = e,
\qquad \lim_{u\to 0} (1 + u)^{1/u} = e
$$

它是处理 $1^{\infty}$ 型未定式的标准工具。一般地, 若 $\alpha(x) \to 0$,
$\beta(x) \to \infty$, 则 $(1+\alpha)^{\beta} = e^{\beta\ln(1+\alpha)}$,
指数部分化为 $\beta\alpha$ 的极限即可。例如
$\lim_{x\to\infty}\left(1 - \frac{2}{x}\right)^{3x} = e^{-6}$。

### 1.3 无穷小量的比较与等价无穷小

若 $\lim f(x) = 0$, 称 $f$ 为该极限过程下的无穷小量。设 $\alpha, \beta$ 为
同一过程的无穷小且 $\beta \ne 0$: 若 $\lim \alpha/\beta = 0$, 称 $\alpha$ 是
$\beta$ 的高阶无穷小, 记 $\alpha = o(\beta)$; 若极限为非零常数, 称同阶;
若极限为 1, 称**等价**, 记 $\alpha \sim \beta$。

等价无穷小替换定理: 在乘除运算中, 无穷小因子可以换成与之等价的无穷小而不
改变极限值 (加减运算中不能直接替换, 需保留高阶项)。$x \to 0$ 时的常用等价
无穷小如下表:

| 原式 | 等价无穷小 ($x \to 0$) |
| --- | --- |
| $\sin x$ | $x$ |
| $\tan x$ | $x$ |
| $\arcsin x$ | $x$ |
| $\arctan x$ | $x$ |
| $1 - \cos x$ | $x^2 / 2$ |
| $e^x - 1$ | $x$ |
| $\ln(1 + x)$ | $x$ |
| $(1 + x)^{\alpha} - 1$ | $\alpha x$ |
| $a^x - 1$ | $x \ln a$ |

例: $\lim_{x\to 0} \dfrac{\tan x - \sin x}{x^3}$。注意分子是减法, 不能把
$\tan x$ 与 $\sin x$ 分别换成 $x$; 应先变形
$\tan x - \sin x = \tan x (1 - \cos x) \sim x \cdot \frac{x^2}{2}$,
故原极限为 $\frac{1}{2}$。

### 1.4 函数的连续性

若 $\lim_{x\to x_0} f(x) = f(x_0)$, 称 $f$ 在 $x_0$ 处连续; 等价地, 增量形式
$\lim_{\Delta x\to 0} \Delta y = 0$。间断点分为两类: 第一类间断点的左右极限
都存在 (相等为可去间断点, 不等为跳跃间断点); 第二类间断点的左右极限至少有
一个不存在, 如 $y = \sin(1/x)$ 在 $x=0$ 的振荡间断与 $y = 1/x$ 的无穷间断。

闭区间上连续函数有三大定理: **最值定理** (必取得最大值与最小值)、
**有界性定理**、**介值定理** (可取到最大值与最小值之间的一切值)。介值定理的
重要推论是零点存在定理: 若 $f$ 在 $[a,b]$ 上连续且 $f(a)f(b) < 0$, 则存在
$\xi \in (a,b)$ 使 $f(\xi) = 0$, 这是二分法求根的理论依据。

## 第二章 导数与微分

### 2.1 导数的定义与几何意义

**定义**: 设 $f$ 在 $x_0$ 的某邻域内有定义, 若极限

$$
f'(x_0) = \lim_{\Delta x \to 0} \frac{f(x_0 + \Delta x) - f(x_0)}{\Delta x}
= \lim_{x \to x_0} \frac{f(x) - f(x_0)}{x - x_0}
$$

存在, 则称 $f$ 在 $x_0$ 处可导, 该极限称为 $f$ 在 $x_0$ 处的导数。

**几何意义**: $f'(x_0)$ 是曲线 $y = f(x)$ 在点 $(x_0, f(x_0))$ 处**切线的
斜率**, 切线方程为 $y - f(x_0) = f'(x_0)(x - x_0)$; 物理上, 若 $s(t)$ 是
位移函数, 则 $s'(t_0)$ 是 $t_0$ 时刻的瞬时速度。

可导必连续, 但连续未必可导: $f(x) = |x|$ 在 $x = 0$ 处连续, 左导数为 $-1$、
右导数为 $1$, 不相等故不可导, 曲线在该点出现 "尖点"。可导要求左右导数存在
且相等。

### 2.2 基本初等函数导数表

以下导数公式必须熟记, 它们与求导法则一起构成计算任何初等函数导数的完整
工具箱:

| 函数 $f(x)$ | 导数 $f'(x)$ |
| --- | --- |
| $C$ (常数) | $0$ |
| $x^{\mu}$ | $\mu x^{\mu - 1}$ |
| $\sin x$ | $\cos x$ |
| $\cos x$ | $-\sin x$ |
| $\tan x$ | $\sec^2 x$ |
| $e^x$ | $e^x$ |
| $a^x$ | $a^x \ln a$ |
| $\ln x$ | $1/x$ |
| $\log_a x$ | $\dfrac{1}{x \ln a}$ |
| $\arcsin x$ | $\dfrac{1}{\sqrt{1 - x^2}}$ |
| $\arccos x$ | $-\dfrac{1}{\sqrt{1 - x^2}}$ |
| $\arctan x$ | $\dfrac{1}{1 + x^2}$ |

其中 $(\sin x)' = \cos x$ 可直接由定义与和差化积公式配合第一个重要极限推出,
$(e^x)' = e^x$ 则说明指数函数是导数运算的不动点。

### 2.3 求导法则: 四则、复合与反函数

设 $u, v$ 可导, 则四则运算法则为: $(u \pm v)' = u' \pm v'$;
$(uv)' = u'v + uv'$; $\left(\dfrac{u}{v}\right)' = \dfrac{u'v - uv'}{v^2}$
($v \ne 0$)。

**链式法则 (复合函数求导)**: 若 $u = g(x)$ 在 $x$ 处可导, $y = f(u)$ 在对应
的 $u$ 处可导, 则复合函数 $y = f(g(x))$ 在 $x$ 处可导, 且

$$
\frac{dy}{dx} = \frac{dy}{du} \cdot \frac{du}{dx},
\qquad \big(f(g(x))\big)' = f'(g(x)) \, g'(x)
$$

即 "外层导数乘以内层导数", 由外向内逐层求导。例:
$(\sin(x^2))' = \cos(x^2)\cdot 2x$;
$(e^{-x^2})' = e^{-x^2}\cdot(-2x)$。

**反函数求导法则**: 若 $x = \varphi(y)$ 单调可导且 $\varphi'(y) \ne 0$, 则其
反函数 $y = f(x)$ 可导, 且 $f'(x) = \dfrac{1}{\varphi'(y)}$。由此可从
$(\sin y)' = \cos y$ 推出 $(\arcsin x)' = \frac{1}{\sqrt{1-x^2}}$。

对幂指函数 $y = u(x)^{v(x)}$, 常用**对数求导法**: 两边取对数得
$\ln y = v \ln u$, 再对 $x$ 求导。例如 $y = x^x$ ($x>0$) 时
$\ln y = x\ln x$, 求导得 $y'/y = \ln x + 1$, 故 $y' = x^x(\ln x + 1)$。

### 2.4 高阶导数与隐函数求导

$f$ 的导函数 $f'$ 若仍可导, 其导数记为 $f''$, 依此类推得 $n$ 阶导数
$f^{(n)}$。常用高阶导数公式: $(e^x)^{(n)} = e^x$;
$(\sin x)^{(n)} = \sin\left(x + \frac{n\pi}{2}\right)$;
$(x^m)^{(n)} = m(m-1)\cdots(m-n+1)x^{m-n}$;
$\left(\ln(1+x)\right)^{(n)} = \dfrac{(-1)^{n-1}(n-1)!}{(1+x)^n}$。

两个函数乘积的 $n$ 阶导数由**莱布尼茨公式**给出:

$$
(uv)^{(n)} = \sum_{k=0}^{n} \binom{n}{k} u^{(k)} v^{(n-k)}
$$

形式上与二项式定理完全平行。当 $v$ 为低次多项式时, 求和只剩有限几项,
计算高阶导数非常高效。

对由方程 $F(x, y) = 0$ 确定的隐函数, 将 $y$ 视为 $x$ 的函数, 方程两边同时
对 $x$ 求导再解出 $y'$。例: 圆 $x^2 + y^2 = R^2$ 两边求导得
$2x + 2y y' = 0$, 故 $y' = -x/y$, 这正是圆上各点切线斜率的表达式。微分
$dy = f'(x)\,dx$ 是函数增量的线性主部, 一阶微分形式在变量替换下保持不变
(一阶微分形式不变性)。

## 第三章 微分中值定理

微分中值定理架起了函数值 (整体信息) 与导数值 (局部信息) 之间的桥梁, 是利用
导数研究函数单调性、极值与不等式的理论基础。三个定理层层推广: 罗尔定理是
特例, 拉格朗日中值定理是主干, 柯西中值定理是参数化推广。

### 3.1 罗尔定理

**罗尔定理**: 若函数 $f$ 满足三个条件: (1) 在闭区间 $[a, b]$ 上连续;
(2) 在开区间 $(a, b)$ 内可导; (3) $f(a) = f(b)$, 则至少存在一点
$\xi \in (a, b)$, 使得 $f'(\xi) = 0$。

几何直观: 两端等高的光滑曲线内部必有水平切线。证明依赖闭区间上连续函数的
最值定理与费马引理 (可导的极值点处导数为零): 若最大值与最小值都在端点取得,
则 $f$ 为常数, 结论平凡; 否则内部极值点即为所求 $\xi$。三个条件缺一不可,
例如 $f(x) = |x|$ 在 $[-1, 1]$ 上满足 (1)(3) 但在 $x=0$ 不可导, 结论失效。

罗尔定理常用来证明方程根的存在性: 若要证 $f'(x) = 0$ 有根, 只需找到 $f$ 的
两个等值点; 推而广之, $f^{(n)}$ 的零点个数问题可以通过对 $f$ 逐阶用罗尔
定理来解决。

### 3.2 拉格朗日中值定理

**拉格朗日中值定理**: 若 $f$ 在 $[a, b]$ 上连续、在 $(a, b)$ 内可导, 则至少
存在一点 $\xi \in (a, b)$, 使得

$$
f(b) - f(a) = f'(\xi)(b - a), \qquad \xi \in (a, b)
$$

等价写法 $\dfrac{f(b)-f(a)}{b-a} = f'(\xi)$: 曲线上必有一点的切线平行于
连接两端点的弦。取 $f(a) = f(b)$ 即退化为罗尔定理; 证明只需对辅助函数
$\varphi(x) = f(x) - \frac{f(b)-f(a)}{b-a}(x - a)$ 应用罗尔定理。

两个基本推论: 若在区间内恒有 $f'(x) = 0$, 则 $f$ 在该区间上为常数; 若恒有
$f'(x) > 0$ (或 $< 0$), 则 $f$ 严格单调递增 (递减)。这是用导数符号判断
单调性的理论依据。中值定理还是证明不等式的利器, 例如对 $0 < a < b$, 在
$[a,b]$ 上对 $\ln x$ 用中值定理得 $\ln b - \ln a = \frac{b-a}{\xi}$,
由 $a < \xi < b$ 立得 $\frac{b-a}{b} < \ln\frac{b}{a} < \frac{b-a}{a}$。

### 3.3 柯西中值定理

**柯西中值定理**: 若 $f, g$ 在 $[a, b]$ 上连续、在 $(a, b)$ 内可导, 且
$g'(x) \ne 0$, 则至少存在一点 $\xi \in (a, b)$, 使得

$$
\frac{f(b) - f(a)}{g(b) - g(a)} = \frac{f'(\xi)}{g'(\xi)}
$$

取 $g(x) = x$ 即回到拉格朗日中值定理。它可以看作对参数曲线
$(g(t), f(t))$ 叙述的中值定理: 曲线上有一点的切向量平行于两端点连线。注意
证明不能对分子分母分别用拉格朗日定理 (两次所得的中值点未必相同), 正确做法
是对 $\varphi(x) = f(x) - \frac{f(b)-f(a)}{g(b)-g(a)}\,g(x)$ 用罗尔定理。
柯西中值定理最重要的应用是推导洛必达法则。

### 3.4 洛必达法则

**洛必达法则**适用于 $\dfrac{0}{0}$ **型与** $\dfrac{\infty}{\infty}$
**型未定式**: 设在 $x_0$ 的某去心邻域内 $f, g$ 可导且 $g'(x) \ne 0$, 若
$\lim_{x\to x_0} f(x) = \lim_{x\to x_0} g(x) = 0$ (或都趋于无穷), 且
$\lim_{x\to x_0} \dfrac{f'(x)}{g'(x)}$ 存在 (或为无穷), 则

$$
\lim_{x\to x_0} \frac{f(x)}{g(x)} = \lim_{x\to x_0} \frac{f'(x)}{g'(x)}
$$

使用要点: (1) 每次使用前必须核验仍是 $0/0$ 或 $\infty/\infty$ 型;
(2) 导数比的极限不存在 (且非无穷) 时法则失效, 不能反推原极限不存在, 如
$\lim_{x\to\infty}\frac{x+\sin x}{x} = 1$ 但导数比 $1+\cos x$ 无极限;
(3) 其余未定式 $0\cdot\infty$, $\infty-\infty$, $1^{\infty}$, $0^0$,
$\infty^0$ 需先变形化为商的形式; (4) 与等价无穷小替换联用常可大幅化简。
例: $\lim_{x\to 0^+} x\ln x = \lim_{x\to 0^+}\frac{\ln x}{1/x}
= \lim_{x\to 0^+}\frac{1/x}{-1/x^2} = 0$。

## 第四章 泰勒公式

多项式是最容易计算的函数: 只需加法与乘法。泰勒公式的思想是在一点附近用
多项式逼近一般函数, 并给出误差 (余项) 的精确刻画, 是近似计算、极限计算与
理论分析的核心工具。

### 4.1 泰勒多项式与佩亚诺余项

若 $f$ 在 $x_0$ 处有 $n$ 阶导数, 定义 $n$ 次泰勒多项式

$$
P_n(x) = \sum_{k=0}^{n} \frac{f^{(k)}(x_0)}{k!} (x - x_0)^k
= f(x_0) + f'(x_0)(x - x_0) + \frac{f''(x_0)}{2!}(x - x_0)^2 + \cdots
$$

它是与 $f$ 在 $x_0$ 处 $0$ 到 $n$ 阶导数完全相同的唯一 $n$ 次多项式。
**带佩亚诺余项的泰勒公式**: $f(x) = P_n(x) + o\big((x - x_0)^n\big)$,
$x \to x_0$。佩亚诺余项只给出误差的阶, 是**定性**的, 适合求极限、判断
极值等局部问题。$n = 1$ 时即微分近似 $f(x) \approx f(x_0) + f'(x_0)(x-x_0)$。

### 4.2 拉格朗日余项

若 $f$ 在含 $x_0$ 的区间内有 $n+1$ 阶导数, 则对区间内任意 $x$, 存在介于
$x_0$ 与 $x$ 之间的 $\xi$, 使得

$$
f(x) = \sum_{k=0}^{n} \frac{f^{(k)}(x_0)}{k!}(x - x_0)^k
+ \underbrace{\frac{f^{(n+1)}(\xi)}{(n+1)!}(x - x_0)^{n+1}}_{R_n(x)}
$$

其中 $R_n(x) = \dfrac{f^{(n+1)}(\xi)}{(n+1)!}(x - x_0)^{n+1}$ 称为
**拉格朗日余项**。它是**定量**的误差表达式: 只要能估计 $|f^{(n+1)}|$ 在
区间上的界 $M$, 就有误差上界 $|R_n(x)| \le \dfrac{M}{(n+1)!}|x - x_0|^{n+1}$。
$n = 0$ 时公式退化为拉格朗日中值定理 $f(x) - f(x_0) = f'(\xi)(x - x_0)$,
可见泰勒公式是中值定理的高阶推广。$x_0 = 0$ 时的泰勒公式称为
**麦克劳林公式**。

### 4.3 常用函数的麦克劳林展开

下表列出五个基本函数的麦克劳林展开 (佩亚诺余项形式), 应当熟记:

| 函数 | 麦克劳林展开 ($x \to 0$) |
| --- | --- |
| $e^x$ | $1 + x + \dfrac{x^2}{2!} + \cdots + \dfrac{x^n}{n!} + o(x^n)$ |
| $\sin x$ | $x - \dfrac{x^3}{3!} + \dfrac{x^5}{5!} - \cdots + (-1)^{m-1}\dfrac{x^{2m-1}}{(2m-1)!} + o(x^{2m})$ |
| $\cos x$ | $1 - \dfrac{x^2}{2!} + \dfrac{x^4}{4!} - \cdots + (-1)^{m}\dfrac{x^{2m}}{(2m)!} + o(x^{2m+1})$ |
| $\ln(1+x)$ | $x - \dfrac{x^2}{2} + \dfrac{x^3}{3} - \cdots + (-1)^{n-1}\dfrac{x^n}{n} + o(x^n)$ |
| $(1+x)^{\alpha}$ | $1 + \alpha x + \dfrac{\alpha(\alpha-1)}{2!}x^2 + \cdots + o(x^n)$ |

以 $e^x$ 为例, 因 $(e^x)^{(k)} = e^x$ 在 $0$ 处取值均为 1, 带拉格朗日余项的
展开为

$$
e^x = 1 + x + \frac{x^2}{2!} + \cdots + \frac{x^n}{n!}
+ \frac{e^{\xi}}{(n+1)!} x^{n+1}, \qquad \xi \text{ 介于 } 0 \text{ 与 } x \text{ 之间}
$$

由展开式可见 $\sin x$ 只含奇次幂、$\cos x$ 只含偶次幂, 与二者的奇偶性一致;
$\ln(1+x)$ 的系数不含阶乘, 收敛远慢于 $e^x$。

### 4.4 泰勒公式的应用: 近似计算与求极限

**近似计算**: 用 $P_n$ 代替 $f$, 用拉格朗日余项控制误差。例: 计算 $e$ 使
误差小于 $10^{-6}$。取 $x = 1$, 余项 $R_n(1) = \frac{e^{\xi}}{(n+1)!} <
\frac{3}{(n+1)!}$, 解 $\frac{3}{(n+1)!} < 10^{-6}$ 得 $n = 9$ 即可, 此时
$e \approx \sum_{k=0}^{9} \frac{1}{k!} \approx 2.718282$。

**求极限**: 将各因子展开到 "首个不相消的阶" 再比较。例:

$$
\lim_{x\to 0} \frac{\cos x - e^{-x^2/2}}{x^4}
= \lim_{x\to 0} \frac{\left(1 - \frac{x^2}{2} + \frac{x^4}{24}\right)
- \left(1 - \frac{x^2}{2} + \frac{x^4}{8}\right) + o(x^4)}{x^4}
= -\frac{1}{12}
$$

这里 $\cos x$ 与 $e^{-x^2/2}$ 展开到 $x^4$ 项才分出差别, 洛必达法则做此题
需要求导四次, 泰勒展开明显更高效。**判定极值**也可用泰勒公式: 若
$f'(x_0) = \cdots = f^{(n-1)}(x_0) = 0$ 而 $f^{(n)}(x_0) \ne 0$, 则 $n$ 为
偶数时 $x_0$ 是极值点 ($f^{(n)}(x_0) > 0$ 为极小), $n$ 为奇数时不是极值点。

## 第五章 积分

### 5.1 不定积分与基本积分表

若 $F'(x) = f(x)$, 称 $F$ 为 $f$ 的一个原函数; $f$ 的全体原函数
$F(x) + C$ 称为 $f$ 的不定积分, 记 $\int f(x)\,dx = F(x) + C$。连续函数必有
原函数。求导公式反过来读即得基本积分表:

| 被积函数 $f(x)$ | 不定积分 $\int f(x)\,dx$ |
| --- | --- |
| $x^{\mu}$ ($\mu \ne -1$) | $\dfrac{x^{\mu+1}}{\mu+1} + C$ |
| $\dfrac{1}{x}$ | $\ln \lvert x \rvert + C$ |
| $e^x$ | $e^x + C$ |
| $a^x$ | $\dfrac{a^x}{\ln a} + C$ |
| $\sin x$ | $-\cos x + C$ |
| $\cos x$ | $\sin x + C$ |
| $\sec^2 x$ | $\tan x + C$ |
| $\dfrac{1}{1+x^2}$ | $\arctan x + C$ |
| $\dfrac{1}{\sqrt{1-x^2}}$ | $\arcsin x + C$ |

注意 $\int \frac{1}{x} dx = \ln|x| + C$ 中绝对值不可省略, 它统一覆盖了
$x > 0$ 与 $x < 0$ 两个区间上的原函数。

### 5.2 换元积分法与分部积分法

**第一类换元 (凑微分)**: 若 $\int f(u)\,du = F(u) + C$, 则
$\int f(\varphi(x))\varphi'(x)\,dx = F(\varphi(x)) + C$。关键是把被积式中的
一部分 "凑" 进微分号, 如 $\int \tan x\,dx = -\int \frac{(\cos x)'}{\cos x}dx
= -\ln|\cos x| + C$。

**第二类换元**: 令 $x = \psi(t)$ ($\psi$ 单调可导), 则
$\int f(x)\,dx = \int f(\psi(t))\psi'(t)\,dt$, 求出后回代。典型用法是三角
换元去根号: $\sqrt{a^2 - x^2}$ 令 $x = a\sin t$; $\sqrt{a^2 + x^2}$ 令
$x = a\tan t$; $\sqrt{x^2 - a^2}$ 令 $x = a\sec t$。

**分部积分法**由乘积求导公式 $(uv)' = u'v + uv'$ 移项积分得到:

$$
\int u \, dv = uv - \int v \, du
$$

选取 $u$ 的经验次序为 "反对幂指三" (反三角、对数优先当 $u$; 指数、三角
优先凑进 $dv$)。例: $\int x e^x dx = x e^x - \int e^x dx = (x-1)e^x + C$;
$\int \ln x\,dx = x\ln x - x + C$。对 $\int e^x \sin x\,dx$ 连用两次分部
积分会回到原积分, 移项即解得 $\frac{e^x(\sin x - \cos x)}{2} + C$。

### 5.3 定积分的定义与性质

**定义 (黎曼和的极限)**: 设 $f$ 在 $[a, b]$ 上有界, 任意分割
$a = x_0 < x_1 < \cdots < x_n = b$, 在每个小区间上任取 $\xi_i \in
[x_{i-1}, x_i]$, 记 $\Delta x_i = x_i - x_{i-1}$,
$\lambda = \max_i \Delta x_i$。若不论分割与取点方式如何, 和式极限

$$
\int_a^b f(x)\,dx = \lim_{\lambda \to 0} \sum_{i=1}^{n} f(\xi_i)\,\Delta x_i
$$

都存在且相等, 则称 $f$ 在 $[a, b]$ 上可积。几何上, 当 $f \ge 0$ 时定积分
就是曲边梯形的面积。闭区间上的连续函数、只有有限个间断点的有界函数均可积。

主要性质: 线性性; 对积分区间的可加性
$\int_a^b = \int_a^c + \int_c^b$; 保序性 (被积函数大则积分大); 估值不等式
$m(b-a) \le \int_a^b f\,dx \le M(b-a)$; **积分中值定理**: $f$ 连续时存在
$\xi \in [a, b]$ 使 $\int_a^b f(x)\,dx = f(\xi)(b - a)$, $f(\xi)$ 即 $f$ 在
区间上的平均值。

### 5.4 牛顿-莱布尼茨公式

设 $f$ 在 $[a, b]$ 上连续。变上限积分函数 $\Phi(x) = \int_a^x f(t)\,dt$
可导且 $\Phi'(x) = f(x)$ —— 这就是**微积分基本定理**, 它表明连续函数的
变上限积分正是它的一个原函数, 从而把微分与积分两大运算联结起来。

由此得**牛顿-莱布尼茨公式**: 若 $F$ 是连续函数 $f$ 在 $[a, b]$ 上的任一
原函数, 则

$$
\int_a^b f(x)\,dx = F(b) - F(a) = \big[ F(x) \big]_a^b
$$

它把定积分的计算归结为求原函数, 无需再回到黎曼和。例:
$\int_0^1 x^2\,dx = \left[\frac{x^3}{3}\right]_0^1 = \frac{1}{3}$;
$\int_0^{\pi} \sin x\,dx = [-\cos x]_0^{\pi} = 2$。定积分的换元法要求同时
换限: $\int_0^1 \sqrt{1-x^2}\,dx$ 令 $x = \sin t$ 化为
$\int_0^{\pi/2} \cos^2 t\,dt = \frac{\pi}{4}$, 恰为四分之一单位圆面积。

### 5.5 定积分的应用

**平面图形面积**: 由 $y = f(x)$ 与 $y = g(x)$ ($f \ge g$) 及 $x = a, b$
围成的面积为 $S = \int_a^b [f(x) - g(x)]\,dx$。例: 抛物线 $y = x^2$ 与直线
$y = x$ 围成的面积为 $\int_0^1 (x - x^2)\,dx = \frac{1}{6}$。

**旋转体体积**: 曲边梯形 $0 \le y \le f(x)$, $a \le x \le b$ 绕 $x$ 轴旋转
一周的体积为 $V = \pi \int_a^b f(x)^2\,dx$。例: 半圆 $y = \sqrt{R^2 - x^2}$
绕 $x$ 轴旋转得球, $V = \pi\int_{-R}^{R}(R^2 - x^2)\,dx = \frac{4}{3}\pi R^3$,
即球体积公式。

**弧长**: 光滑曲线 $y = f(x)$ ($a \le x \le b$) 的弧长为
$L = \int_a^b \sqrt{1 + f'(x)^2}\,dx$。此外, 变力做功、液体压力、平均值等
物理量凡能写成 "分割-近似-求和-取极限" 结构的, 都可以用定积分统一表达,
这正是微元法的思想。
"""

# 黄金集的 relevant_headings 均为语料中真实出现的标题片段（子串匹配）。
GOLDEN_ITEMS: list[dict] = [
    {
        "qid": "q01",
        "question": "数列极限的 ε-N 定义是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["数列的极限"],
        "ground_truth": "对任意 ε>0, 存在正整数 N, 当 n>N 时恒有 |a_n - A| < ε, 则称数列 {a_n} 收敛于 A。",
    },
    {
        "qid": "q02",
        "question": "两个重要极限分别是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["两个重要极限"],
        "ground_truth": "lim_{x→0} sin x / x = 1 与 lim_{x→∞} (1 + 1/x)^x = e。",
    },
    {
        "qid": "q03",
        "question": "x 趋于 0 时 1 - cos x 的等价无穷小是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["等价无穷小"],
        "ground_truth": "1 - cos x ~ x^2 / 2 (x→0)。",
    },
    {
        "qid": "q04",
        "question": "导数的几何意义是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["导数的定义与几何意义"],
        "ground_truth": "f'(x0) 是曲线 y=f(x) 在点 (x0, f(x0)) 处切线的斜率, 切线方程为 y - f(x0) = f'(x0)(x - x0)。",
    },
    {
        "qid": "q05",
        "question": "sin x 和 ln x 的导数分别是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["基本初等函数导数表"],
        "ground_truth": "(sin x)' = cos x; (ln x)' = 1/x。",
    },
    {
        "qid": "q06",
        "question": "复合函数求导的链式法则怎么用?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["求导法则"],
        "ground_truth": "dy/dx = (dy/du)(du/dx), 即 (f(g(x)))' = f'(g(x)) g'(x), 外层导数乘以内层导数。",
    },
    {
        "qid": "q07",
        "question": "罗尔定理的三个条件和结论是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["罗尔定理"],
        "ground_truth": "f 在 [a,b] 连续、(a,b) 可导且 f(a)=f(b), 则存在 ξ∈(a,b) 使 f'(ξ)=0。",
    },
    {
        "qid": "q08",
        "question": "拉格朗日中值定理的结论公式是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["拉格朗日中值定理"],
        "ground_truth": "f(b) - f(a) = f'(ξ)(b - a), 其中 ξ∈(a,b)。",
    },
    {
        "qid": "q09",
        "question": "洛必达法则适用于哪些类型的未定式?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["洛必达法则"],
        "ground_truth": "0/0 型与 ∞/∞ 型未定式; 其他未定式需先变形为商的形式。",
    },
    {
        "qid": "q10",
        "question": "泰勒公式的拉格朗日余项是什么形式?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["拉格朗日余项"],
        "ground_truth": "R_n(x) = f^(n+1)(ξ)/(n+1)! · (x - x0)^(n+1), ξ 介于 x0 与 x 之间。",
    },
    {
        "qid": "q11",
        "question": "e^x 的麦克劳林展开式是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["麦克劳林展开"],
        "ground_truth": "e^x = 1 + x + x^2/2! + ... + x^n/n! + o(x^n); 带拉格朗日余项时余项为 e^ξ/(n+1)! · x^(n+1)。",
    },
    {
        "qid": "q12",
        "question": "如何用泰勒公式求极限?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["泰勒公式的应用"],
        "ground_truth": "将各因子在 x0 处展开到首个不相消的阶, 再比较同阶系数, 如 lim (cos x - e^{-x^2/2})/x^4 = -1/12。",
    },
    {
        "qid": "q13",
        "question": "分部积分公式是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["分部积分法"],
        "ground_truth": "∫ u dv = uv - ∫ v du, 由乘积求导公式移项积分得到; 选 u 的经验次序为反对幂指三。",
    },
    {
        "qid": "q14",
        "question": "定积分是如何用黎曼和定义的?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["定积分的定义"],
        "ground_truth": "∫_a^b f(x)dx = lim_{λ→0} Σ f(ξ_i)Δx_i, 其中 λ 为最大小区间长度, 极限与分割和取点方式无关。",
    },
    {
        "qid": "q15",
        "question": "牛顿-莱布尼茨公式的内容是什么?",
        "relevant_doc": "calculus.md",
        "relevant_headings": ["牛顿-莱布尼茨公式"],
        "ground_truth": "若 F 是连续函数 f 在 [a,b] 上的原函数, 则 ∫_a^b f(x)dx = F(b) - F(a)。",
    },
]


def generate(root: Path = REPO_ROOT) -> tuple[Path, Path]:
    """生成语料与黄金集, 返回 (corpus_path, golden_path)。root 可换成临时目录供测试。"""
    corpus_path = root / "corpus" / "calculus.md"
    golden_path = root / "eval" / "golden_set" / "v1.jsonl"
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.parent.mkdir(parents=True, exist_ok=True)

    corpus = CORPUS_MD.lstrip("\n")
    corpus_path.write_text(corpus, encoding="utf-8")

    with golden_path.open("w", encoding="utf-8") as f:
        for item in GOLDEN_ITEMS:
            f.write(json.dumps({**item, "version": VERSION}, ensure_ascii=False) + "\n")
    return corpus_path, golden_path


def main() -> None:
    corpus_path, golden_path = generate()
    corpus = corpus_path.read_text(encoding="utf-8")
    n_chars = len(corpus)
    n_block_formula = corpus.count("$$") // 2
    n_tables = len(re.findall(r"^\|[\s:|-]+\|$", corpus, flags=re.MULTILINE))
    n_headings = len(re.findall(r"^#{1,6} ", corpus, flags=re.MULTILINE))
    n_golden = sum(1 for line in golden_path.read_text(encoding="utf-8").splitlines() if line)
    print(f"corpus  : {corpus_path} ({n_chars} chars, {n_headings} headings, "
          f"{n_block_formula} block formulas, {n_tables} tables)")
    print(f"golden  : {golden_path} ({n_golden} questions)")
    print(f"version : {VERSION}")


if __name__ == "__main__":
    main()
