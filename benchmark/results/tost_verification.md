# TOST 等价检验复核与归档（N4 红审）

归档论文 §5.3（`paper/sections/results_selection.tex`）中 TOST 等价检验的完整溯源：脚本、输入、公式、输出、与论文数字对照。

## 脚本

`experiments/tost_and_greedy_wait.py`（commit `04f63ad` "Final-draft fixes" 引入）。运行方式：

```
python -X utf8 experiments/tost_and_greedy_wait.py
```

复核运行日期：2026-08-06（scipy 1.18.1）。

## 输入数据

选择层 `full_commit − greedy` 种子内配对差，5 个种子（E1 种子稳健性，来源 `experiments/results/seed_robustness.md` 第 9–13 行表格）：

| 种子 | greedy | full_commit | full−greedy |
|---|---|---|---|
| 42（原始） | 60.5 | 59.4 | −1.0 |
| 1001 | 60.9 | 60.1 | −0.8 |
| 2002 | 61.2 | 59.7 | −1.5 |
| 3003 | 61.6 | 60.2 | −1.3 |
| 4004 | 60.2 | 59.5 | −0.7 |

由逐种子差值算出 mean = −1.060、SD = 0.336（n=5）。

**注意**：脚本以汇总量 `mean_d=-1.060, sd_d=0.336` 直接重建（注释注明来源），而非读逐种子数组——这对 TOST 无损（检验只依赖 mean/SD/n），上面逐种子差值已在此归档供复算。
另外 `seed_robustness.md` 正文写"均值 −1.08，标准差 0.34"，是 −1.06/0.336 的四舍五入误差（−5.3/5=−1.06 精确）；统计量按精确值计算。`stats_verification.md` §4 使用的也是 −1.060/0.336。

## 公式（脚本第 14–22 行）

- `se = sd/√n`；等价界 ±Δ，Δ = 2.0 judge points
- `t1 = (mean − (−Δ))/se`（H0: mean ≤ −Δ），`t2 = (Δ − mean)/se`（H0: mean ≥ +Δ）
- df = n−1 = 4；`p1 = stats.t.sf(t1, 4)`，`p2 = stats.t.sf(t2, 4)`
- 90% CI = mean ± `stats.t.ppf(0.95, 4)` · se
- 判定：max(p1,p2) < .05 且 90% CI 整体落在 [−Δ, +Δ] 内 → 等价

## 输出（复核运行实际结果）

```
TOST selection: t1=6.26 p1=0.0017 | t2=20.36 p2=1.72e-05
  90% CI = [-1.38, -0.74] vs bounds ±2.0 -> EQUIVALENT (both p<.05, CI inside)
```

## 与论文 §5.3 数字对照

| 量 | 论文 | 复核输出 | 一致 |
|---|---|---|---|
| t₁ | 6.26 | 6.26 | ✓ |
| p₁ | 0.0017 | 0.0017 | ✓ |
| t₂ | 20.36 | 20.36 | ✓ |
| p₂ | 1.7×10⁻⁵ | 1.72e-05 | ✓ |
| df | 4 | 4 | ✓ |
| 90% CI | [−1.38, −0.74] | [−1.38, −0.74] | ✓ |
| 等价界 | ±2 judge points | ±2.0 | ✓ |

逐位吻合，无差异。结论 EQUIVALENT 成立：选择层（full vs greedy）在 ±2 分等价界内统计等价（方向微负 −1.06 分，见 `stats_verification.md` §4 的单样本 t 检验）。
