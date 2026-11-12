"""版本化分步骤录入流程的单元测试，全部打向真实回环门户。"""
import json
import tempfile
import unittest
from pathlib import Path

from collector.domain import Form, bound_fingerprint, diff_content
from collector.portal import (Portal, PortalError, PortalSession,
                              create_server)
from collector.progress import (PAUSE_APPROVAL_STALE,
                                PAUSE_AWAITING_APPROVAL,
                                PAUSE_VERSION_CONFLICT, ROW_CANCELED,
                                ROW_SUBMITTED, ProgressStore)
from collector.workflow import Engine, parse_form, read_requests

CONFIG = json.loads(Path('fixtures/forms.json').read_text())


def row(request_id, revision=1, amount='1200', project='P2', institution='I1'):
    return dict(request_id=request_id, revision=revision,
                content=dict(institution=institution, project=project,
                             amount=amount))


class PortalHarness(unittest.TestCase):
    def setUp(self):
        self.portal = Portal(CONFIG)
        server, thread, base = create_server(self.portal)
        self.server, self.thread, self.base = server, thread, base
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ProgressStore(Path(self.temporary.name) / 'wf.sqlite3')
        self.session = PortalSession(base)
        self.engine = Engine(self.session, self.store, CONFIG)

    def tearDown(self):
        self.store.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def engine_with_rows(self, *rows):
        self.engine.load_rows(list(rows))
        return self.engine


class FormParsingTests(unittest.TestCase):
    def test_parses_options_attachments_and_step(self):
        html = ('<form action="/p/x/project" data-step="project">'
                '<input name="csrf" value="t1">'
                '<input name="draft_version" value="3">'
                '<select name="project">'
                '<option value="P1" data-label="甲">甲</option>'
                '<option value="P2" data-label="乙">乙</option></select>'
                '<ul data-attachments="1">'
                '<li data-attachment="plan_pdf" data-required="true" '
                'data-project="P2" data-label="计划书"></li></ul></form>')
        form = parse_form(html)
        self.assertEqual(form.step, 'project')
        self.assertEqual(form.fields['csrf'], 't1')
        self.assertEqual(form.select_values('project'), ['P1', 'P2'])
        self.assertEqual(form.attachments[0]['key'], 'plan_pdf')
        self.assertTrue(form.attachments[0]['required'])

    def test_fingerprint_changes_with_bound_field_only(self):
        base = dict(institution='I1', project='P2', amount='1200')
        changed = dict(base, amount='1680')
        self.assertNotEqual(bound_fingerprint(base, ['institution', 'project', 'amount']),
                            bound_fingerprint(changed, ['institution', 'project', 'amount']))
        self.assertEqual(
            bound_fingerprint(base, ['institution', 'project']),
            bound_fingerprint(changed, ['institution', 'project']))

    def test_diff_reports_only_changed_fields(self):
        differences = diff_content(dict(a='1', b='2'), dict(a='1', b='9'), ['a', 'b'])
        self.assertEqual(differences, {'b': dict(local='2', remote='9')})


class TokenIsolationTests(PortalHarness):
    def test_tokens_are_per_request_and_single_use(self):
        self.engine_with_rows(row('REQ1'), row('REQ2', institution='I2',
                                               project='P3', amount='2400'))
        self.engine.advance('REQ1')
        self.engine.advance('REQ2')
        form1 = self.engine._pending_forms['REQ1']
        form2 = self.engine._pending_forms['REQ2']
        self.assertNotEqual(form1.fields['csrf'], form2.fields['csrf'])

        # 把 REQ2 的令牌挪到 REQ1 的提交中必须被拒
        stolen = dict(form1.fields)
        stolen['csrf'] = form2.fields['csrf']
        with self.assertRaises(PortalError) as caught:
            self.session.post('REQ1', 'project', stolen)
        self.assertEqual(caught.exception.code, 'bad_csrf')

        # 成功消费后同一令牌不能再用
        good = dict(form1.fields, project='P2', amount='1200',
                    att_plan_pdf='on', att_ethics_doc='on')
        self.session.post('REQ1', 'project', good)
        with self.assertRaises(PortalError) as caught:
            self.session.post('REQ1', 'project', good)
        self.assertEqual(caught.exception.code, 'bad_csrf')


class DependentOptionsTests(PortalHarness):
    def test_project_options_refresh_after_institution(self):
        self.engine_with_rows(row('REQ1'))
        self.engine.advance('REQ1')
        form = self.engine._pending_forms['REQ1']
        self.assertEqual(form.select_values('project'), ['P1', 'P2'])
        # 表单之外的项目 ID 不被接受
        illegal = dict(form.fields, project='P3', amount='1', att_plan_pdf='on')
        with self.assertRaises(PortalError) as caught:
            self.session.post('REQ1', 'project', illegal)
        self.assertEqual(caught.exception.code, 'bad_project')

    def test_row_failure_is_isolated(self):
        self.engine_with_rows(
            row('REQ1', amount='1200'),
            row('REQ2', institution='I2', project='P9', amount='2400'))
        self.engine.run_until_pause('REQ1')
        result = self.engine.run_until_pause('REQ2')
        self.assertEqual(result, 'failed')
        # REQ2 失败不影响 REQ1，REQ1 仍停在审批门控
        self.assertEqual(self.store.get_row('REQ1')['pause_reason'],
                         PAUSE_AWAITING_APPROVAL)


