# B2 审计：greedy 逐条回复的 fidelity 与同口径 verified 覆盖对比

> 起因：红审指出摘要卖点 69.4%→100% 是 nominal 口径；主臂 verified mention 58.3%（seed 42 实测），按 100%×58.3% 算主臂 verified=58.3% < greedy 69.4%，"still a gain in absolute terms" 站不住。
> 日期：2026-09-26。

## 1. greedy 的逐条回复 fidelity 测过吗？——没测过，且无法从缓存补测

- fidelity 检查（`merge_fidelity_check.md`）只测了**批量回复**（λ=24_drift seed 42，full_merge 13 批 / full_merge_q3 21 批的真实生成文本）。
- greedy/full_commit 的逐条回复在回放里用的是**占位文本（STUB_REPLY）**，从未真实生成；缓存目录只有评分缓存（score_danmaku 4570 条、qwen、tech）和**批量生成缓存**（merge_gen 25 条）——**没有逐条回复的生成缓存**，fidelity 规则需要回复文本，因此"用缓存对 greedy 跑一次同样检查"在现有产物下不可行（需新生成约 260 条回复的真实调用，且需先定义"逐条回复的 fidelity"口径）。
- 结构性论证（替代测量）：逐条精回的 prompt 只含**一条**目标弹幕（`弹幕内容：<text>` 式单消息生成），生成模型没有"该提谁"的选择空间，fidelity 失败只可能来自完全跑题；批量回复则要在一条里织入 4-10 条，才有"漏提"空间。因此逐条回复 fidelity 结构性 ≈100%，批量 58.3% 的失败模式（漏点名）在单条场景不存在对应机制。此论证可与保真检查的人工校准锚点互证（人工判读显示自动指标偏保守）。

## 2. 审稿人算法的漏洞：58.3% 只适用于批量子集，不适用于全部覆盖

审稿人算式 100%×58.3% 把批量 fidelity 乘到了**全部**覆盖上。但主臂 λ=12_drift 的 100% 名义覆盖由两部分构成（`merge_summary.md`）：

- 精回 35.0%（逐条生成，fidelity 结构性 ≈1）
- 批量覆盖 65.0%（fidelity 实测 58.3%，ID 级 / 59.6% 话题级）

正确的分部件算法：

| 口径 | 主臂 verified 覆盖 | greedy verified 覆盖 |
|---|---|---|
| ID 级 fidelity | 35.0% + 65.0%×0.583 = **72.9%** | 69.4%×~1.0 ≈ **69.4%** |
| 话题级 fidelity | 35.0% + 65.0%×0.596 = **73.7%** | ≈ **69.4%** |

**结论：卖点不反转，但大幅缩水。** 主臂 verified 覆盖 72.9% vs greedy 69.4%，增益从名义 +30.6pp 缩到 **+3.5pp（ID 级）/+4.3pp（话题级）**。论文 §VI-B 自写的 "verified ≈ nominal × fidelity = 58%" 是把批量保真率错误地平乘到含精回的全体上，比审稿人的算法还保守；按分部件算法主臂仍是绝对增益。

## 3. 真正的卖点在哪：低分桶

聚合增益集中在 greedy 放弃的低分桶（λ=12_drift）：

- 低分桶名义覆盖 19.7%→100%；verified ≈ 100%×58.3% = **58.3%**（低分桶几乎全部走批量通道）vs greedy **19.7%**×~1.0 = 19.7% → **verified 口径下仍 +38.6pp**。
- 中/高分桶两臂本来就 ~100%（精回），无差异。

即：聚合的真实价值不是"总覆盖 100%"，而是"**把 greedy 结构性丢弃的低分消息从 19.7% 救到 verified 58%**"。

## 4. 论文诚实改写建议（措辞草案）

1. 摘要与 §VI-A 的头条改为双口径并列："nominal coverage 69.4%→100%; verified mention coverage 69.4%→72.9% (ID-level), with the gain concentrated in the low-score bucket (19.7%→58.3% verified)"。
2. §VI-B 的 "verified ≈ nominal × fidelity" 公式改为分部件公式：verified = precise_rate + batch_cov_rate × fidelity_batch，并注明 fidelity_batch 仅在 λ=24 seed 42 实测（13 批 60 条），搬到 λ=12 是外推假设。
3. 明说逐条精回 fidelity 未实测、结构性近似 1 的论证，并把"补测逐条回复 fidelity"列入 future work。
4. 人评被提及感 4.56/5 继续作为观众侧上限证据，但不再承担"调和反转"的角色——分部件口径下没有反转。

## 5. 残留风险（如实记录）

- fidelity 实测点只有 λ=24 seed 42 一处；λ=12 的批量保真率可能更高（批均 4.6 条 < λ=24 的 4.4/6.7，文本更宽松），也可能因话题漂移而不同——未测。
- "逐条回复 fidelity≈1" 是结构论证不是测量；若审稿人坚持要数字，需约 260 次真实生成调用补测。
