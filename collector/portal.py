"""分步骤申报门户的本地回环实现。

门户按申请号维护各自的草稿：每一步表单携带自己的 CSRF 与 draft_version，
项目选项依赖机构选择，附件要求依赖项目选择；批准绑定具体内容指纹，
外部修改会抬高草稿版本，取消的申请返回 410。所有数据均为合成数据。
"""
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import bound_fingerprint
from .transport import Client

STEP_INSTITUTION = 'institution'
STEP_PROJECT = 'project'
STEP_APPROVAL = 'approval'
STEP_SUBMIT = 'submit'
STEP_ORDER = (STEP_INSTITUTION, STEP_PROJECT, STEP_APPROVAL, STEP_SUBMIT)

# 项目切换后附件要求随之变化（项目 -> 附件清单）
ATTACHMENT_RULES = {
    'P1': [dict(key='plan_pdf', required=True, label='项目计划书'),
           dict(key='budget_xls', required=True, label='预算明细表')],
    'P2': [dict(key='plan_pdf', required=True, label='项目计划书'),
           dict(key='ethics_doc', required=True, label='伦理审查函'),
           dict(key='supplement', required=False, label='补充材料（可选）')],
    'P3': [dict(key='contract_scan', required=True, label='合同扫描件'),
           dict(key='budget_xls', required=True, label='预算明细表')],
}

INSTITUTION_LABELS = {'I1': '第一研究所', 'I2': '第二研究所'}
PROJECT_LABELS = {'P1': '基础研究项目', 'P2': '应用示范项目', 'P3': '委托开发项目'}


class PortalError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


class PortalState:
    """单条申请在门户侧的全部状态。"""

    def __init__(self, request_id, revision, steps):
        self.request_id = request_id
        self.revision = revision
        self.steps = steps
        self.step_index = 0
        self.draft_version = 0
        self.content = {}
        self.attachments = {}
        self.canceled = False
        self.submitted = False
        self.confirmations = {}
        # 每一步各自的令牌；不同申请之间互不共享
        self.tokens = {}
        self.approval = None  # 含指纹的批准记录，而不是裸布尔值

    @property
    def step(self):
        if self.submitted:
            return 'done'
        if self.canceled:
            return 'canceled'
        return STEP_ORDER[self.step_index]

    def token_for(self, step):
        token = self.tokens.get(step)
        if token is None:
            token = secrets.token_hex(12)
            self.tokens[step] = token
        return token

    def check_token(self, step, token):
        # 仅验真不消费：跨申请/跨步骤/已消费令牌在此被拒
        if not token or self.tokens.get(step) != token:
            raise PortalError(403, 'bad_csrf',
                              '步骤令牌缺失、已消费或属于其他申请/步骤')

    def consume_token(self, step):
        # 业务校验全部通过后才消费并换发，保证 409 重试仍可用原令牌
        self.tokens[step] = secrets.token_hex(12)

    def bound_fields(self):
        for step in self.steps:
            if step['name'] == STEP_APPROVAL:
                return step.get('bound_fields', [])
        return []


