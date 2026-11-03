"""将安全的诊断结果排成 Markdown，不接触连接、密码或正文。"""

def display_bytes(value):
    return '未知' if value is None else f'{value:,} B（{value / 1024**2:.3f} MiB）'


def render_capacity_report(report):
    lines = [
        '# 数据库容量与查询计划报告', '',
        f"生成时间 UTC：{report['generated_at_utc']}", '',
        f"本次检查耗时：{report['elapsed_ms']:.3f} ms。这是诊断整体耗时，不是聊天响应时间。", '',
        '## 表空间与估计行数', '',
        '以下为 MySQL 元数据；InnoDB 行数和分配空间为估计，可能缓存或滞后。',
        '数据空间包含聚簇索引，索引空间主要是二级索引；不能理解为正文大小或磁盘剩余量。', '',
        '| 表 | 引擎 | 估计行数 | 数据空间 | 索引空间 |',
        '| --- | --- | ---: | ---: | ---: |',
    ]
    for row in report['tables']:
        count = f"{row['estimated_rows']:,}" if row['estimated_rows'] is not None else '未知'
        lines.append(f"| {row['table_name']} | {row['engine']} | {count} | {display_bytes(row['data_bytes'])} | {display_bytes(row['index_bytes'])} |")
    if report['missing_tables']:
        lines.extend(['', '未发现或当前账号不可见的预期表：' + '、'.join(report['missing_tables']) + '。'])
    lines.extend(['', '## 索引定义', '', '| 表 | 索引 | 第几列 | 列名 | 唯一 | 可见 |',
                  '| --- | --- | ---: | --- | --- | --- |'])
    for row in report['indexes']:
        lines.append(f"| {row['table_name']} | {row['index_name']} | {row['position']} | {row['column_name']} | {'是' if row['unique'] else '否'} | {'是' if row['visible'] else '否'} |")
    message_index = [row for row in report['indexes'] if row['table_name'] == 'chat_messages'
                     and row['index_name'] == 'uq_chat_messages_session_seq']
    if not ([(r['position'], r['column_name']) for r in message_index] == [(1, 'session_id'), (2, 'seq')]
            and all(r['unique'] and r['visible'] for r in message_index)):
        lines.extend(['', '检查提示：未发现预期可见的 (session_id, seq) 唯一索引定义，请人工核对；脚本不会自动改表。'])

    session = report['session']
    if session is None:
        lines.extend(['', '未指定会话：已跳过精确消息统计和业务查询计划。'])
    else:
        settings = report['settings']
        lines.extend(['', '## 指定归属会话的有限范围统计', '',
                      f"范围：seq > {settings['after_seq']}，最多 {settings['sample_limit']} 条真实消息；序号缺口不会补造。",
                      f"本范围实际统计：{session['inspected_messages']} 条；正文 UTF-8 字节合计：{display_bytes(session['content_bytes'])}。",
                      f"本范围首尾序号：{session['first_seq']} / {session['last_seq']}。",
                      f"范围之后仍有消息：{'是；以上不是完整会话总量' if session['has_more'] else '否；已覆盖此起点后的全部现存消息'}。",
                      f"摘要覆盖到 seq={session['summarized_until_seq']}，版本={session['summary_version']}；0 / None 表示尚无摘要。", '',
                      '## 业务查询计划', '',
                      '与实际 SQL 使用同一文件和参数形状；仅执行普通 EXPLAIN，不读取原始正文结果。',
                      'rows 是优化器估计值，不能当作实际扫描量或响应耗时。', '',
                      '| 查询 | 表别名 | 访问方式 | 选择的索引 | 估计行数 | filesort | 临时表 |',
                      '| --- | --- | --- | --- | ---: | --- | --- |'])
        for query, rows in session['plans'].items():
            for row in rows:
                lines.append(f"| {query} | {row['table']} | {row['access_type']} | {row['key']} | {row['estimated_rows']} | {row['filesort']} | {row['temporary']} |")
        lines.extend(['', '访问方式 ALL、filesort 或未选择索引，需要结合表规模和过滤范围判断；小表也可能合理地扫描。',
                      '计划退化可提示进一步排查，但本工具不据此自动断言需要分表。'])
    lines.extend(['', '## 边界与后续', '',
                  f"本连接只读事务；SELECT 超时配置 {report['settings']['max_execution_ms']} ms（每条，不是整体硬期限）；元数据锁等待另限 3 秒。",
                  '只展示结构和数量，不展示消息、摘要正文、用户名、密码、Token 或模型 Key。',
                  f"报告只覆盖 {report.get('tracked_table_count', len(report['tables']) + len(report['missing_tables']))} 张预期业务表；未统计数据库其他对象、日志、备份和操作系统剩余空间。",
                  '摘要缩短模型输入，不删除历史。后续按增长趋势和真实负载决定归档策略。',
                  '本报告不是并发压测或生产容量承诺。', ''])
    return '\n'.join(lines)
