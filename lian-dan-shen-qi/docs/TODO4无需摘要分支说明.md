# TODO 4：校验、计数和无需摘要的返回

后续已完成摘要与正式聊天路由接入，当前流程见 [聊天接口接入上下文与任务校验说明](聊天接口接入上下文与任务校验说明.md)。本文保留早期短输入分支的教学说明。

2026-10-25，按用户要求实现 prepare 的 TODO 4，并补充当前长输入的自动精简。后续已完成旧历史摘要 TODO 5、6，详见 [历史自动摘要实现与检查说明](历史自动摘要实现与检查说明.md)。本文主要解释无需历史摘要的分支。

## 1. 本次完成什么

```text
输入 history 和旧 summary
    ↓
validate_history 检查输入
    ↓
单独检查系统提示词 + 当前问题的最低输入占用
    ├─ 超过硬预算：保留原文 → 精简本次问题的副本 → 完整重计数
    │                ├─ 容纳：返回本次输入
    │                └─ 仍超限：明确提示拆分任务
    └─ 未超过：组装全部候选上下文并计数
                  ↓
             未达到摘要阈值？
                  ├─ 是：返回 PreparedContext，不调用摘要器
                  └─ 否：进入已实现的旧历史摘要分支
```

TODO 3 决定如何分成 older/recent，但不是每次都必须执行。输入未达到阈值时，既不切掉早期历史，也不重新总结，原摘要和所有未总结原文直接用于本次请求。

## 2. 保留你已写的校验调用

```python
self.validate_history(history=history, summary=summary)
```

校验通过返回 None，表示可以继续，不需要把返回值当作消息。校验失败，异常向上传递，后面的下标访问和计数不会执行。

## 3. 为什么先单独检查当前问题

```python
current_question = history[-1]
required_messages = [
    ("system", self.system_prompt),
    ("human", current_question.content),
]
required_tokens = self._count_input(required_messages)
```

这是最低需要处理的内容：系统提示词和当前问题。当前问题不能暗中截短，也不能为了计数而覆盖原始聊天记录。

如果这部分超过 self.max_input_tokens，现在会 await self._prepare_long_input(history, summary)。它保留原文，为本次请求生成带标记的精简稿，再把旧摘要、历史、缺口提示全部组装并重新计数。能够放入硬预算时继续，仍过长时提示拆分任务；系统提示词本身占满预算或精简器未配置时明确报错。日志不打印问题正文。

这里不用 _assemble_messages([current_question], None)：当前问题的真实 seq 可能大于 1，那样会把前面已有历史错误解释为缺口，并引入不必要标记。直接构造两条必需消息，不改变原问题或真实序号。

这只是独立模块的处理。将来接入聊天时，仍要保存用户原文和明确的处理状态，精简失败不能把原文替换成失败提示或空内容。

## 4. 然后才组装完整上下文

```python
model_messages = self._assemble_messages(history, summary)
input_tokens = self._count_input(model_messages)
```

这里包含：

- 系统提示词。
- 旧摘要和“这是历史资料”的标记（有旧摘要时）。
- 未总结的真实原文。
- 缺口与不完整摘要的资料提示（存在缺口时）。
- 当前问题，且只出现一次。

所有这些内容都计入输入预算，不能只数用户问题正文，也不能漏掉摘要或缺口标记。

计数器由外部传入。当前练习中的 FakeTokenCounter 用字符长度和固定开销作为测试刻度，不是 DeepSeek 的真实 token 数；正式接入前会适配真实计数或明确的估算策略，预留输出和安全余量。

## 5. 缺口信息不能因为返回而丢失

```python
previous_gaps = () if summary is None else summary.missing_ranges
missing_ranges = previous_gaps + self.find_history_gaps(history, summary)
```

前半段是旧摘要已经记录的缺口，后半段是当前原文中的缺口。加号在这里拼接两个元组，不会修改原摘要。

例如旧摘要覆盖 1～10，其中 2～3 缺失；当前原文从 13 开始，那么返回的范围包括 2～3 和 11～12。模型输入中也带明确的资料标记，不能伪造缺失事实。

## 6. 没达到阈值时返回什么

```python
if input_tokens < self.compact_at_tokens:
    return PreparedContext(
        model_messages=model_messages,
        summary=summary,
        input_tokens=input_tokens,
        was_compacted=False,
        missing_ranges=missing_ranges,
    )
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| model_messages | 真正准备交给回答模型的消息列表 |
| summary | 原来的摘要，未重新生成，可能为 None |
| input_tokens | 本次计数器返回的完整输入占用 |
| was_compacted | False，表示本次没有生成新摘要 |
| missing_ranges | 摘要和原文已知的缺失范围 |
| input_compressed | 当前问题是否使用精简稿，普通短输入默认为 False |

PreparedContext 是本项目的数据对象，不是模型回复、登录 Token 或数据库记录。调用方以后使用 result.model_messages 交给回答模型。短输入分支没有模型请求；长输入分支会调用注入的精简器，但这里仍不保存任何数据。

输入等于触发阈值时，也进入摘要分支，因为条件使用小于号。触发阈值不能大于硬预算，这一点在构造函数中已经校验。

## 7. 必要的前置修正

TODO 2 原来写的是：

```python
list.append(self.build_history_messages(history, summary))
```

它会把整份列表作为一个元素塞进去，产生嵌套结构。计数器按 (role, content) 遍历时会失败。本次只将 append 改为 extend，使历史消息逐条加入；没有改变消息选择、摘要提示或缺口策略。

局部变量名 list 仍沿用你的代码；后面可以改成 messages 避免遮蔽内置类型，这属于命名改进，不是本次错误原因。

## 8. async、包和日志在这里做什么

- 校验和内存计数是同步方法，调用时不加 await。
- 长输入分支会 await 精简器，历史摘要分支也会 await 摘要模型；练习通过 asyncio.run 执行 prepare。
- ContextMessage、ContextSummary、PreparedContext 和 ContextTooLargeError 都来自项目自己的 app.models.context_state。
- @logged_operation 复用项目现有的持久化安全日志，记录结果、代码位置和固定异常分类，不记录输入或返回正文。
- 本次没有增加新的第三方依赖。

## 9. 练习检查

```powershell
python -m scripts.check_context_manager --part assembly
python -m scripts.check_context_manager --part prepare-basic
python -m unittest tests.test_context_prepare_basic -v
```

前两组分别有 2 项检查。prepare-basic 现在检查短输入直接返回、长输入使用假精简器后继续。常规测试还覆盖旧摘要复用、缺口、精简器未配置、系统提示词占用、错误输入和计数器异常；新增长输入专项检查见配套说明。

计数、摘要和精简均使用假对象，测试不访问数据库、Redis 或供应商。旧历史摘要、长问题精简和二者衔接现在均已实现，但尚未接入正式聊天接口；不能把模块检查通过当作整条真实聊天链路已经通过。

最新流程与测试结果见 [历史自动摘要实现与检查说明](历史自动摘要实现与检查说明.md)。
