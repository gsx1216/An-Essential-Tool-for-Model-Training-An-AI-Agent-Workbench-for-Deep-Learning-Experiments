# TODO 3：历史如何分组、如何返回

2026-10-25，按用户要求实现 ContextManager._split_for_summary。应用代码只修改这个方法，其他 TODO 和业务流程保持原样。

## 1. 这个方法做什么

输入是已经通过校验的 ContextMessage 对象列表，末尾是当前等待回复的问题。方法返回：

```python
older, recent
```

older 是待总结的较早原文；recent 是保留的近期原文加当前问题。两者都是平坦的对象列表，不是分别存放 human 和 ai，也不是分组列表。是否真的调用模型总结，由后续 prepare 根据输入预算判断。

## 2. 先保留当前问题

遍历 history[:-1]，只处理之前的消息。history[-1] 是当前问题，最后加到 recent。切片不会删除原列表元素。

方法的基础检查会拒绝空列表、非消息元素和末尾不是 human 的输入。完整的正文、角色和真实序号校验仍应由 prepare 在调用前执行 validate_history；本方法没有 summary 参数，不能单独判断摘要覆盖范围。

## 3. 为什么用 groups

```python
groups = [
    [问题1对象, 回复1对象],
    [问题2对象, 回复2对象],
]
```

每个组保存一轮对话或缺口两侧的独立片段。原来分别创建 human 和 ai 两个列表会破坏对话归属关系；previous_list 没有实际作用，也已移除。

## 4. 每条消息如何决定去哪个组

```python
has_gap = (
    previous is not None
    and message.seq != previous.seq + 1
)

if previous is None or message.role == "human" or has_gap:
    groups.append([message])
else:
    groups[-1].append(message)

previous = message
```

- 第一条：没有已有组，创建第一个组。
- 当前角色是 human：新的用户提问，创建新组。
- 与上一条之间缺号：创建独立片段，不把两侧硬配成一轮。
- 其他情况：把当前回复放进最后一组。

previous 保存上一个完整消息对象；比较时取 .seq，不使用列表下标冒充真实序号。and 会短路：previous 为 None 时，不会继续访问 previous.seq。

每次处理完成都执行 previous = message。不能在第一次循环直接 continue，否则可能跳过更新，让 previous 一直为 None。

## 5. 遇到缺口的例子

输入：

```text
1 human：问题1
2 ai：回复1
3 human：问题3
5 ai：缺口后的实际回复
6 human：问题6
7 ai：回复7
8 human：当前问题
```

先排除当前问题，得到：

```text
第1组：[1, 2]
第2组：[3]
第3组：[5]
第4组：[6, 7]
```

保留最近两组时：

```text
older：[1, 2, 3]
recent：[5, 6, 7, 8]
```

缺口决定分组边界；保留组数决定 older/recent 边界。不是一遇到缺口就把之前全部总结。只有带完整问答时，组数才等同于正常对话轮数；有缺口时，不完整片段也计一组并保留真实内容。

## 6. 分组以后如何变回两个平坦列表

```python
older = []
for group in groups[:-self.keep_recent_turns]:
    older.extend(group)

recent = []
for group in groups[-self.keep_recent_turns:]:
    recent.extend(group)

recent.append(history[-1])
return older, recent
```

- groups[:-N]：最后 N 组之前的组。
- groups[-N:]：最后 N 组；总数不足时自动取全部。
- extend(group)：把一组中的消息逐条放入结果，避免列表嵌套。
- recent.append(history[-1])：把当前问题作为一个完整对象加入。
- return older, recent：返回一个包含两个列表的元组，调用方可用两个变量接收。

没有更早历史时，older 是空列表；只有当前问题时，recent 是包含该对象的新列表。所有原消息的顺序、序号和正文均不改变，不添加假的 AI 回复，也不修改数据库。

本方法只有 Python 内存中的列表操作，不需要 async、await、try、数据库连接或额外第三方包。ContextMessage 是项目自己的数据类，list、append、extend 和切片都是 Python 内置能力。

## 7. 验证方式与范围

```powershell
python -m scripts.check_context_manager --part split
python -m unittest tests.test_context_summary_split -v
```

2 项练习检查与 8 项新增回归已通过。用例包含连续历史、刚好达到保留组数、历史不足、只有当前问题、缺失提问或回复、孤立回复、缺口不决定前后切分位置、输入对象和顺序保持不变。

完整常规回归共 548 项通过。这不表示尚未完成的 prepare 或 TODO 2 已通过验收。

这些检查不连接数据库、Redis 或真实模型。完整自动摘要仍需完成 prepare，并处理其他尚未完成的步骤。

TODO 3 完成时发现 TODO 2 的 list.append(self.build_history_messages(...)) 会形成嵌套列表，当时未修改。2026-10-25 实现 TODO 4 时，已将这处必要的前置错误修正为 extend，详情见 [TODO4无需摘要分支说明.md](TODO4无需摘要分支说明.md)。
