# R3 替代目标与交互行为模式代理

使用原 k=2 基线决策，不与 R1 服务时间变化混杂。主分析仅 λ=12/24 drift 五种子；其余流只留在 CSV 作描述。

`U(wB)=[Σprecise score+wB·Σbatch score]/(80N)`；N 为全部弹幕到达，未回复贡献 0，wP=1，wB=0/0.25/0.5/0.75/1。
U 是覆盖加权判定分，不是观众满意度或真人感。小礼物 `price<=100` 电池，高价值礼物 `>100`。
最长连续精回在每个运行内按全部回应动作排序，批量或任何其他精回都会中断；不跨种子拼接。
较少逐条谢小礼物不自动等于更好，这些指标只称 interaction-pattern proxies。

## 主分析：替代目标

| λ | 策略 | U(0) | U(.25) | U(.5) | U(.75) | U(1) | 精回均分 | 总覆盖% |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 12 | fifo | 0.352 | 0.352 | 0.352 | 0.352 | 0.352 | 39.29 | 71.61 |
| 12 | full_commit | 0.434 | 0.434 | 0.434 | 0.434 | 0.434 | 48.75 | 71.28 |
| 12 | full_merge_commit | 0.276 | 0.330 | 0.383 | 0.437 | 0.491 | 55.41 | 100.00 |
| 12 | full_merge_q3_commit | 0.276 | 0.330 | 0.383 | 0.437 | 0.491 | 55.41 | 100.00 |
| 12 | greedy | 0.437 | 0.437 | 0.437 | 0.437 | 0.437 | 49.20 | 71.06 |
| 24 | fifo | 0.185 | 0.185 | 0.185 | 0.185 | 0.185 | 40.38 | 36.58 |
| 24 | full_commit | 0.267 | 0.267 | 0.267 | 0.267 | 0.267 | 59.80 | 35.78 |
| 24 | full_merge_commit | 0.239 | 0.251 | 0.263 | 0.276 | 0.288 | 60.70 | 41.81 |
| 24 | full_merge_q3_commit | 0.226 | 0.249 | 0.272 | 0.295 | 0.319 | 60.80 | 50.03 |
| 24 | greedy | 0.271 | 0.271 | 0.271 | 0.271 | 0.271 | 60.88 | 35.64 |

## 主分析：同种子相对 greedy 的效用差

| λ | 策略 | ΔU(0) | ΔU(.25) | ΔU(.5) | ΔU(.75) | ΔU(1) |
|---:|---|---:|---:|---:|---:|---:|
| 12 | fifo | -0.085 [-0.100,-0.073] | -0.085 [-0.100,-0.073] | -0.085 [-0.100,-0.073] | -0.085 [-0.100,-0.073] | -0.085 [-0.100,-0.073] |
| 12 | full_commit | -0.003 [-0.008,+0.001] | -0.003 [-0.008,+0.001] | -0.003 [-0.008,+0.001] | -0.003 [-0.008,+0.001] | -0.003 [-0.008,+0.001] |
| 12 | full_merge_commit | -0.161 [-0.172,-0.138] | -0.107 [-0.114,-0.091] | -0.054 [-0.057,-0.044] | -0.000 [-0.008,+0.002] | +0.054 [+0.041,+0.061] |
| 12 | full_merge_q3_commit | -0.161 [-0.172,-0.138] | -0.107 [-0.114,-0.091] | -0.054 [-0.057,-0.044] | -0.000 [-0.008,+0.002] | +0.054 [+0.041,+0.061] |
| 24 | fifo | -0.086 [-0.091,-0.077] | -0.086 [-0.091,-0.077] | -0.086 [-0.091,-0.077] | -0.086 [-0.091,-0.077] | -0.086 [-0.091,-0.077] |
| 24 | full_commit | -0.004 [-0.006,-0.002] | -0.004 [-0.006,-0.002] | -0.004 [-0.006,-0.002] | -0.004 [-0.006,-0.002] | -0.004 [-0.006,-0.002] |
| 24 | full_merge_commit | -0.033 [-0.051,-0.023] | -0.020 [-0.030,-0.017] | -0.008 [-0.010,-0.003] | +0.005 [-0.004,+0.012] | +0.017 [+0.003,+0.034] |
| 24 | full_merge_q3_commit | -0.046 [-0.052,-0.040] | -0.022 [-0.027,-0.017] | +0.001 [-0.008,+0.006] | +0.024 [+0.011,+0.034] | +0.048 [+0.030,+0.063] |