class VersionConflictTests(PortalHarness):
    def test_external_edit_is_never_silently_overwritten(self):
        self.engine_with_rows(row('REQ1'))
        self.engine.run_until_pause('REQ1')  # 到审批门控
        self.session.admin_external_edit('REQ1', '1350')
        result = self.engine.advance('REQ1')
        self.assertEqual(result, 'paused')
        paused = self.store.get_row('REQ1')
        self.assertEqual(paused['pause_reason'], PAUSE_VERSION_CONFLICT)
        self.assertEqual(paused['pause_detail']['differences']['amount'],
                         dict(local='1200', remote='1350'))
        # 门户草稿保持外部金额
        self.assertEqual(self.portal.snapshot('REQ1')['content']['amount'], '1350')

        # 人工裁定以本地为准：外部金额被改写，但完整留痕
        self.engine.resolve_conflict_use_local('REQ1')
        self.assertEqual(self.portal.snapshot('REQ1')['content']['amount'], '1200')
        events = [e['event'] for e in self.store.audit_trail('REQ1')]
        self.assertIn('paused_version_conflict', events)
        self.assertIn('conflict_resolved_use_local', events)

    def test_remote_resolution_adopts_external_content(self):
        self.engine_with_rows(row('REQ1'))
        self.engine.run_until_pause('REQ1')
        self.session.admin_external_edit('REQ1', '1350')
        self.engine.advance('REQ1')
        self.engine.resolve_conflict_use_remote('REQ1')
        self.assertEqual(self.store.get_row('REQ1')['local_content']['amount'],
                         '1350')


class ApprovalTests(PortalHarness):
    def _approved_request(self):
        self.engine_with_rows(row('REQ1'))
        self.engine.run_until_pause('REQ1')
        first = self.engine.supervisor_approve('REQ1')
        result = self.engine.advance('REQ1')
        self.assertEqual(result, ROW_SUBMITTED)
        return first

    def test_approval_is_content_bound_not_boolean(self):
        fingerprint = self._approved_request()
        approval = self.portal.snapshot('REQ1')['approval']
        self.assertEqual(approval['fingerprint'], fingerprint)
        self.assertEqual(approval['bound_fields'],
                         ['institution', 'project', 'amount'])

    def test_amount_change_after_approval_invalidates_it(self):
        old_fingerprint = self._approved_request()
        state = self.portal.state('REQ1')
        state.submitted = False
        state.step_index = 2
        self.session.admin_external_edit('REQ1', '1680')
        self.engine.load_rows([row('REQ1', revision=2, amount='1680')])
        self.engine.advance('REQ1')  # 重确认项目
        result = self.engine.advance('REQ1')  # 门控识别失效
        paused = self.store.get_row('REQ1')
        self.assertEqual(result, 'paused')
        self.assertEqual(paused['pause_reason'], PAUSE_APPROVAL_STALE)
        self.assertNotEqual(paused['pause_detail']['approved_fingerprint'],
                            paused['pause_detail']['current_fingerprint'])
        # 门户提交端同样拒绝旧批准
        self.engine.reapprove_after_change('REQ1')
        new_fingerprint = self.engine.supervisor_approve('REQ1')
        self.assertNotEqual(old_fingerprint, new_fingerprint)
        self.assertEqual(self.engine.advance('REQ1'), ROW_SUBMITTED)

    def test_portal_rejects_submit_with_stale_approval(self):
        self.engine_with_rows(row('REQ1'))
        self.engine.run_until_pause('REQ1')
        self.engine.supervisor_approve('REQ1')
        # 直接在门户篡改金额（回退到项目步骤，旧批准保留）
        self.portal.external_edit('REQ1', '9999')
        project_form = parse_form(self.session.get_form('REQ1'))
        self.session.post('REQ1', 'project',
                          dict(project_form.fields, project='P2', amount='9999',
                               att_plan_pdf='on', att_ethics_doc='on'))
        # 重新拿到审批表单但不重新批准；把门户推进到提交步骤且保留旧批准
        approval_form = parse_form(self.session.get_form('REQ1', 'approval'))
        # 不调用批准，直接让门户停在审批；手动前进一步以测试提交端指纹校验
        state = self.portal.state('REQ1')
        state.step_index = 3
        submit_form = parse_form(self.session.get_form('REQ1', 'submit'))
        with self.assertRaises(PortalError) as caught:
            self.session.post('REQ1', 'submit',
                              dict(submit_form.fields, confirm='1'))
        self.assertEqual(caught.exception.code, 'approval_stale')


class CancelAndRestartTests(PortalHarness):
    def test_canceled_row_is_not_recreated_on_restart(self):
        self.engine_with_rows(row('REQ1'),
                              row('REQ2', institution='I2', project='P3',
                                  amount='2400'))
        self.engine.run_until_pause('REQ1')
        self.engine.advance('REQ2')
        self.session.admin_cancel('REQ2')
        self.assertEqual(self.engine.advance('REQ2'), ROW_CANCELED)

        db_path = Path(self.temporary.name) / 'wf.sqlite3'
        self.store.close()
        store = ProgressStore(db_path)
        engine = Engine(self.session, store, CONFIG)
        # 输入文件再次出现含 REQ2 的修订行
        engine.load_rows([row('REQ2', revision=2, institution='I2',
                              project='P3', amount='2400')])
        results = engine.resume_all(order=['REQ1', 'REQ2'])
        self.assertEqual(results['REQ2'], ROW_CANCELED)
        self.assertFalse(self.portal.snapshot('REQ2')['submitted'])
        store.close()


class CsvInputTests(unittest.TestCase):
    def test_reads_stable_id_and_revision(self):
        rows = read_requests('fixtures/requests.csv')
        self.assertEqual([(r['request_id'], r['revision']) for r in rows],
                         [('REQ1', 1), ('REQ2', 1)])
        self.assertEqual(rows[0]['content']['amount'], '1200')


if __name__ == '__main__':
    unittest.main()
