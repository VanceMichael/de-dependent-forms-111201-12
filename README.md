# 多步骤申报表·版本化续填

面向合作资料接收人员的 Python 采集组件。读取输入表单字段并维护带乐观版本的机构项目草稿，校验选项归属。
输入样本都是本地合成数据，example 域名只表示来源身份，不需联网访问。

## 安装与运行

使用 Python 3.11，依赖仅为标准库，SQLite 随解释器提供，无需安装第三方包。
从项目根目录执行；演示启动真实的回环 HTTP 门户、按实际返回的表单推进，事务入库后读取实际结果。
每次演示使用独立临时目录，结束后清理；通常两秒内结束，不回放历史输出。

```bash
python3 -m compileall -q collector
python3 -m unittest discover -s tests -v
python3 -m collector workflow-demo
python3 -m collector demo
```

`workflow-demo` 读取 `fixtures/forms.json`（步骤定义、依赖选项、审批绑定字段、外部事件）
与 `fixtures/requests.csv`（稳定申请号 + 修订号），完整演示：

1. 两条申请交错录入：机构选择后重新拉取合法项目选项，附件要求随项目变化，步骤令牌不跨行共享；
2. 外部业务人员修改草稿：门户返回版本冲突，程序重新读取草稿并逐字段对照本地输入，暂停等人工裁定，不悄悄覆盖；
3. 外部取消：取消的申请立即停止一切写入，重启后也不会被重新创建；
4. 重启续填：从 SQLite 中已确认的步骤继续，校验失败只影响对应申请；
5. 主管批准：批准记录绑定机构/项目/金额的内容指纹而非裸布尔值；
6. 批准后改金额：门控识别指纹不符并暂停，主管针对新内容重新批准后才能提交。

每个暂停点都在审计轨迹中记录原因、门户实际表单（CSRF、draft_version、选项、附件）与字段对照。

## 版本化录入流程（collector/workflow.py、portal.py、progress.py）

* `portal.py` 是有状态的本地回环门户：按申请号维护草稿版本、每步一次性 CSRF、
  依赖机构的项目选项、依赖项目的附件要求、绑定内容指纹的批准记录，以及外部编辑/取消管理动作。
* `workflow.py` 是录入引擎：CSRF 与 `draft_version` 只取自门户实际返回的表单；
  项目 ID 只用表单 `option value`，不按显示文字猜测；409 冲突触发重读与字段对照，
  提供“以本地为准/以门户为准”两种留痕裁定；审批是人工门控，程序不自行批准。
* `progress.py` 用 SQLite 持久化每行状态、已确认步骤、暂停原因与审计事件；
  取消（canceled）与提交（submitted）是终态，重新出现的输入行不会复活终态申请。
* 暂停原因集中在 `progress.PAUSE_EXPLANATIONS`，复核人员可直接读懂每一处暂停。

## 旧版采集命令（保留兼容）

常规导入保留在本地数据库中，可重复运行；默认路径已忽略，不会进入版本库。

```bash
python3 -m collector import --input fixtures/sample.json --db var/records.sqlite3
python3 -m collector show --db var/records.sqlite3
python3 -m collector serve --db var/records.sqlite3 --port 8080
```

查询服务监听回环地址，`GET /records` 返回数据库中的记录；按 Ctrl-C 停止。

## 组件与数据

`collector/domain.py` 负责来源格式和领域校验（HTML 表单解析、审批指纹、字段对照）；
`storage.py` 保留原始对象、导入凭据和当前记录。
单次导入在同一事务内提交，异常时整体回滚；相同来源和原文只登记一次凭据。
当前记录以领域标识为键，同键再次导入替换当前观察值，不代表保留了完整修订历史。
`transport.py` 提供请求客户端和本地响应服务；服务按路径依次消费响应脚本，并记录实际请求。
`api.py` 提供只读查询入口。`fixtures/sample.json` 是旧导入器支持的输入。
其他附件描述同领域的合成页面、协议或事件，可以由调用方构造本地来源。

## 可复现运行

宿主机或容器均不需要网络。镜像准备好以后可以关闭外部网络再运行上述命令。
仓库内不含真实账号、新闻正文或人员资料；不要将生产凭据写入样本或日志。
