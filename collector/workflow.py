"""带版本约束的分步骤录入引擎。

关键约束：
* 每一步的 CSRF 与 draft_version 都来自门户实际返回的表单，令牌不跨行、不跨步骤共享；
* 项目选项必须来自机构选择后重新拉取的表单，禁止按显示文字猜 ID；
* 门户返回 409 时重新读取草稿并与本地输入逐字段对照，外部改动不会被悄悄覆盖；
* 批准绑定内容指纹，影响审批的字段变化后旧批准失效，必须重新批准；
* 每条申请独立推进、独立持久化；校验失败只暂停本行，其他行继续办理；
* 重启后从已确认步骤之后继续；取消的行不会被重新创建。
"""
import csv
import json

from .domain import Form, bound_fingerprint, diff_content
from .portal import (PortalError, STEP_APPROVAL, STEP_INSTITUTION,
                     STEP_ORDER, STEP_PROJECT, STEP_SUBMIT)
from .progress import (PAUSE_APPROVAL_STALE, PAUSE_AWAITING_APPROVAL,
                       PAUSE_VERSION_CONFLICT, ROW_CANCELED, ROW_FAILED,
                       ROW_PAUSED, ROW_RUNNING, ROW_SUBMITTED)


def read_requests(csv_path):
    """读取输入行：稳定申请号 + 修订号。"""
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as handle:
        for line in csv.DictReader(handle):
            rows.append(dict(
                request_id=line['id'].strip(),
                revision=int(line['revision']),
                content=dict(institution=line['institution'].strip(),
                             project=line['project'].strip(),
                             amount=line['amount'].strip())))
    return rows


def parse_form(html):
    form = Form()
    form.feed(html)
    return form