Δ 单元格为五种子配对差均值 [最小值,最大值]。

## 主分析：交互行为模式代理

| λ | 策略 | 小礼物到达 | 小礼物精回 | 小礼物总回应 | 最长连续小礼物精回（均值/最大） | 高价值到达/精回/总回应 |
|---:|---|---:|---:|---:|---:|---:|
| 12 | fifo | 34 | 24 | 24 | 1.20/2 | 0/0/0 |
| 12 | full_commit | 34 | 34 | 34 | 1.40/2 | 0/0/0 |
| 12 | full_merge_commit | 34 | 34 | 34 | 1.20/2 | 0/0/0 |
| 12 | full_merge_q3_commit | 34 | 34 | 34 | 1.20/2 | 0/0/0 |
| 12 | greedy | 34 | 34 | 34 | 1.40/2 | 0/0/0 |
| 24 | fifo | 60 | 26 | 26 | 1.00/1 | 0/0/0 |
| 24 | full_commit | 60 | 60 | 60 | 1.80/2 | 0/0/0 |
| 24 | full_merge_commit | 60 | 60 | 60 | 2.20/4 | 0/0/0 |
| 24 | full_merge_q3_commit | 60 | 60 | 60 | 1.80/2 | 0/0/0 |
| 24 | greedy | 60 | 60 | 60 | 1.80/2 | 0/0/0 |

## 逐运行明细（含描述性流）

