# 本地运行说明

先运行单元测试，再执行演示命令。演示从干净临时目录发起真实请求并处理响应，不需要外部站点。

```bash
python3 -m unittest discover -s tests -v
python3 -m collector workflow-demo
```

## 版本化录入的暂停与处置

录入过程中每条申请独立推进，出现以下情况时该行暂停并写入审计轨迹，其他申请继续办理：

| 暂停原因 | 触发条件 | 处置入口 |
| --- | --- | --- |
| `version_conflict` | 门户草稿被外部修改，提交版本/步骤与门户不一致 | 重新读取表单并对照字段后，`resolve_conflict_use_local` 或 `resolve_conflict_use_remote` |
| `awaiting_approval` | 到达主管审批步骤 | 主管核对绑定字段指纹后 `supervisor_approve` |
| `approval_stale` | 批准后机构/项目/金额变化，旧指纹与当前内容不符 | `reapprove_after_change` 后重新批准 |
| `canceled_externally` | 申请被外部取消 | 终态，停止一切写入，重启不复活 |
| `invalid_input` | 机构/项目不在表单合法选项或附件缺失 | 修正输入后重新登记该行 |

所有暂停详情都包含门户实际返回的表单（步骤、CSRF、draft_version、合法选项、附件要求），
原因的中文解释见 `collector/progress.py` 的 `PAUSE_EXPLANATIONS`。

## 旧版采集

常规导入失败时返回非零退出码，事务中的原文和记录会一起回滚，可保留源文件再次处理。
查询模式只读取当前数据库，不会补采或删除来源数据。需要长期保留证据时，请备份整个数据库。
本地响应服务支持状态码、响应头、文本或 base64 字节正文；未配置路径返回未找到。
每条路径的脚本独立推进，最后一个响应会重复；可借请求记录核对调用顺序。
数据文件的路径均相对项目根目录，修改工作目录前请确认输入和数据库参数。
