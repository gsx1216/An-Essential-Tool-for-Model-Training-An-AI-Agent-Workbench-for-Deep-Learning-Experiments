# 个人模型凭据仓储检查

## 最新复查结果

你已经按下方改法修正两处核心代码。最新实际仓储已通过17项模拟测试和完整373项回归，真实MySQL独立临时库联调也通过，包含保存/更新/查询/删除、重复删除、用户隔离、未知用户拒绝、SQL错误传播和提交失败回滚。临时库已清理，没有调用模型。

下一步练习见个人模型Key业务练习.md。下文保留最初检查时发现的错误和原因，便于回顾，不表示当前代码仍有这些错误。

## 原始检查结论

本次检查发现两个位置需要修改，get_key方法正确。核心代码保留给你修改；助手只在独立检查副本应用下方修正，验证其可行性。

原16项仓储模拟测试为13项通过、2项断言失败、1项异常。追加“DELETE没有查询结果行”的回归后，共17项中13项通过、2项失败、2项异常。根因仍是下面两个代码位置。

检查副本按下方修改后全部373项回归通过；真实MySQL独立临时库也通过实际仓储的保存、更新、查询、删除、重复删除、用户隔离、错误传播和提交失败回滚。该结果属于修正后的检查副本，不表示项目当前错误代码已经通过。没有调用模型，临时库已清理。

## 错误一：保存SQL参数顺序

文件app/repositories/model_credential_repository.py，save_key第61行：

```python
cursor.execute(self.save_key_sql, (encrypted_key, user_id))
```

保存SQL的列顺序是user_id、provider、encrypted_key，provider已在SQL固定为deepseek，所以两个占位符依次对应user_id和encrypted_key。驱动按位置绑定，不按Python变量名字识别。当前代码会把密文当用户ID，真实写入会失败。

改为：

```python
cursor.execute(self.save_key_sql, (user_id, encrypted_key))
```

## 错误二：DELETE不能用fetchone取被删除记录

delete_key第104～105行：

```python
if cursor.rowcount == 1:
    deleted = cursor.fetchone()[0]
```

DELETE只提供影响行数，没有SELECT那样的结果行。删除命中时，fetchone可能返回None，继续[0]会出错；没有命中时，整个if被跳过，deleted没有赋值，末尾return又会抛UnboundLocalError。

把这两行整体替换为：

```python
deleted = cursor.rowcount == 1
```

这样命中一条时是True，未命中时是False，两条分支都已赋值。保留后面的commit和return；只有提交成功才能返回删除结果。

## get_key为什么正确

execute执行SELECT；fetchone取一行；没有配置时返回None，有配置时一行是(encrypted_key,)，取row[0]得到密文字符串。这与方法声明str | None一致。数据库异常不捕获成None，也正确。

## 调用和事务流程

1. 构造ModelCredentialRepository(password)时，把服务器提供的数据库密码保存到对象，读取4个SQL文件；此时不会连接数据库。
2. 调用方法时先用validate_positive_integer检查用户ID格式。save_key还通过validate_encrypted_key检查密文外形，避免误传明文。它没有主密钥，不验证密文签名或内部归属。
3. get_connection建立本次操作的连接，connection.cursor创建游标。with管理它们的生命周期，不代替写操作的commit。
4. _require_user用get_user.sql检查用户存在。这个检查不创建用户，也不代替登录鉴权；以后传入的user_id必须来自已认证身份。
5. save_key执行保存/更新SQL，然后commit；正常结束隐式返回None。用户外键错误转换为ValueError，其他错误继续抛出，写入或提交失败均rollback。
6. get_key执行查询并返回字符串或None，不提交写入。
7. delete_key执行限定用户和供应商的删除，先把影响行数转换成布尔值，再提交，最后返回。失败回滚，不删除用户、聊天或登录记录。
8. logged_operation包住这些同步方法，记录耗时、结果和安全异常后保持返回值或重新抛出异常；不记录参数、密文、数据库密码。

本次都是同步仓储方法，没有await；以后异步业务层通过asyncio.to_thread调用。ApiKeyCipher负责真正的加解密，仓储负责存取，未来接口负责Token身份和HTTP状态。

## 本次模块与包

| 模块或对象 | 来源 | 当前职责 |
| --- | --- | --- |
| ModelCredentialRepository | 项目仓储 | 保存、读取、删除密文，组织事务 |
| sql目录中的4个文件 | 项目SQL | 保存/查询/删除凭据和查询用户；表的供应商限定在SQL中 |
| get_connection | 项目app.database | 创建MySQL连接，建表迁移也由database模块负责 |
| ApiKeyCipher、MAX_ENCRYPTED_KEY_LENGTH | 项目加密模块 | 前者负责加解密，仓储导入后者限制密文长度 |
| validate_positive_integer | 项目校验模块 | 检查ID范围和类型，排除bool等非法输入 |
| logged_operation | 项目日志模块 | 装饰方法、持久化安全日志、原样传递异常 |
| base64、binascii | Python标准库 | 解码并检查密文外形、处理格式错误；本身不提供加密 |
| Path | Python标准库 | 根据当前文件位置读取项目SQL |
| IntegrityError、errorcode | 第三方mysql-connector-python | 识别外键等完整性错误和对应错误编号 |

## 修改后的检查命令

```powershell
python -m scripts.check_model_credential_repository
python -m unittest discover -s tests
python -m scripts.verify_model_credential_sql_mysql --repository
```

前两条模拟数据库连接。仓储17项检查已经纳入自动回归，不会再出现只运行旧回归而漏掉仓储业务的情况。第三条使用真实MySQL和独立临时库，同时执行SQL和实际仓储；不操作业务库，不调用模型。没有--repository时只验证SQL。

先完成这两处修正并通过检查，再继续业务层把ApiKeyCipher与仓储串起来。