class Portal:
    def __init__(self, config):
        self.config = config
        self.steps = config['steps']
        self.options = next(
            step['options'] for step in self.steps
            if step['name'] == STEP_PROJECT)
        self.bound_fields = next(
            step.get('bound_fields', []) for step in self.steps
            if step['name'] == STEP_APPROVAL)
        self.states = {}
        self.lock = threading.RLock()
        self.access_log = []

    # ---------- 状态查询（供演示与测试核对） ----------
    def state(self, request_id):
        with self.lock:
            return self.states.get(request_id)

    def snapshot(self, request_id):
        state = self.state(request_id)
        if state is None:
            return None
        with self.lock:
            return dict(
                request_id=state.request_id, revision=state.revision,
                step=state.step, draft_version=state.draft_version,
                content=dict(state.content), attachments=dict(state.attachments),
                canceled=state.canceled, submitted=state.submitted,
                confirmations=dict(state.confirmations),
                approval=(None if state.approval is None
                          else dict(state.approval)))

    # ---------- 内部 ----------
    def _ensure(self, request_id, revision):
        state = self.states.get(request_id)
        if state is None:
            state = PortalState(request_id, revision, self.steps)
            self.states[request_id] = state
        return state

    def _check_open(self, state):
        if state.canceled:
            raise PortalError(410, 'canceled', '该申请已被取消，不能继续办理')
        if state.submitted:
            raise PortalError(410, 'submitted', '该申请已提交，门户关闭编辑')

    def _validate_token(self, state, step, token):
        state.check_token(step, token)

    def _validate_version(self, state, version):
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise PortalError(422, 'bad_version', 'draft_version 必须为整数')
        if version != state.draft_version:
            raise PortalError(
                409, 'version_conflict',
                f'版本冲突：门户草稿为 v{state.draft_version}，提交基于 v{version}',
                dict(server_version=state.draft_version,
                     submitted_version=version))

    def _advance(self, state):
        if state.step_index < len(STEP_ORDER) - 1:
            state.step_index += 1

    # ---------- GET ----------
    def handle_get(self, request_id, step_hint=None):
        with self.lock:
            state = self._ensure(request_id, None)
            if state.canceled:
                return self._render_canceled(state)
            if state.submitted:
                return self._render_done(state)
            step = step_hint or state.step
            if step not in STEP_ORDER:
                raise PortalError(404, 'unknown_step', '未知步骤')
            return self._render_form(state, step)

    # ---------- POST ----------
    def handle_post(self, request_id, step, form):
        with self.lock:
            state = self._ensure(request_id, None)
            self._check_open(state)
            if step not in STEP_ORDER:
                raise PortalError(404, 'unknown_step', '未知步骤')
            # 先认证步骤令牌：跨申请/跨步骤/已消费的令牌一律拒绝
            self._validate_token(state, step, form.get('csrf'))
            # 外部修改可能已把门户回退到更早步骤：表单步骤漂移按冲突语义拒绝
            if step != state.step:
                raise PortalError(
                    409, 'step_drift',
                    f'表单步骤 {step} 已过期，门户当前步骤为 {state.step}，请重新读取表单',
                    dict(server_version=state.draft_version,
                         submitted_version=form.get('draft_version'),
                         current_step=state.step, submitted_step=step))
            self._validate_version(state, form.get('draft_version'))

            handler = getattr(self, f'_post_{step}')
            detail = handler(state, form)
            state.consume_token(step)
            state.draft_version += 1
            state.confirmations[step] = dict(
                version=state.draft_version, detail=detail)
            self._advance(state)
            return self._render_form(state, state.step)

    def _post_institution(self, state, form):
        institution = form.get('institution', '')
        if institution not in self.options:
            raise PortalError(422, 'bad_institution', '机构不在合法选项内')
        changed = state.content.get('institution') not in (None, institution)
        state.content['institution'] = institution
        # 机构变更后旧项目不再合法，回退并要求重新选择
        if changed and state.content.get('project') not in self.options.get(institution, []):
            state.content.pop('project', None)
            state.attachments = {}
            state.step_index = STEP_ORDER.index(STEP_PROJECT)
        return dict(institution=institution)

    def _post_project(self, state, form):
        institution = state.content.get('institution')
        project = form.get('project', '')
        if project not in self.options.get(institution, []):
            raise PortalError(422, 'bad_project',
                              '项目不属于所选机构，选项必须来自表单返回的合法列表')
        amount = form.get('amount', '')
        try:
            amount_value = int(amount)
            if amount_value <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise PortalError(422, 'bad_amount', '金额必须为正整数')
        project_changed = state.content.get('project') != project
        state.content['project'] = project
        state.content['amount'] = str(amount_value)
        if project_changed:
            # 附件要求随项目变化，已确认附件全部作废，需按新表单重新确认
            state.attachments = {}
        required = {item['key'] for item in ATTACHMENT_RULES.get(project, [])
                    if item['required']}
        supplied = {key for key, value in form.items()
                    if key.startswith('att_') and value == 'on'}
        all_keys = {item['key'] for item in ATTACHMENT_RULES.get(project, [])}
        state.attachments = {key: (f'att_{key}' in supplied) for key in all_keys}
        missing = sorted(required - {key[4:] for key in supplied})
        if missing:
            raise PortalError(422, 'missing_attachment',
                              f'缺少必交附件：{", ".join(missing)}',
                              dict(missing=missing))
        return dict(project=project, amount=state.content['amount'],
                    attachments=sorted(supplied))

    def _post_approval(self, state, form):
        if form.get('decision') != 'approved':
            raise PortalError(422, 'not_approved', '主管未批准，流程不能继续')
        fingerprint = bound_fingerprint(state.content, self.bound_fields)
        state.approval = dict(
            fingerprint=fingerprint,
            bound_fields=list(self.bound_fields),
            bound_content={field: state.content.get(field)
                           for field in self.bound_fields},
            version=state.draft_version + 1)
        return dict(fingerprint=fingerprint)

    def _post_submit(self, state, form):
        approval = state.approval
        if approval is None:
            raise PortalError(403, 'no_approval', '缺少主管批准，不能提交')
        current = bound_fingerprint(state.content, self.bound_fields)
        if approval['fingerprint'] != current:
            raise PortalError(
                409, 'approval_stale',
                '批准针对的内容已变化，原批准失效，需要重新批准',
                dict(approved_fingerprint=approval['fingerprint'],
                     current_fingerprint=current,
                     bound_fields=list(self.bound_fields)))
        state.submitted = True
        state.step_index = len(STEP_ORDER) - 1
        return dict(receipt=f'RCV-{state.request_id}-{state.draft_version + 1}')

    # ---------- 管理动作（模拟外部业务人员/主管） ----------
    def external_edit(self, request_id, amount):
        """业务人员在主管审批之外直接改金额，抬高草稿版本。"""
        with self.lock:
            state = self._ensure(request_id, None)
            self._check_open(state)
            state.content['amount'] = str(int(amount))
            state.draft_version += 1
            # 回退到项目步骤：影响审批的字段已变，旧批准不再可信
            state.step_index = min(
                state.step_index, STEP_ORDER.index(STEP_PROJECT))
            state.confirmations['external_edit'] = dict(
                version=state.draft_version, amount=str(int(amount)))
            return state.draft_version

    def cancel(self, request_id):
        with self.lock:
            state = self._ensure(request_id, None)
            if state.submitted:
                raise PortalError(409, 'already_submitted', '已提交的申请不能取消')
            state.canceled = True
            state.confirmations['cancel'] = dict(version=state.draft_version)
            return True

    # ---------- 渲染 ----------
    def _render_form(self, state, step):
        token = state.token_for(step)
        action = f'/portal/{state.request_id}/{step}'
        head = (f'<form action="{action}" method="post" data-step="{step}">'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="draft_version" value="{state.draft_version}">'
                f'<input type="hidden" name="request_id" value="{state.request_id}">')
        if step == STEP_INSTITUTION:
            options = ''.join(
                f'<option value="{iid}" data-label="{INSTITUTION_LABELS.get(iid, iid)}">'
                f'{INSTITUTION_LABELS.get(iid, iid)}</option>'
                for iid in self.options)
            current = state.content.get('institution', '')
            body = (f'<select name="institution">{options}</select>'
                    f'<p>当前选择：{INSTITUTION_LABELS.get(current, "未选择")}（{current or "-"}）</p>')
        elif step == STEP_PROJECT:
            institution = state.content.get('institution')
            legal = self.options.get(institution, [])
            current_project = state.content.get('project', '')
            options = ''.join(
                f'<option value="{pid}" data-label="{PROJECT_LABELS.get(pid, pid)}"'
                f'{" selected" if pid == current_project else ""}>'
                f'{PROJECT_LABELS.get(pid, pid)}</option>' for pid in legal)
            # 附件要求随项目变化：列出本机构每个合法项目各自的附件要求
            attachment_items = ''.join(
                f'<li data-attachment="{item["key"]}" data-required="{str(item["required"]).lower()}" '
                f'data-label="{item["label"]}" data-project="{pid}">'
                f'<input type="checkbox" name="att_{item["key"]}"'
                f'{" checked" if state.attachments.get(item["key"]) else ""}>'
                f'{PROJECT_LABELS.get(pid, pid)} / {item["label"]}'
                f'{"（必交）" if item["required"] else "（可选）"}</li>'
                for pid in legal
                for item in ATTACHMENT_RULES.get(pid, []))
            body = (
                f'<select name="project">{options}</select>'
                f'<input type="text" name="amount" value="{state.content.get("amount", "")}">'
                f'<ul data-attachments="1">{attachment_items}</ul>'
                f'<p data-project-owner="{institution}">合法项目与附件要求以本表单为准</p>')
        elif step == STEP_APPROVAL:
            bound = ''.join(
                f'<li>{field} = {state.content.get(field, "")}</li>'
                for field in self.bound_fields)
            fingerprint = bound_fingerprint(state.content, self.bound_fields)
            body = (f'<ul data-bound="1">{bound}</ul>'
                    f'<p>内容指纹：{fingerprint}</p>'
                    '<select name="decision"><option value="approved">批准</option>'
                    '<option value="rejected">退回</option></select>')
        else:
            approval = state.approval
            body = (
                '<p>提交前复核：</p>'
                f'<p>批准指纹：{approval["fingerprint"] if approval else "无"}</p>'
                f'<p>当前指纹：{bound_fingerprint(state.content, self.bound_fields)}</p>'
                '<button name="confirm" value="1">确认提交</button>')
        return ('text/html; charset=utf-8',
                f'<!doctype html>{head}{body}</form>')

    def _render_canceled(self, state):
        html = (f'<!doctype html><div data-state="canceled">'
                f'申请 {state.request_id} 已取消（草稿停在 v{state.draft_version}），不可恢复创建</div>')
        return 'text/html; charset=utf-8', html

    def _render_done(self, state):
        html = (f'<!doctype html><div data-state="submitted">'
                f'申请 {state.request_id} 已提交，收据 {state.confirmations.get("submit", {}).get("detail", {}).get("receipt", "")}</div>')
        return 'text/html; charset=utf-8', html


