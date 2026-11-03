# 个人模型 Key：保存到 MySQL

保存参数顺序和删除结果判断均已修正，并通过仓储17项及完整373项回归和真实临时库联调，详见[个人模型Key仓储检查说明](个人模型Key仓储检查说明.md)。下一步是[个人模型Key业务练习](个人模型Key业务练习.md)；下文的356项和16项是准备仓储练习时的阶段记录。

## 这次做什么

加解密工具已完成，下一步让后端可以保存、读取和删除当前用户的 DeepSeek 密文。本次填写仓储，不写HTTP接口。仓储负责参数校验、SQL和事务；后续业务层负责先加密再保存、读取后解密，接口负责确认Token身份。

## 表里存什么

新表是 experiment_assistant.model_credentials，所有用户共用，不按用户名建表。

| 字段 | 含义 |
| --- | --- |
| user_id | 所属用户，外键关联users.id，不生成新用户 |
| provider | 当前固定deepseek，表示供应商 |
| encrypted_key | ApiKeyCipher.encrypt返回的密文，不保存明文 |
| created_at | 首次创建时间，由保存SQL写入UTC时间 |
| updated_at | 最新保存时间，由保存SQL写入UTC时间 |

user_id和provider共同组成主键：一位用户每个供应商只有一条记录，再次保存更新原记录。不是每次保存都新建一行，也不需要lastrowid。保存SQL使用INSERT ... ON DUPLICATE KEY UPDATE，采用MySQL 8.0.19+的行别名，参见[MySQL官方说明](https://dev.mysql.com/doc/refman/8.0/en/insert-on-duplicate.html)。已用你本机MySQL验证SQL兼容性。

加密主密钥继续由后端保管，本表不保存它。

## 文件已经准备好

| 文件 | 职责 |
| --- | --- |
| sql/create_model_credentials.sql | 建表、联合主键、用户外键和密文长度约束 |
| sql/save_model_credential.sql | 按用户保存或更新DeepSeek密文 |
| sql/get_model_credential.sql | 按用户读取DeepSeek密文 |
| sql/delete_model_credential.sql | 按用户删除DeepSeek配置 |
| app/repositories/model_credential_repository.py | 你填写的仓储骨架，已经加载SQL |
| app/database.py | 已接入新表初始化及单独建表命令 |
| tests/test_model_credential_foundation.py | 已提供部分的自动回归 |
| scripts/check_model_credential_repository.py | 你填完三个方法后运行的16项模拟测试 |
| scripts/verify_model_credential_sql_mysql.py | 独立临时库验证SQL，不调用待填仓储 |

## 你现在填写哪里

打开app/repositories/model_credential_repository.py，按顺序填写9个TODO：

1. save_key的TODO 1～2：execute保存SQL，然后提交事务。
2. get_key的TODO 3～5：execute查询、fetchone取一行、处理None或row[0]。
3. delete_key的TODO 6～9：execute删除、记录rowcount是否等于1、提交、返回布尔值。

完成对应部分后删除NotImplementedError，保留已有输入校验、归属检查、日志和异常回滚。多个返回值的区别：save_key成功返回None；get_key返回密文字符串或None；delete_key返回True/False。

本仓储的三个方法都是同步方法；你本次不需要写async或await。以后由异步业务层通过asyncio.to_thread调用。

## 执行逻辑和几个容易混淆的位置

save_key：校验用户ID → 检查密文外形 → 连接数据库 → 确认用户存在 → 执行保存SQL → 提交。写入或提交失败都会回滚并继续抛出异常。外键作为最终限制，拒绝给不存在的用户保存配置，不会自动创建用户。

get_key：校验用户ID → 确认用户存在 → 查询该用户的DeepSeek记录。fetchone返回None表示没有配置；返回一行时形如(encrypted_key,)，所以需要取row[0]。数据库故障必须继续抛出，不能当成未配置。

delete_key：校验用户ID → 确认用户存在 → 删除该用户的DeepSeek记录 → 在cursor关闭前保存rowcount == 1的布尔结果 → 提交成功后返回结果。删除配置不会删除用户、聊天或登录记录。

_require_user已经在仓储内部调用，不要在调用它之前再查询一次相同用户。以后接口的Token校验负责确认“谁在请求”；这个检查负责确认本次数据库操作所关联的用户仍然存在。不存在用户与存在但未配置密文是不同情况。

validate_encrypted_key只检查Fernet密文外形，目的是防止开发时误传明文。它没有主密钥，不验证签名或用户归属；未来业务层必须先调用真实加密工具，读取后仍需通过解密验证。本接口未来也不接收用户提交的密文。

## 本次引用的包

| 对象 | 来源 | 用途 |
| --- | --- | --- |
| base64、binascii | Python标准库 | 密文编码和结构检查，不负责加密 |
| Path | Python标准库 | 按项目目录加载SQL文件 |
| IntegrityError、errorcode | mysql-connector-python | 处理用户外键错误，其余数据库错误继续抛出 |
| get_connection | 项目数据库模块 | 每次操作创建本次连接，不共用全局游标 |
| logged_operation | 项目日志模块 | 记录操作、耗时和安全失败信息，不记录密文或密码 |
| MAX_ENCRYPTED_KEY_LENGTH | 项目加密模块 | 密文长度上限，保持加密与存储边界一致 |
| validate_positive_integer | 项目校验模块 | 检查用户ID是合法正整数，拒绝bool等输入 |

cursor.execute的第二个参数使用元组，驱动绑定占位符，不把密文拼接进SQL。只有一个参数时写(user_id,)，逗号表示单元素元组。with管理连接与游标生命周期；写入仍需要显式commit，except里的rollback撤销未提交写入，raise继续传递错误。

## 建表与测试

已有项目库和users表的环境，可在项目根目录运行：

```powershell
python -m app.database --model-credential-schema
```

该命令只创建新表，不会清空旧数据。之后完整初始化命令python -m app.database也包含它。助手本次只在独立临时库验证，尚未替你在业务库执行此命令。

填完三个方法后运行：

```powershell
python -m scripts.check_model_credential_repository
```

这16项测试模拟连接，不需要数据库，也不需要真实Key。覆盖保存与提交、重复使用的SQL参数、读取密文/None、删除True/False、未知用户、非法ID、误传明文、数据库故障和回滚。骨架未填写时出现NotImplementedError属于预期。

如需单独复查已提供SQL：

```powershell
python -m scripts.verify_model_credential_sql_mysql
```

这个脚本需要本机MySQL和后端配置的MYSQL_PASSWORD，仅创建和清理自己的随机临时库，不调用模型。它直接验证SQL，不代表你的仓储方法已经通过。

## 当前已经验证什么

现有功能和新提供基础部分共356项自动测试通过。真实临时MySQL已验证新表与重复建表、插入/覆盖后仍只有一条用户记录、按用户读取/删除、密文可还原、外键限制、长度约束、保存/删除回滚和用户记录保留。临时库已清理。

最新进展：save_key、get_key和delete_key已完成修正并通过真实临时库验收，下一步继续写业务层；这组仓储方法尚未接入公开接口。