| 角色 | 流 | 策略 | N | U(0) | U(.5) | U(1) | 小礼物 到/精/总 | 最长连谢 | 高价值 到/精/总 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| descriptive | ovl_l3_uniform | fifo | 180 | 0.479 | 0.479 | 0.479 | 2/2/2 | 1 | 0/0/0 |
| descriptive | ovl_l3_uniform | greedy | 180 | 0.479 | 0.479 | 0.479 | 2/2/2 | 1 | 0/0/0 |
| descriptive | ovl_l3_uniform | full_commit | 180 | 0.479 | 0.479 | 0.479 | 2/2/2 | 1 | 0/0/0 |
| descriptive | ovl_l3_uniform | full_merge_commit | 180 | 0.479 | 0.479 | 0.479 | 2/2/2 | 1 | 0/0/0 |
| descriptive | ovl_l3_uniform | full_merge_q3_commit | 180 | 0.479 | 0.479 | 0.479 | 2/2/2 | 1 | 0/0/0 |
| descriptive | ovl_l3_drift | fifo | 180 | 0.510 | 0.510 | 0.510 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l3_drift | greedy | 180 | 0.510 | 0.510 | 0.510 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l3_drift | full_commit | 180 | 0.510 | 0.510 | 0.510 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l3_drift | full_merge_commit | 180 | 0.510 | 0.510 | 0.510 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l3_drift | full_merge_q3_commit | 180 | 0.510 | 0.510 | 0.510 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l6_uniform | fifo | 180 | 0.468 | 0.468 | 0.468 | 4/4/4 | 1 | 0/0/0 |
| descriptive | ovl_l6_uniform | greedy | 180 | 0.468 | 0.468 | 0.468 | 4/4/4 | 2 | 0/0/0 |
| descriptive | ovl_l6_uniform | full_commit | 180 | 0.468 | 0.468 | 0.468 | 4/4/4 | 2 | 0/0/0 |
| descriptive | ovl_l6_uniform | full_merge_commit | 180 | 0.466 | 0.467 | 0.468 | 4/4/4 | 2 | 0/0/0 |
| descriptive | ovl_l6_uniform | full_merge_q3_commit | 180 | 0.466 | 0.467 | 0.468 | 4/4/4 | 2 | 0/0/0 |
| descriptive | ovl_l6_drift | fifo | 180 | 0.499 | 0.499 | 0.499 | 0/0/0 | 0 | 0/0/0 |
| descriptive | ovl_l6_drift | greedy | 180 | 0.499 | 0.499 | 0.499 | 0/0/0 | 0 | 0/0/0 |
| descriptive | ovl_l6_drift | full_commit | 180 | 0.499 | 0.499 | 0.499 | 0/0/0 | 0 | 0/0/0 |
| descriptive | ovl_l6_drift | full_merge_commit | 180 | 0.499 | 0.499 | 0.499 | 0/0/0 | 0 | 0/0/0 |
| descriptive | ovl_l6_drift | full_merge_q3_commit | 180 | 0.499 | 0.499 | 0.499 | 0/0/0 | 0 | 0/0/0 |
| descriptive | ovl_l12_uniform | fifo | 360 | 0.347 | 0.347 | 0.347 | 9/7/7 | 2 | 0/0/0 |
| descriptive | ovl_l12_uniform | greedy | 360 | 0.439 | 0.439 | 0.439 | 9/9/9 | 1 | 0/0/0 |
| descriptive | ovl_l12_uniform | full_commit | 360 | 0.439 | 0.439 | 0.439 | 9/9/9 | 2 | 0/0/0 |
| descriptive | ovl_l12_uniform | full_merge_commit | 360 | 0.268 | 0.383 | 0.498 | 9/9/9 | 1 | 0/0/0 |
| descriptive | ovl_l12_uniform | full_merge_q3_commit | 360 | 0.268 | 0.383 | 0.498 | 9/9/9 | 1 | 0/0/0 |
| primary | ovl_l12_drift | fifo | 360 | 0.342 | 0.342 | 0.342 | 10/6/6 | 1 | 0/0/0 |
| primary | ovl_l12_drift | greedy | 360 | 0.415 | 0.415 | 0.415 | 10/10/10 | 2 | 0/0/0 |
| primary | ovl_l12_drift | full_commit | 360 | 0.415 | 0.415 | 0.415 | 10/10/10 | 2 | 0/0/0 |
| primary | ovl_l12_drift | full_merge_commit | 360 | 0.247 | 0.360 | 0.473 | 10/10/10 | 1 | 0/0/0 |
| primary | ovl_l12_drift | full_merge_q3_commit | 360 | 0.247 | 0.360 | 0.473 | 10/10/10 | 1 | 0/0/0 |
| descriptive | ovl_l24_uniform | fifo | 720 | 0.172 | 0.172 | 0.172 | 3/0/0 | 0 | 0/0/0 |
| descriptive | ovl_l24_uniform | greedy | 720 | 0.269 | 0.269 | 0.269 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l24_uniform | full_commit | 720 | 0.270 | 0.270 | 0.270 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l24_uniform | full_merge_commit | 720 | 0.217 | 0.269 | 0.322 | 3/3/3 | 1 | 0/0/0 |
| descriptive | ovl_l24_uniform | full_merge_q3_commit | 720 | 0.197 | 0.296 | 0.394 | 3/3/3 | 1 | 0/0/0 |
| primary | ovl_l24_drift | fifo | 720 | 0.183 | 0.183 | 0.183 | 7/5/5 | 1 | 0/0/0 |
| primary | ovl_l24_drift | greedy | 720 | 0.273 | 0.273 | 0.273 | 7/7/7 | 2 | 0/0/0 |
| primary | ovl_l24_drift | full_commit | 720 | 0.269 | 0.269 | 0.269 | 7/7/7 | 2 | 0/0/0 |
| primary | ovl_l24_drift | full_merge_commit | 720 | 0.243 | 0.265 | 0.286 | 7/7/7 | 2 | 0/0/0 |
| primary | ovl_l24_drift | full_merge_q3_commit | 720 | 0.221 | 0.279 | 0.336 | 7/7/7 | 2 | 0/0/0 |
| primary | ovl_l12_drift_s1001 | fifo | 360 | 0.361 | 0.361 | 0.361 | 3/3/3 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s1001 | greedy | 360 | 0.442 | 0.442 | 0.442 | 3/3/3 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s1001 | full_commit | 360 | 0.442 | 0.442 | 0.442 | 3/3/3 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s1001 | full_merge_commit | 360 | 0.269 | 0.386 | 0.502 | 3/3/3 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s1001 | full_merge_q3_commit | 360 | 0.269 | 0.386 | 0.502 | 3/3/3 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s2002 | fifo | 360 | 0.377 | 0.377 | 0.377 | 5/2/2 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s2002 | greedy | 360 | 0.453 | 0.453 | 0.453 | 5/5/5 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s2002 | full_commit | 360 | 0.451 | 0.451 | 0.451 | 5/5/5 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s2002 | full_merge_commit | 360 | 0.315 | 0.409 | 0.502 | 5/5/5 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s2002 | full_merge_q3_commit | 360 | 0.315 | 0.409 | 0.502 | 5/5/5 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s3003 | fifo | 360 | 0.321 | 0.321 | 0.321 | 10/9/9 | 2 | 0/0/0 |
| primary | ovl_l12_drift_s3003 | greedy | 360 | 0.418 | 0.418 | 0.418 | 10/10/10 | 2 | 0/0/0 |
| primary | ovl_l12_drift_s3003 | full_commit | 360 | 0.414 | 0.414 | 0.414 | 10/10/10 | 2 | 0/0/0 |
| primary | ovl_l12_drift_s3003 | full_merge_commit | 360 | 0.246 | 0.362 | 0.477 | 10/10/10 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s3003 | full_merge_q3_commit | 360 | 0.246 | 0.362 | 0.477 | 10/10/10 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s4004 | fifo | 360 | 0.358 | 0.358 | 0.358 | 6/4/4 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s4004 | greedy | 360 | 0.458 | 0.458 | 0.458 | 6/6/6 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s4004 | full_commit | 360 | 0.450 | 0.450 | 0.450 | 6/6/6 | 1 | 0/0/0 |
| primary | ovl_l12_drift_s4004 | full_merge_commit | 360 | 0.302 | 0.400 | 0.498 | 6/6/6 | 2 | 0/0/0 |
| primary | ovl_l12_drift_s4004 | full_merge_q3_commit | 360 | 0.302 | 0.400 | 0.498 | 6/6/6 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s1001 | fifo | 720 | 0.171 | 0.171 | 0.171 | 15/6/6 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s1001 | greedy | 720 | 0.262 | 0.262 | 0.262 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s1001 | full_commit | 720 | 0.260 | 0.260 | 0.260 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s1001 | full_merge_commit | 720 | 0.211 | 0.253 | 0.296 | 15/15/15 | 4 | 0/0/0 |
| primary | ovl_l24_drift_s1001 | full_merge_q3_commit | 720 | 0.214 | 0.263 | 0.312 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s2002 | fifo | 720 | 0.190 | 0.190 | 0.190 | 12/6/6 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s2002 | greedy | 720 | 0.274 | 0.274 | 0.274 | 12/12/12 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s2002 | full_commit | 720 | 0.269 | 0.269 | 0.269 | 12/12/12 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s2002 | full_merge_commit | 720 | 0.248 | 0.266 | 0.283 | 12/12/12 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s2002 | full_merge_q3_commit | 720 | 0.234 | 0.280 | 0.325 | 12/12/12 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s3003 | fifo | 720 | 0.186 | 0.186 | 0.186 | 15/4/4 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s3003 | greedy | 720 | 0.263 | 0.263 | 0.263 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s3003 | full_commit | 720 | 0.258 | 0.258 | 0.258 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s3003 | full_merge_commit | 720 | 0.240 | 0.253 | 0.266 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s3003 | full_merge_q3_commit | 720 | 0.217 | 0.255 | 0.293 | 15/15/15 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s4004 | fifo | 720 | 0.193 | 0.193 | 0.193 | 11/5/5 | 1 | 0/0/0 |
| primary | ovl_l24_drift_s4004 | greedy | 720 | 0.283 | 0.283 | 0.283 | 11/11/11 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s4004 | full_commit | 720 | 0.281 | 0.281 | 0.281 | 11/11/11 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s4004 | full_merge_commit | 720 | 0.250 | 0.280 | 0.311 | 11/11/11 | 2 | 0/0/0 |
| primary | ovl_l24_drift_s4004 | full_merge_q3_commit | 720 | 0.241 | 0.285 | 0.328 | 11/11/11 | 2 | 0/0/0 |
