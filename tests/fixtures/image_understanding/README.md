# 中文图意诊断集

24 个图片样本：8 个重复动作/处境转换，4 个普通时间序列，4 个颜色确实承载意义的样本，
4 个证据不足的歧义图，4 张普通照片。成对颜色变体留在同一 dev/holdout 家族。
标签由构造规则和可见资料编写，经过 Codex 图像检查，尚未经过独立人工标注。
这是小规模诊断，不能估计真实 QQ 图片总体准确率。

`baseline.json` 冻结主线 48c86193 的 PRINCIPLES 与 participation_v2；
`cases.json` 分离可见事实、评分参照与输入条件。原始图片任务不会收到参考解释或后续纠正；
facts 条件只收到文字、位置、动作等事实；纠正条件使用固定初始回答，分别提供正确、错误、
模糊纠正，并区分本轮实际有图与原图过期。人格对照只在评测请求中弱化亲密条款。

`scripts/evaluate_image_understanding.py` 离线准备原生 evaluation manifest，分批遵守现有
36 案例/216 尝试、请求费用、输入、图片和 manifest 字节限额。
每个 job 的预留合计再限制在既有 50000 microUSD 结算门槛内；全套 208 次分为17份清单。
模型调用仍由已有授权、预算、usage、journal、UNKNOWN 与 worker 机制负责。
不保存私人截图或聊天；公开照片保留原始下载字节。

首轮为非思考模式、2048 输出 token、每条件一次、无提供方种子。它能做提示和输入的
探索性对照，不能直接代表生产思考模式或证明稳定提升。读取 holdout 结果后不得针对它调提示
再继续把它当未见测试。受限额、失败、UNKNOWN 或截断的尝试都留在分母，禁止只重跑失败。

盲评分开记录感知、图内关系、解释忠实、具体纠正、证据诚实和自然度。应分别报告
错→对与对→错，按任务、类型、dev/holdout 切分，并附实际调用数、usage 成本和延迟。
语义评分必须来自对实际输出的评审；固定模拟回复只证明运行合同。

## 图像来源

合成图由 `scripts/build_image_benchmark.py` 用几何图形和显式 CJK 字体生成；
字体文件不随仓库分发。重新生成需明确传 `--font`；读取既有 fixture 无字体依赖。

公开照片来自 [scikit-image v0.25.2](https://github.com/scikit-image/scikit-image/tree/v0.25.2/skimage/data)：
`chelsea.png`，Stefan van der Walt，CC0；`coffee.png`，Rachel Michetti，CC0；
`brick.png`，源自 CC0Textures，CC0；`clock_motion.png`，Stefan van der Walt，public domain。
授权说明见[官方数据说明](https://scikit-image.org/docs/0.26.x/api/skimage.data.html)。
文件摘要记录在 cases.json；准备评测时重新校验。

评价方法参考 [MemeCap](https://aclanthology.org/2023.emnlp-main.89/) 和
[HallusionBench](https://tianyi-lab.github.io/HallusionBench/)，不复刻其测试集或声称其论文结果。