def create_server(portal):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *arguments):
            return None

        def _send(self, status, content_type, body, extra_headers=None):
            data = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _send_error(self, error):
            payload = json.dumps(dict(code=error.code, message=error.message,
                                      **error.extra), ensure_ascii=False)
            self._send(error.status, 'application/json; charset=utf-8', payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            portal.access_log.append(dict(method='GET', path=self.path))
            state_match = re.fullmatch(r'/state/([^/]+)', parsed.path)
            if state_match:
                snapshot = portal.snapshot(state_match.group(1))
                if snapshot is None:
                    self._send_error(PortalError(404, 'not_found', '申请不存在'))
                    return
                self._send(200, 'application/json; charset=utf-8',
                           json.dumps(snapshot, ensure_ascii=False, default=str))
                return
            match = re.fullmatch(r'/portal/([^/]+)(?:/([^/]+))?', parsed.path)
            if not match:
                self._send_error(PortalError(404, 'not_found', '未知路径'))
                return
            try:
                content_type, body = portal.handle_get(match.group(1),
                                                       match.group(2))
            except PortalError as error:
                self._send_error(error)
            else:
                self._send(200, content_type, body)

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length).decode('utf-8')
            form = {key: values[-1]
                    for key, values in parse_qs(raw, keep_blank_values=True).items()}
            portal.access_log.append(dict(method='POST', path=self.path, form=form))
            match = re.fullmatch(r'/portal/([^/]+)/([^/]+)', parsed.path)
            admin = re.fullmatch(r'/admin/([^/]+)/([^/]+)', parsed.path)
            try:
                if match:
                    content_type, body = portal.handle_post(
                        match.group(1), match.group(2), form)
                elif admin:
                    request_id, action = admin.group(1), admin.group(2)
                    if action == 'cancel':
                        portal.cancel(request_id)
                    elif action == 'external_edit':
                        portal.external_edit(request_id, form.get('amount', '0'))
                    else:
                        raise PortalError(404, 'not_found', '未知管理动作')
                    self._send(200, 'application/json; charset=utf-8',
                               json.dumps(dict(ok=True), ensure_ascii=False))
                    return
                else:
                    raise PortalError(404, 'not_found', '未知路径')
            except PortalError as error:
                self._send_error(error)
            else:
                self._send(200, content_type, body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f'http://127.0.0.1:{server.server_port}'


class PortalSession:
    """录入程序侧的门户 HTTP 客户端，按实际返回的表单推进。"""

    def __init__(self, base, client=None):
        self.base = base
        self.client = client or Client()

    def get_form(self, request_id, step=None):
        path = f'/portal/{request_id}' + (f'/{step}' if step else '')
        status, headers, raw = self.client.request(self.base + path)
        body = raw.decode('utf-8')
        if status >= 400:
            raise PortalError(status, *self._error(body))
        return body

    def post(self, request_id, step, payload):
        body = '&'.join(f'{key}={value}' for key, value in payload.items())
        status, headers, raw = self.client.request(
            f'{self.base}/portal/{request_id}/{step}', method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            body=body.encode('utf-8'))
        text = raw.decode('utf-8')
        if status >= 400:
            code, message, extra = self._error(text)
            raise PortalError(status, code, message, extra)
        return text

    def admin_cancel(self, request_id):
        status, headers, raw = self.client.request(
            f'{self.base}/admin/{request_id}/cancel', method='POST', body=b'')
        if status >= 400:
            raise PortalError(status, *self._error(raw.decode('utf-8')))

    def admin_external_edit(self, request_id, amount):
        body = f'amount={amount}'.encode('utf-8')
        status, headers, raw = self.client.request(
            f'{self.base}/admin/{request_id}/external_edit', method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            body=body)
        if status >= 400:
            raise PortalError(status, *self._error(raw.decode('utf-8')))

    @staticmethod
    def _error(body):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return 'error', body, {}
        return data.get('code', 'error'), data.get('message', body), {
            key: value for key, value in data.items()
            if key not in ('code', 'message')}