class Engine:
    def __init__(self, session, store, config):
        self.session = session
        self.store = store
        self.config = config
        self.bound_fields = next(
            step.get('bound_fields', []) for step in config['steps']
            if step['name'] == STEP_APPROVAL)
        # 每条申请各自持有“上一张实际表单”，令牌绝不共享给其他申请
        self._pending_forms = {}

    # ---------- 装载输入 ----------
    def load_rows(self, rows):
        for row in rows:
            # 取消的行即使输入文件再次出现也不复活
            existing = self.store.get_row(row['request_id'])
            if existing and existing['status'] == ROW_CANCELED:
                self.store.audit(row['request_id'], row['revision'],
                                 'row_ignored_canceled',
                                 dict(status=ROW_CANCELED))
                continue
            # 已提交行只有在门户侧被重新打开时才接受新修订
            if existing and existing['status'] == ROW_SUBMITTED:
                snapshot = self._snapshot(row['request_id'])
                if snapshot is None or snapshot.get('submitted'):
                    self.store.audit(row['request_id'], row['revision'],
                                     'row_ignored_submitted',
                                     dict(status=ROW_SUBMITTED))
                    continue
                # 门户已重新打开：本地行回到运行态，登记修订内容
                with self.store.transaction():
                    self.store.connection.execute(
                        'UPDATE requests SET status=? WHERE request_id=?',
                        (ROW_RUNNING, row['request_id']))
                    self.store._audit(row['request_id'], row['revision'],
                                      'row_reopened_by_portal', snapshot)
            self.store.register(row['request_id'], row['revision'], row['content'])

    # ---------- 表单获取 ----------
    def _current_form(self, request_id, step=None):
        html = self.session.get_form(request_id, step)
        form = parse_form(html)
        self._pending_forms[request_id] = form
        self.store.save_form(request_id, self._form_dump(form, html))
        return form

    @staticmethod
    def _form_dump(form, html):
        return dict(step=form.step, action=form.action,
                    csrf=form.fields.get('csrf'),
                    draft_version=form.fields.get('draft_version'),
                    options=form.options, attachments=form.attachments,
                    html=html)

    # ---------- 单步推进 ----------
    def advance(self, request_id):
        row = self.store.get_row(request_id)
        if row is None:
            raise KeyError(request_id)
        if row['status'] in (ROW_CANCELED, ROW_SUBMITTED):
            return row['status']

        form = self._pending_forms.get(request_id)
        if form is None:
            form = self._current_form(request_id)

        # 门户侧状态（取消/提交/步骤漂移）以快照为准，优先于本地挂起表单
        snapshot = self._snapshot(request_id)
        if snapshot and snapshot.get('canceled'):
            detail = dict(portal_state='canceled',
                          draft_version=snapshot.get('draft_version'))
            self.store.mark_canceled(request_id, row['revision'], detail)
            self._pending_forms.pop(request_id, None)
            return ROW_CANCELED
        if snapshot and snapshot.get('submitted'):
            return ROW_SUBMITTED

        # 挂起表单可能已被外部修改（门户回退到更早步骤）：
        # 用步骤序号比较，门户展示态（done/canceled）不参与漂移判断
        server_step = snapshot.get('step') if snapshot else None
        if server_step in STEP_ORDER and server_step != form.step:
            try:
                self.session.post(
                    request_id, form.step, self._payload_for(form, row))
            except PortalError as error:
                return self._handle_portal_error(row, form, error)

        step = form.step
        try:
            if step == STEP_INSTITUTION:
                return self._do_institution(row, form)
            if step == STEP_PROJECT:
                return self._do_project(row, form)
            if step == STEP_APPROVAL:
                return self._do_approval_gate(row, form)
            if step == STEP_SUBMIT:
                return self._do_submit(row, form)
        except PortalError as error:
            return self._handle_portal_error(row, form, error)

    # ---------- 各步骤 ----------
    def _payload_for(self, form, row):
        if form.step == STEP_INSTITUTION:
            return dict(form.fields,
                        institution=row['local_content']['institution'])
        if form.step == STEP_PROJECT:
            payload = dict(form.fields, project=row['local_content']['project'],
                           amount=str(row['local_content']['amount']))
            required = {item['key'] for item in form.attachments
                        if item['required']
                        and item['project'] == row['local_content']['project']}
            for key in required:
                payload[f'att_{key}'] = 'on'
            return payload
        if form.step == STEP_APPROVAL:
            return dict(form.fields, decision='approved')
        return dict(form.fields, confirm='1')

    def _do_institution(self, row, form):
        institution = row['local_content']['institution']
        legal = form.select_values('institution')
        if institution not in legal:
            return self._invalid(row, form,
                                 f'机构 {institution} 不在表单合法选项 {legal} 内')
        payload = dict(form.fields, institution=institution)
        return self._post_and_continue(row, STEP_INSTITUTION, payload)

    def _do_project(self, row, form):
        project = row['local_content']['project']
        legal = form.select_values('project')
        # 选项 ID 只认表单返回的 value，绝不按显示文字猜测
        if project not in legal:
            return self._invalid(
                row, form,
                f'项目 {project} 不属于当前机构的合法选项 {legal}（选项必须来自表单）')
        # 附件要求随项目变化：只取表单中属于所选项目的条目并逐项确认必交件
        required = {item['key'] for item in form.attachments
                    if item['required'] and item['project'] == project}
        payload = dict(form.fields, project=project,
                       amount=str(row['local_content']['amount']))
        for key in required:
            payload[f'att_{key}'] = 'on'
        return self._post_and_continue(
            row, STEP_PROJECT, payload,
            extra=dict(legal_projects=legal,
                       required_attachments=sorted(required)))

    def _do_approval_gate(self, row, form):
        snapshot = self._snapshot(row['request_id'])
        approval = snapshot.get('approval') if snapshot else None
        current_fingerprint = bound_fingerprint(row['local_content'],
                                                self.bound_fields)
        if approval is not None and approval.get('fingerprint') != current_fingerprint:
            # 已批准内容与当前内容不一致：批准是针对具体内容的，不能永久沿用
            detail = dict(
                draft_version=form.fields.get('draft_version'),
                bound_fields=self.bound_fields,
                approved_fingerprint=approval.get('fingerprint'),
                current_fingerprint=current_fingerprint,
                approved_content=approval.get('bound_content'),
                local_content=row['local_content'],
                explanation='影响审批的字段在批准后发生变化，原批准已失效，需重新批准')
            self.store.set_status(row['request_id'], ROW_PAUSED,
                                  PAUSE_APPROVAL_STALE, detail)
            self.store.audit(row['request_id'], row['revision'],
                             'paused_approval_stale', detail)
            return ROW_PAUSED
        # 批准是人工职责：程序停在审批步骤，等待主管对当前内容指纹批准
        detail = dict(
            draft_version=form.fields.get('draft_version'),
            bound_fields=self.bound_fields,
            current_fingerprint=current_fingerprint)
        self.store.set_status(row['request_id'], ROW_PAUSED,
                              PAUSE_AWAITING_APPROVAL, detail)
        self.store.audit(row['request_id'], row['revision'],
                         'paused_awaiting_approval', detail)
        return ROW_PAUSED

    def _do_submit(self, row, form):
        # 提交前由门户校验批准指纹；引擎同时在本地做一次显式核对
        payload = dict(form.fields, confirm='1')
        try:
            html = self.session.post(row['request_id'], STEP_SUBMIT, payload)
        except PortalError as error:
            if error.code == 'approval_stale':
                detail = dict(error=error.extra,
                              local_content=row['local_content'],
                              explanation='批准后金额等字段已修改，旧批准不得沿用')
                self.store.set_status(row['request_id'], ROW_PAUSED,
                                      PAUSE_APPROVAL_STALE, detail)
                self.store.audit(row['request_id'], row['revision'],
                                 'paused_approval_stale', detail)
                return ROW_PAUSED
            raise
        receipt = self._receipt_after_submit(row['request_id'])
        self.store.confirm_step(row['request_id'], row['revision'],
                                STEP_SUBMIT, form.fields.get('draft_version'),
                                self._form_dump(form, html),
                                dict(payload_csrf=payload['csrf']))
        self.store.mark_submitted(row['request_id'], row['revision'],
                                  receipt, dict(local=row['local_content']))
        self._pending_forms.pop(row['request_id'], None)
        return ROW_SUBMITTED

    # ---------- 主管批准（人工动作的程序化入口） ----------
    def supervisor_approve(self, request_id):
        row = self.store.get_row(request_id)
        form = self._current_form(request_id)
        if form.step != STEP_APPROVAL:
            raise PortalError(409, 'not_at_approval',
                              f'当前步骤为 {form.step}，不能批准')
        payload = dict(form.fields, decision='approved')
        html = self.session.post(request_id, STEP_APPROVAL, payload)
        next_form = parse_form(html)
        self._pending_forms[request_id] = next_form
        fingerprint = bound_fingerprint(row['local_content'], self.bound_fields)
        self.store.confirm_step(
            request_id, row['revision'], STEP_APPROVAL,
            next_form.fields.get('draft_version'),
            self._form_dump(next_form, html),
            dict(decision='approved', fingerprint=fingerprint,
                 bound_content={f: row['local_content'].get(f)
                                for f in self.bound_fields}))
        self.store.save_form(request_id, self._form_dump(next_form, html))
        return fingerprint

    # ---------- 冲突处理 ----------
    def _post_and_continue(self, row, step, payload, extra=None):
        """单次门户交互：提交一张实际表单，确认后停在下一步，不自动连跑。"""
        try:
            html = self.session.post(row['request_id'], step, payload)
        except PortalError as error:
            return self._handle_portal_error(
                row, self._pending_forms[row['request_id']], error)
        form = parse_form(html)
        self._pending_forms[row['request_id']] = form
        snapshot = self._form_dump(form, html)
        self.store.confirm_step(
            row['request_id'], row['revision'], step,
            form.fields.get('draft_version'), snapshot,
            dict(submitted_csrf=payload['csrf'],
                 submitted_version=payload['draft_version'],
                 **(extra or {})))
        self.store.save_form(row['request_id'], snapshot)
        return ROW_RUNNING

    def _handle_portal_error(self, row, form, error):
        if error.status == 410 and error.code == 'canceled':
            self.store.mark_canceled(row['request_id'], row['revision'],
                                     dict(message=error.message))
            self._pending_forms.pop(row['request_id'], None)
            return ROW_CANCELED
        if error.status == 409 and error.code in ('version_conflict', 'step_drift'):
            # 重新读取门户草稿，对照本地输入，绝不直接覆盖
            fresh_html = self.session.get_form(row['request_id'])
            fresh = parse_form(fresh_html)
            self._pending_forms[row['request_id']] = fresh
            remote_content = self._remote_content(row['request_id'])
            differences = diff_content(row['local_content'], remote_content,
                                       self.bound_fields)
            detail = dict(
                conflict_code=error.code,
                submitted_version=error.extra.get('submitted_version'),
                server_version=error.extra.get('server_version'),
                current_step=error.extra.get('current_step'),
                remote_content=remote_content,
                local_content=row['local_content'],
                differences=differences,
                fresh_form=self._form_dump(fresh, fresh_html))
            self.store.set_status(row['request_id'], ROW_PAUSED,
                                  PAUSE_VERSION_CONFLICT, detail)
            self.store.audit(row['request_id'], row['revision'],
                             'paused_version_conflict', detail)
            return ROW_PAUSED
        if error.status == 422:
            return self._invalid(row, form, f'{error.code}: {error.message}',
                                 extra=error.extra)
        if error.status == 403 and error.code == 'bad_csrf':
            # 令牌已消费或串用：重新读取实际表单后按门户当前步骤重试一次
            fresh = parse_form(self.session.get_form(row['request_id']))
            self._pending_forms[row['request_id']] = fresh
            self.store.audit(row['request_id'], row['revision'],
                             'csrf_refreshed',
                             dict(message=error.message, step=fresh.step))
            return self.advance(row['request_id'])
        if error.status == 403:
            # 其他令牌问题属于程序缺陷，直接抛出而不是静默重试
            raise
        raise error

    def resolve_conflict_use_local(self, request_id):
        """人工裁定：以本地输入为准，基于最新表单重新推进（外部改动留痕不覆盖）。"""
        row = self.store.get_row(request_id)
        if not row or row['pause_reason'] != PAUSE_VERSION_CONFLICT:
            raise RuntimeError('该行当前不在版本冲突暂停状态')
        self.store.audit(request_id, row['revision'],
                         'conflict_resolved_use_local',
                         dict(remote_kept_in_audit=True,
                              local=row['local_content']))
        self.store.set_status(request_id, ROW_RUNNING, None, None)
        # 以最新门户表单为准重新出发，内容仍取本地输入
        self._pending_forms.pop(request_id, None)
        return self.run_until_pause(request_id)

    def resolve_conflict_use_remote(self, request_id):
        """人工裁定：接受门户草稿，外部改动写入本地输入快照后继续。"""
        row = self.store.get_row(request_id)
        if not row or row['pause_reason'] != PAUSE_VERSION_CONFLICT:
            raise RuntimeError('该行当前不在版本冲突暂停状态')
        remote = self._remote_content(request_id)
        with self.store.transaction():
            self.store.connection.execute(
                'UPDATE requests SET local_content=? WHERE request_id=?',
                (json.dumps(remote, ensure_ascii=False), request_id))
            self.store._audit(request_id, row['revision'],
                              'conflict_resolved_use_remote',
                              dict(remote=remote))
        self.store.set_status(request_id, ROW_RUNNING, None, None)
        self._pending_forms.pop(request_id, None)
        return self.run_until_pause(request_id)

    def reapprove_after_change(self, request_id):
        """批准后字段变化导致失效：确认重新走审批，主管随后针对新内容批准。"""
        row = self.store.get_row(request_id)
        if not row or row['pause_reason'] != PAUSE_APPROVAL_STALE:
            raise RuntimeError('该行当前不在批准失效暂停状态')
        self.store.audit(request_id, row['revision'],
                         'reapproval_requested', dict(local=row['local_content']))
        self.store.set_status(request_id, ROW_PAUSED, None, None)
        self._pending_forms.pop(request_id, None)
        return self.advance(request_id)

    def run_until_pause(self, request_id, max_steps=12):
        """从当前位置持续推进，直到暂停/取消/失败/提交。"""
        terminal = {ROW_PAUSED, ROW_CANCELED, ROW_SUBMITTED, ROW_FAILED}
        result = None
        for unused in range(max_steps):
            result = self.advance(request_id)
            if result in terminal:
                return result
        raise RuntimeError(f'{request_id} 推进超过 {max_steps} 步仍未终止')

    # ---------- 辅助 ----------
    def _snapshot(self, request_id):
        status, headers, raw = self.session.client.request(
            self.session.base + f'/state/{request_id}')
        if status != 200:
            return None
        return json.loads(raw.decode('utf-8'))

    def _remote_content(self, request_id):
        snapshot = self._snapshot(request_id)
        return dict(snapshot.get('content', {})) if snapshot else {}

    def _receipt_after_submit(self, request_id):
        status, headers, raw = self.session.client.request(
            self.session.base + f'/state/{request_id}')
        data = json.loads(raw.decode('utf-8'))
        return data.get('confirmations', {}).get('submit', {}).get(
            'detail', {}).get('receipt')

    def _invalid(self, row, form, message, extra=None):
        detail = dict(message=message,
                      form_step=form.step,
                      form_version=form.fields.get('draft_version'),
                      local=row['local_content'], **(extra or {}))
        self.store.mark_failed(row['request_id'], row['revision'], detail)
        self._pending_forms.pop(row['request_id'], None)
        return ROW_FAILED

    def resume_all(self, order=None):
        """重启后续填：跳过取消/提交/失败行，只推进仍可办理的行。"""
        results = {}
        rows = self.store.all_rows()
        if order:
            rows.sort(key=lambda item: order.index(item['request_id'])
                      if item['request_id'] in order else len(order))
        for row in rows:
            if row['status'] in (ROW_CANCELED, ROW_SUBMITTED):
                results[row['request_id']] = row['status']
                continue
            if row['status'] == ROW_FAILED:
                results[row['request_id']] = ROW_FAILED
                continue
            self._pending_forms.pop(row['request_id'], None)
            results[row['request_id']] = self.run_until_pause(row['request_id'])
        return results
