# 精确回复保真度 f 实测（红审 v3 洞 2，两臂对称口径）

- 此前 verified = 35.0%×f + 65.0%×0.583 中 f=1.0 只是结构论证；审稿人指出 f=0.90 时卖点归零
- 方法：λ=12_drift 种子42，greedy 与主臂各自精回消息等距抽 60 条，DeepSeek 真实逐条生成
  （prompt = 生产管线同格式 `username：text` + persona 系统提示，温度 0.9，max_tokens 200；缓存 precise_gen）
- 口径与 merge_fidelity 完全相同：ID 保真 = name_hit（精确子串/观众NN数字/≥3字用户名末2字后缀）；
  话题保真 = 弹幕内容 bigram（CJK+数字，去停用字）任一命中；无 bigram 的弹幕不进话题分母
- 抽样：两臂各 60 条（greedy 精回 260 条、主臂精回 126 条中等距抽样）；逐条样本见 precise_fidelity_samples.jsonl

## 实测 f

| 臂 | 样本 | ID 保真 f_id | 话题保真 f_topic（分母=有bigram条目） |
|---|---|---|---|
| greedy | 60 | 13/60 = **21.7%** | 37/55 = **67.3%** |
| 主臂 full_merge_commit | 60 | 9/60 = **15.0%** | 41/58 = **70.7%** |

## 对称修正后的 verified 对比（λ=12 种子42 组件）

- 主臂 verified = 35.0%×f_main + 65.0%×0.583（ID 级；批量保真来自 merge_fidelity_check.md）
- greedy verified = 69.4%×f_greedy（greedy 无批量，全部覆盖都是精回）

| 口径 | greedy | 主臂 | 差 |
|---|---|---|---|
| verified（ID 级） | 15.0% | 43.1% | +28.1pp |
| verified（话题级，批量保真 0.596） | 46.7% | 63.5% | +16.8pp |

## 解读

- 若两臂 f 都 ≈1：结构论证成立，verified 增益维持 +3.5pp，低分桶卖点不变
- 若 f 显著 <1：按上表对称修正后重述头条；注意 f 对两臂同时打折，主臂只有 35% 组件受 f 影响，greedy 100% 组件受 f 影响——f<1 时差距反而**扩大**
