"""端到端情景演示：两条申请交错录入、外部修改、取消、重启与批准后改金额。

所有交互都打向真实的本地回环门户；每一步打印门户实际返回的表单内容，
所有暂停都带复核人员可读的原因和留痕。
"""
import json
import tempfile
from pathlib import Path

from .portal import Portal, PortalSession, create_server
from .progress import PAUSE_EXPLANATIONS, ProgressStore
from .workflow import Engine, read_requests


def line(title=''):
    print('\n' + '=' * 78)
    if title:
        print(title)
        print('-' * 78)


def show_form(title, form):
    print(f'[{title}] 步骤={form.step} action={form.action} '
          f'csrf={form.fields.get("csrf")} version=v{form.fields.get("draft_version")}')
    if form.options:
        for name, options in form.options.items():
            print('  选项 %s: %s' % (name, ', '.join(
                f'{o["value"]}（{o["label"]}）' for o in options)))
    if form.attachments:
        print('  附件要求: ' + '; '.join(
            f'{a["key"]}={a["label"]}'
            f'{"[必交]" if a["required"] else "[可选]"}' for a in form.attachments))


def show_status(store, portal):
    line('当前状态总览')
    for row in store.all_rows():
        snap = portal.snapshot(row['request_id']) or {}
        print(f'  {row["request_id"]} (rev{row["revision"]}) 本地状态={row["status"]}'
              f' 门户步骤={snap.get("step", "-")} 门户版本=v{snap.get("draft_version", "-")}'
              f' 门户内容={snap.get("content", {})}')
        if row['pause_reason']:
            print('    暂停原因: %s —— %s' % (
                row['pause_reason'],
                PAUSE_EXPLANATIONS.get(row['pause_reason'], '')))
    line('审计轨迹')
    for row in store.all_rows():
        print(f'  {row["request_id"]}:')
        for event in store.audit_trail(row['request_id']):
            detail = json.dumps(event['detail'], ensure_ascii=False, default=str)
            print(f'    - {event["event"]} {detail}')


