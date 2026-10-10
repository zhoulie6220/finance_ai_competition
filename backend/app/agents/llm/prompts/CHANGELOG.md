# Prompt 变更记录

规则：**改动一律新增版本号，不覆盖旧文件。**

删掉旧版本会让历史 `llm_call` 记录里的 `prompt_hash` 找不到对应文件，
证据链断在那里，而且不会有任何报错。所以只增不删。

版本号进 `llm_call.prompt_version`，内容 sha256 进 `llm_call.prompt_hash`，
整表哈希进 `run_manifest.prompt_registry_hash`。三者都留：版本号给人读，
内容哈希给机器比对，整表哈希判断「这次运行与上次是不是同一套提示词」。

---

## claim_extract

### v3 — 2026-10-10

**取值表加 `management_budget`。**

`claim.claim_type` 的 CHECK 加了这个值（会计口径：首钢的 H 需要一个合法的历史计划兑现
观测来源，见《首钢出分问题解决方案》第二节）。取值表必须跟着加，否则这条测试会红：

    tests/unit/agents/test_prompt_vocabulary.py::test_latest_prompt_matches_db_constraint

它是**从 schema 抠 CHECK 取值、与最新版提示词逐字比对**的——三方同源的那份约束。

提示词里同时写明了这个类型**只用于下一年度的计划目标值，不是已发生的实绩**：
两者在原文里长得极像（都是「营业收入 XXX 亿元」），混起来会让「拿事实核验事实」，
而那种判定**永远判支持**、且把 H 和 C 一起抬上去。

⚠ **没有改 v2 的正文，而是新出一版。** v2 的 sha256 是历史 `llm_call` 证据链的一部分
（见本文件开头），改一个字符就让那些记录对不上文件了。

### v2 — 2026-09-27

**修正 `claim_type` 的取值表。**

v1 让模型从 `cost | demand | capacity | collection | product_mix | risk | other`
里选，但 `claim.claim_type` 的 CHECK 约束只接受：

    demand / order / capacity / collection / product_mix / risk / macro / other

两个问题：

1. **`cost` 不在允许列表里**——模型照 v1 返回 `cost`，INSERT 会被数据库拒绝。
   而拒绝发生在整批写入的中途，前面写进去的回滚、后面的全没写。
2. **少了 `order` 与 `macro`**——模型没有合适的选项时会挑一个「差不多」的，
   而错分类不会报错，只会让主题统计和分项判定悄悄偏掉。

v2 把取值表**逐字对齐数据库的 CHECK**，并明确「选不出来就填 other，不要自造」。

> 教训：**prompt 里的枚举必须与数据库约束同源**。两边各写一份的话，
> 改了一边另一边还是旧的，而错误要到写库那一刻才暴露——
> 那时已经跑了几十次模型调用了。

### v1 — 2026-09-26

初版。从 MD&A 段落抽可验证主张。

要点：
- 明确「可验证」的三个条件（对象 / 期间 / 方向或数值目标），并给了正反例
- 明确「努力、力争、拟、计划」不是保证承诺（会计口径 v1.1 §A.2）
- `text` 必须原文原句不改写——证据链的终点
- `metric_key` 只能从传入的字段表里选，选不到填 null 并说明，**不许自造键名**
  （自造的键名不会报错，只会让这条主张永远匹配不上任何财务事实）
- 允许返回空数组