def run(config_path, csv_path):
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    rows = read_requests(csv_path)

    portal = Portal(config)
    server, thread, base = create_server(portal)
    temporary = tempfile.TemporaryDirectory()
    db_path = Path(temporary.name) / 'workflow.sqlite3'
    store = ProgressStore(db_path)
    session = PortalSession(base)
    engine = Engine(session, store, config)
    engine.load_rows(rows)

    assertions = []

    def check(name, condition):
        assertions.append((name, bool(condition)))
        print(f'  断言[{"通过" if condition else "失败"}] {name}')

    try:
        line('阶段 1：两条申请交错录入（上一行的项目选项/步骤令牌不得带到下一行）')
        engine.advance('REQ1')  # REQ1 确认机构 -> 拿到 REQ1 自己的项目表单
        engine.advance('REQ2')  # REQ2 确认机构 -> 拿到 REQ2 自己的项目表单
        form_r1 = engine._pending_forms['REQ1']
        form_r2 = engine._pending_forms['REQ2']
        show_form('REQ1 机构确认后门户返回的项目表单', form_r1)
        show_form('REQ2 机构确认后门户返回的项目表单', form_r2)
        check('两申请的 CSRF 令牌不同（不共享步骤令牌）',
              form_r1.fields['csrf'] != form_r2.fields['csrf'])
        check('REQ1 的合法项目来自 I1 表单',
              form_r1.select_values('project') == ['P1', 'P2'])
        check('REQ2 的合法项目来自 I2 表单（I1 的 P2 不得串入 REQ2）',
              form_r2.select_values('project') == ['P3'])
        check('附件要求随项目不同：P2 三件、P3 三件但条目不同',
              {a['key'] for a in form_r1.attachments if a['project'] == 'P2'} ==
              {'plan_pdf', 'ethics_doc', 'supplement'}
              and {a['key'] for a in form_r2.attachments if a['project'] == 'P3'} ==
              {'contract_scan', 'budget_xls'})

        engine.advance('REQ1')  # REQ1 确认项目+金额+附件 -> 审批表单
        show_form('REQ1 项目确认后到达的审批表单', engine._pending_forms['REQ1'])
        check('REQ1 必交附件按 P2 表单逐项确认',
              portal.snapshot('REQ1')['attachments'].get('plan_pdf')
              and portal.snapshot('REQ1')['attachments'].get('ethics_doc'))

        line('阶段 2：外部业务人员修改 REQ1 草稿金额（forms.json: external_edit v2）')
        external_amount = '1350'
        session.admin_external_edit('REQ1', external_amount)
        print(f'  外部动作：REQ1 金额被改为 {external_amount}，门户版本抬升并回退到项目步骤')
        result = engine.advance('REQ1')  # 沿用内存中的旧审批表单令牌 -> 409
        row = store.get_row('REQ1')
        check('REQ1 因版本冲突暂停，外部改动未被覆盖',
              result == 'paused' and row['pause_reason'] == 'version_conflict'
              and portal.snapshot('REQ1')['content']['amount'] == external_amount)
        print('  重新读取门户草稿后的逐字段对照（本地 vs 门户）:')
        for field, values in row['pause_detail']['differences'].items():
            print(f'    字段 {field}: 本地={values["local"]} 门户={values["remote"]}')
        # 人工裁定：本地输入为准，基于重新读取的最新表单继续，外部改动保留在审计中
        engine.resolve_conflict_use_local('REQ1')
        check('裁定后本地金额重新写入门户，过程留痕可查',
              portal.snapshot('REQ1')['content']['amount'] == '1200'
              and any(e['event'] == 'conflict_resolved_use_local'
                      for e in store.audit_trail('REQ1')))

        line('阶段 3：REQ2 继续推进后被外部取消（forms.json: cancel）')
        engine.advance('REQ2')  # REQ2 确认项目+附件 -> 审批表单
        session.admin_cancel('REQ2')
        result = engine.advance('REQ2')
        check('REQ2 取消后立即停止，状态为 canceled 且未提交',
              result == 'canceled'
              and store.get_row('REQ2')['status'] == 'canceled'
              and not portal.snapshot('REQ2')['submitted'])

        line('阶段 4：模拟程序重启，从已确认步骤续填')
        store.close()
        store = ProgressStore(db_path)
        engine = Engine(session, store, config)
        results = engine.resume_all(order=['REQ1', 'REQ2'])
        print(f'  重启续填结果: {results}')
        check('取消的 REQ2 重启后仍为 canceled，不会被重新创建',
              results['REQ2'] == 'canceled')
        check('REQ1 停在审批门控等待主管（程序不自行批准）',
              store.get_row('REQ1')['pause_reason'] == 'awaiting_approval')

        line('阶段 5：主管针对 REQ1 当前内容指纹批准，然后提交')
        approval_form = engine._current_form('REQ1')
        show_form('REQ1 审批表单（绑定字段与指纹）', approval_form)
        fingerprint = engine.supervisor_approve('REQ1')
        approval_record = portal.snapshot('REQ1')['approval']
        check('批准记录绑定内容指纹与绑定字段，而不是裸布尔值',
              approval_record['fingerprint'] == fingerprint
              and approval_record['bound_fields'] == ['institution', 'project', 'amount'])
        result = engine.advance('REQ1')  # 提交
        check('REQ1 第一次成功提交并取得门户收据',
              result == 'submitted' and portal.snapshot('REQ1')['submitted'])
        receipt = portal.snapshot('REQ1')['confirmations']['submit']['detail']['receipt']
        print(f'  门户收据: {receipt}')

        line('阶段 6：修订号 2 —— 批准之后业务人员改金额（forms.json: change_amount）')
        revised = [dict(request_id='REQ1', revision=2,
                        content=dict(institution='I1', project='P2', amount='1680')),
                   dict(request_id='REQ2', revision=2,
                        content=dict(institution='I2', project='P3', amount='2400'))]
        # 门户侧模拟外部把已提交申请重新打开并改金额，回退到项目步骤，旧批准保留
        state = portal.state('REQ1')
        state.submitted = False
        state.step_index = 2
        session.admin_external_edit('REQ1', '1680')
        # 先登记修订输入，再推进：门控对照的本地内容必须是新金额
        engine.load_rows(revised)
        check('已取消的 REQ2 修订行被忽略，不会因恢复又被创建',
              store.get_row('REQ2')['status'] == 'canceled'
              and any(e['event'] == 'row_ignored_canceled'
                      for e in store.audit_trail('REQ2')))
        check('REQ1 修订已登记，本地金额更新为 1680',
              store.get_row('REQ1')['local_content']['amount'] == '1680')
        engine.advance('REQ1')  # 重新确认项目/金额/附件
        result = engine.advance('REQ1')  # 进入审批门控 -> 识别旧批准失效
        row = store.get_row('REQ1')
        check('审批门控识别出旧批准指纹与新内容不符，approval_stale 暂停',
              result == 'paused'
              and row['pause_reason'] == 'approval_stale'
              and row['pause_detail']['approved_fingerprint'] !=
              row['pause_detail']['current_fingerprint'])
        print('  暂停详情: ' + json.dumps(row['pause_detail'],
                                          ensure_ascii=False, default=str))
        # 主管针对新内容重新批准（新指纹），再提交
        engine.reapprove_after_change('REQ1')
        new_fingerprint = engine.supervisor_approve('REQ1')
        result = engine.advance('REQ1')
        check('重新批准后提交成功，新指纹对应新金额，旧批准未被沿用',
              result == 'submitted'
              and new_fingerprint != fingerprint
              and portal.snapshot('REQ1')['content']['amount'] == '1680'
              and portal.snapshot('REQ1')['approval']['fingerprint'] == new_fingerprint)

        show_status(store, portal)
        line('表单真实性抽查：实际 POST 的 CSRF/版本均取自对应申请的实际表单')
        for call in portal.access_log:
            if call['method'] == 'POST' and call['path'].startswith('/portal/'):
                print(f'  POST {call["path"]} csrf={call["form"].get("csrf")} '
                      f'version={call["form"].get("draft_version")}')

        line('复核结论：每处暂停的原因')
        for row in store.all_rows():
            for event in store.audit_trail(row['request_id']):
                if event['event'].startswith('paused_') or event['event'] in (
                        'row_canceled', 'row_failed'):
                    reason = row['pause_reason'] if row['request_id'] else ''
                    print(f'  {row["request_id"]} {event["event"]}: '
                          f'{PAUSE_EXPLANATIONS.get(event["detail"].get("reason", reason), "") or json.dumps(event["detail"], ensure_ascii=False, default=str)}')
        failed = [name for name, ok in assertions if not ok]
        for name, ok in assertions:
            print(f'  [{"PASS" if ok else "FAIL"}] {name}')
        if failed:
            raise SystemExit(f'演示断言失败: {failed}')
        print('\n全部场景通过：每处暂停均可在审计轨迹中查到原因与实际表单内容。')
    finally:
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        temporary.cleanup()
