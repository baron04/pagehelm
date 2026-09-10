/**
 * Reusable assistant panel and deterministic UI state model.
 *
 * This component contains no session capability, page controller, model access, planning,
 * or application data. Embedded and privacy-safe replay clients supply events through its
 * narrow methods.
 */

export const PANEL_STATES = Object.freeze({
  connecting: {labelKey: 'statusConnecting', tone: 'neutral'},
  ready: {labelKey: 'statusReady', tone: 'ready'},
  queued: {labelKey: 'statusQueued', tone: 'active'},
  observing: {labelKey: 'statusObserving', tone: 'active'},
  acting: {labelKey: 'statusActing', tone: 'active'},
  knowledge: {labelKey: 'statusKnowledge', tone: 'active'},
  stopping: {labelKey: 'statusStopping', tone: 'warning'},
  reconnecting: {labelKey: 'statusReconnecting', tone: 'warning'},
  awaiting_confirmation: {labelKey: 'statusAwaitingConfirmation', tone: 'warning'},
  needs_input: {labelKey: 'statusNeedsInput', tone: 'warning'},
  success: {labelKey: 'statusSuccess', tone: 'success'},
  cancelled: {labelKey: 'statusCancelled', tone: 'neutral'},
  refused: {labelKey: 'statusRefused', tone: 'error'},
  indeterminate: {labelKey: 'statusIndeterminate', tone: 'warning'},
  error: {labelKey: 'statusError', tone: 'error'},
  ended: {labelKey: 'statusEnded', tone: 'neutral'},
  replaced: {labelKey: 'statusReplaced', tone: 'neutral'},
})

const DEFAULT_PANEL_LABELS = Object.freeze({
  assistant: 'AI 助手', product: 'WEB OPERATOR', timeline: '任务进度',
  emptyTitle: '从一个目标开始', emptyDetail: '我会在每次操作后重新观察页面。',
  input: '描述你想完成的目标', placeholder: '例如：创建一家公司并预填表单',
  send: '发送', stop: '停止', collapse: '收起助手', expand: '展开助手',
  keyHint: 'Enter 发送 · Shift + Enter 换行', approvalTitle: '执行前请确认',
  approvalEffect: '将执行', approvalTargets: '影响对象', deny: '返回修改', approve: '确认执行',
  approvalHint: '只会授权上述操作；页面变化后需要重新确认。', retry: '重试', dismiss: '关闭提示',
  goalPrefix: '目标：', progressRunning: '正在执行', progressStopping: '正在安全停止当前任务',
  progressStopFailed: '停止请求未送达，请重试', progressApprovalRequired: '需要确认后才能继续',
  progressApproved: '已确认执行', progressDenied: '已取消操作', progressTerminal: '任务结束',
  statusConnecting: '正在连接', statusReady: '已就绪', statusQueued: '已提交',
  statusObserving: '正在观察', statusActing: '正在执行', statusKnowledge: '正在检索知识',
  statusStopping: '正在停止', statusReconnecting: '正在重连',
  statusAwaitingConfirmation: '等待确认', statusNeedsInput: '等待补充',
  statusSuccess: '已完成', statusCancelled: '已取消', statusRefused: '无法执行',
  statusIndeterminate: '结果不确定', statusError: '任务失败', statusEnded: '已结束',
  statusReplaced: '已由另一页面接管',
})

export class AssistantPanelViewModel {
  constructor(onChange = () => {}, labels = DEFAULT_PANEL_LABELS) {
    this.onChange = onChange
    this.labels = labels
    this.state = 'connecting'
    this.label = this.labels[PANEL_STATES.connecting.labelKey]
    this.busy = false
    this.stopping = false
    this.collapsed = false
  }

  snapshot() {
    return Object.freeze({state: this.state, label: this.label, busy: this.busy,
      stopping: this.stopping, collapsed: this.collapsed,
      tone: PANEL_STATES[this.state].tone})
  }

  transition(state, label = '') {
    if (!Object.hasOwn(PANEL_STATES, state)) throw new Error(`unknown assistant panel state: ${state}`)
    this.state = state
    this.label = String(label || this.labels[PANEL_STATES[state].labelKey]).slice(0, 80)
    this.onChange(this.snapshot())
  }

  setBusy(value) {
    this.busy = Boolean(value)
    if (!this.busy) this.stopping = false
    this.onChange(this.snapshot())
  }

  setStopping(value) {
    this.stopping = Boolean(value)
    this.onChange(this.snapshot())
  }

  setCollapsed(value) {
    this.collapsed = Boolean(value)
    this.onChange(this.snapshot())
  }
}

export function panelErrorMessage(error, action = 'submit') {
  const code = String(error?.code || '')
  if (code === 'backpressure') return '当前任务较多，请稍后重试。'
  if (code === 'session_closed' || code === 'unknown_session') return '会话已结束，请刷新页面重新连接。'
  if (code === 'runtime_replaced') return '当前会话已由另一页面接管。'
  if (['unauthorized', 'principal_mismatch', 'origin_refused'].includes(code)) {
    return '登录或页面身份已变化，请刷新后重试。'
  }
  if (action === 'cancel') return '停止请求未送达，任务可能仍在执行。请重试。'
  if (action === 'connect') return '无法连接助手服务，请检查网络后重试。'
  return '任务未提交，请检查连接后重试。'
}

export class AssistantPanel {
  constructor({labels = {}, documentRef = document, mode = 'interactive'} = {}) {
    this.document = documentRef
    this.labels = {...DEFAULT_PANEL_LABELS, ...labels}
    this.mode = mode === 'replay' ? 'replay' : 'interactive'
    this.host = this.document.createElement('aside')
    this.host.id = 'woa-assistant'
    this.host.setAttribute('data-page-agent-not-interactive', 'true')
    this.host.setAttribute('aria-label', this.labels.assistant)
    // The host carries the executor exclusion marker. The open root makes this surface testable
    // by the trusted first-party application; shadow mode is not an authorization boundary.
    this.shadow = this.host.attachShadow({mode: 'open'})
    this.shadow.innerHTML = `
      <style>
        :host { all:initial; --woa-ink:#182033; --woa-muted:#667085; --woa-line:#d8deea;
          --woa-paper:#fbfcff; --woa-panel:#f4f6fb; --woa-accent:#5b5ce2; --woa-focus:#4656d9;
          position:fixed; right:20px; bottom:20px; z-index:2147483000;
          width:min(392px, calc(100vw - 32px)); color:var(--woa-ink); color-scheme:light dark;
          font:14px/1.45 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          -webkit-font-smoothing:antialiased; }
        * { box-sizing:border-box; }
        button, textarea { font:inherit; }
        .panel { position:relative; overflow:hidden; border:1px solid rgba(113,126,158,.28);
          border-radius:22px; background:rgba(251,252,255,.97);
          box-shadow:0 24px 72px rgba(24,32,51,.20), 0 3px 14px rgba(24,32,51,.08);
          isolation:isolate; max-height:calc(100vh - 16px); }
        .panel::before { content:""; position:absolute; z-index:3; inset:-1px; pointer-events:none;
          border-radius:22px; padding:2px; opacity:0; background:linear-gradient(115deg,
          #6a5cff, #32b8d8, #62cf9b, #f3b35d, #df6b9c, #6a5cff); background-size:260% 260%;
          -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
          -webkit-mask-composite:xor; mask-composite:exclude; }
        .panel[data-tone="active"]::before { opacity:.9; animation:woa-spectrum 3.2s linear infinite; }
        @keyframes woa-spectrum { from { background-position:0 50%; } to { background-position:100% 50%; } }
        header { min-height:66px; display:flex; align-items:center; gap:12px; padding:12px 14px;
          border-bottom:1px solid rgba(113,126,158,.16); background:rgba(255,255,255,.58); }
        .brand { min-width:0; display:flex; align-items:center; gap:10px; flex:1; }
        .mark { display:grid; place-items:center; width:34px; height:34px; border-radius:12px;
          color:#fff; background:linear-gradient(145deg,#4d55d7,#7774ee); font-size:18px;
          box-shadow:inset 0 1px rgba(255,255,255,.28), 0 5px 14px rgba(77,85,215,.24); }
        .titles { display:grid; min-width:0; }
        .kicker { color:var(--woa-muted); font:650 9px/1.2 ui-monospace, "SFMono-Regular", Consolas, monospace;
          letter-spacing:.14em; }
        strong { margin-top:2px; font-size:14px; line-height:1.2; letter-spacing:-.01em; }
        .session-state { display:flex; align-items:center; gap:6px; max-width:132px; color:var(--woa-muted); }
        .state-dot { flex:0 0 auto; width:7px; height:7px; border-radius:50%; background:#98a2b3; }
        [data-tone="ready"] .state-dot, [data-tone="success"] .state-dot { background:#1f9d6a; }
        [data-tone="active"] .state-dot { background:#655fe7; box-shadow:0 0 0 4px rgba(101,95,231,.12); }
        [data-tone="warning"] .state-dot { background:#d17b21; }
        [data-tone="error"] .state-dot { background:#d14343; }
        output { overflow:hidden; color:inherit; font-size:11px; white-space:nowrap; text-overflow:ellipsis; }
        .icon-button { display:grid; place-items:center; flex:0 0 auto; width:36px; height:36px; padding:0;
          border:0; border-radius:11px; color:var(--woa-muted); background:transparent; cursor:pointer; }
        .icon-button:hover { color:var(--woa-ink); background:rgba(91,92,226,.08); }
        .icon-button svg { width:17px; height:17px; transition:transform .18s ease; }
        .panel[data-collapsed="true"] .collapse svg { transform:rotate(180deg); }
        .body { display:grid; grid-template-rows:minmax(0,1fr) auto auto; }
        .panel[data-collapsed="true"] .body { display:none; }
        .timeline { min-height:146px; max-height:min(44vh,360px); overflow:auto; overscroll-behavior:contain;
          margin:0; padding:14px; list-style:none; background:linear-gradient(180deg,var(--woa-panel),var(--woa-paper));
          scrollbar-width:thin; scrollbar-color:#bcc3d4 transparent; }
        .empty { display:grid; align-content:center; min-height:118px; padding:18px 20px; text-align:center; }
        .empty b { font-size:14px; }
        .empty span { margin-top:5px; color:var(--woa-muted); font-size:12px; }
        .entry { position:relative; margin:0 0 8px; padding:9px 11px 9px 31px; border:1px solid transparent;
          border-radius:13px; color:#485269; background:rgba(255,255,255,.7); overflow-wrap:anywhere; }
        .entry > span { white-space:pre-wrap; }
        .entry:last-child { margin-bottom:0; }
        .entry::before { content:""; position:absolute; top:14px; left:13px; width:7px; height:7px;
          border-radius:50%; background:#9aa4b7; box-shadow:0 0 0 4px rgba(154,164,183,.12); }
        .entry[data-kind="goal"] { margin-top:10px; padding:11px 13px; border-color:#d9dcfb;
          color:#292d65; background:#f1f1ff; font-weight:620; }
        .entry[data-kind="goal"]::before { display:none; }
        .entry[data-tone="success"] { border-color:#cde8dc; color:#196247; background:#f0faf5; }
        .entry[data-tone="success"]::before { background:#269b6a; }
        .entry[data-tone="warning"] { border-color:#eedfc6; color:#82521d; background:#fff8ec; }
        .entry[data-tone="warning"]::before { background:#d17b21; }
        .entry[data-tone="error"] { border-color:#efd2d2; color:#8d3030; background:#fff4f4; }
        .entry[data-tone="error"]::before { background:#d14343; }
        .entry time { display:block; margin-top:3px; color:var(--woa-muted); font:10px/1.2 ui-monospace,
          "SFMono-Regular", Consolas, monospace; }
        .turn { margin:0 0 12px; padding:0; list-style:none; }
        .turn:last-child { margin-bottom:0; }
        .turn-events { margin:0; padding:0 0 0 10px; border-left:2px solid rgba(91,92,226,.14); list-style:none; }
        .turn > .entry[data-kind="goal"] { margin:0 0 8px; }
        .notice { display:none; align-items:start; grid-template-columns:1fr auto; gap:10px; margin:12px 12px 0;
          padding:11px 12px; border:1px solid #efcccc; border-radius:13px; color:#7d2f2f; background:#fff3f3; }
        .notice[data-visible="true"] { display:grid; }
        .notice p { margin:0; font-size:12px; }
        .notice-actions { display:flex; gap:6px; }
        .approval { margin:12px 12px 0; padding:14px; border:1px solid #e4cfaa; border-radius:15px;
          color:#4a351f; background:#fff9ed; box-shadow:0 8px 24px rgba(79,56,24,.08); }
        .approval-head { display:flex; align-items:center; gap:8px; margin-bottom:7px; }
        .approval-head span { display:grid; place-items:center; width:24px; height:24px; border-radius:8px;
          color:#7d511b; background:#f7e4bf; }
        .approval h2 { margin:0; font-size:14px; }
        .approval p { margin:0 0 9px; font-size:13px; overflow-wrap:anywhere; }
        .approval-label { display:block; margin-bottom:4px; color:#8a6841; font-size:10px; font-weight:700;
          letter-spacing:.08em; text-transform:uppercase; }
        .approval ul { margin:0 0 10px; padding-left:20px; }
        .approval .hint { color:#795f43; font-size:11px; }
        menu { display:flex; justify-content:flex-end; gap:8px; margin:12px 0 0; padding:0; }
        button { min-height:38px; border:0; border-radius:11px; padding:8px 13px; color:#fff;
          background:#5455d8; font-weight:650; cursor:pointer; }
        button.secondary { border:1px solid #d7dce8; color:#344054; background:#fff; }
        button:hover:not(:disabled) { filter:brightness(.97); }
        button:focus-visible, textarea:focus-visible { outline:3px solid rgba(70,86,217,.34); outline-offset:2px; }
        button:disabled, textarea:disabled { cursor:not-allowed; opacity:.52; }
        form { padding:12px 12px 10px; border-top:1px solid rgba(113,126,158,.16); background:var(--woa-paper); }
        .composer { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:end; gap:8px;
          padding:5px 5px 5px 11px; border:1px solid #cfd5e2; border-radius:15px; background:#fff;
          box-shadow:0 1px 2px rgba(24,32,51,.03); }
        .composer:focus-within { border-color:#6a6be2; box-shadow:0 0 0 3px rgba(91,92,226,.11); }
        textarea { width:100%; min-height:40px; max-height:112px; resize:none; padding:9px 0 7px; border:0;
          outline:0; color:var(--woa-ink); background:transparent; line-height:1.45; }
        textarea::placeholder { color:#98a2b3; }
        .send { display:grid; place-items:center; width:38px; height:38px; min-height:38px; padding:0; }
        .send svg { width:17px; height:17px; }
        .form-row { min-height:31px; display:flex; align-items:end; justify-content:space-between; gap:10px; padding-top:6px; }
        .key-hint { color:var(--woa-muted); font-size:10px; }
        .stop { display:none; min-height:31px; padding:5px 10px; border:1px solid #e0c6c6;
          color:#973b3b; background:#fff5f5; font-size:11px; }
        .panel[data-busy="true"] .stop { display:block; }
        .panel[data-mode="replay"] form { display:none; }
        .panel[data-mode="replay"] .timeline { max-height:min(52vh,420px); }
        .sr-only { position:absolute!important; width:1px!important; height:1px!important; padding:0!important;
          margin:-1px!important; overflow:hidden!important; clip:rect(0,0,0,0)!important; white-space:nowrap!important;
          border:0!important; }
        @media (prefers-color-scheme:dark) {
          :host { --woa-ink:#eef1f8; --woa-muted:#a8b0c0; --woa-line:#394052; --woa-paper:#161a24;
            --woa-panel:#11151e; --woa-focus:#9298ff; }
          .panel { border-color:#3a4153; background:rgba(22,26,36,.98);
            box-shadow:0 24px 72px rgba(0,0,0,.46),0 3px 14px rgba(0,0,0,.2); }
          header { background:rgba(31,36,49,.7); }
          .entry { color:#cad0dd; background:rgba(38,44,58,.72); }
          .entry[data-kind="goal"] { border-color:#484b83; color:#e2e1ff; background:#292a50; }
          .entry[data-tone="success"] { border-color:#315b4d; color:#bfe6d5; background:#17382e; }
          .entry[data-tone="warning"] { border-color:#634d2f; color:#f0d2a9; background:#3d2d19; }
          .entry[data-tone="error"] { border-color:#633b3e; color:#f2c1c3; background:#3d2225; }
          .composer { border-color:#454c60; background:#202532; }
          .notice { border-color:#633b3e; color:#f2c1c3; background:#3d2225; }
          .approval { border-color:#655236; color:#f1dab9; background:#382b1a; }
          .approval-head span { color:#f0c77f; background:#584324; }
          .approval .hint, .approval-label { color:#c4a77e; }
          button.secondary { border-color:#4b5265; color:#d9deea; background:#292f3e; }
          .stop { border-color:#694145; color:#efb8bb; background:#352124; }
        }
        @media (max-width:520px) {
          :host { right:8px; bottom:max(8px, env(safe-area-inset-bottom)); width:calc(100vw - 16px); }
          .panel { border-radius:20px; }
          .body { max-height:calc(100vh - 82px); overflow:auto; overscroll-behavior:contain; }
          .timeline { max-height:42vh; }
          .key-hint { display:none; }
        }
        @media (max-width:360px) {
          .approval { max-height:58vh; overflow:auto; }
          .approval menu { flex-direction:column-reverse; }
          .approval menu button { width:100%; }
        }
        @media (prefers-reduced-motion:reduce) {
          *, *::before, *::after { animation:none!important; scroll-behavior:auto!important; transition:none!important; }
        }
      </style>
      <section class="panel" data-state="connecting" data-tone="neutral" data-busy="false" data-collapsed="false" data-mode="interactive"
        aria-labelledby="woa-panel-title">
        <header>
          <div class="brand"><span class="mark" aria-hidden="true">✦</span><span class="titles">
            <span class="kicker"></span><strong id="woa-panel-title"></strong></span></div>
          <span class="session-state"><span class="state-dot" aria-hidden="true"></span>
            <output role="status" aria-live="polite" aria-atomic="true"></output></span>
          <button type="button" class="icon-button collapse" aria-expanded="true" aria-controls="woa-panel-body">
            <span class="sr-only"></span><svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="m5 8 5 5 5-5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            </svg></button>
        </header>
        <div class="body" id="woa-panel-body">
          <div class="notice" role="alert" data-visible="false"><p></p><span class="notice-actions">
            <button type="button" class="retry"></button><button type="button" class="icon-button dismiss" aria-label=""></button>
          </span></div>
          <ol class="timeline" role="log" aria-live="polite" aria-relevant="additions" aria-label=""></ol>
          <form><label class="sr-only" for="woa-goal"></label><div class="composer">
            <textarea id="woa-goal" rows="1" maxlength="2000"></textarea>
            <button type="submit" class="send"><span class="sr-only"></span><svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="m4 10 11-6-3.5 12-2.2-4.1L4 10Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
              <path d="m9.3 11.9 2.2-2.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>
            </svg></button></div><div class="form-row"><span class="key-hint"></span>
            <button type="button" class="stop"></button></div></form>
        </div>
      </section>`
    this._wireLabels()
    this.shadow.querySelector('.panel').dataset.mode = this.mode
    this.document.documentElement.appendChild(this.host)
    this.model = new AssistantPanelViewModel(snapshot => this._render(snapshot), this.labels)
    this.turns = new Map()
    this.activeTurnId = ''
    this.cancelPendingApproval = () => {}
    this.retryAction = null
    this.taskHandler = null
    this.cancelHandler = null
    this.lastGoal = ''
    this.focusWhenReady = false
    this._bind()
    this._render(this.model.snapshot())
    this._renderEmpty()
  }

  _wireLabels() {
    const q = selector => this.shadow.querySelector(selector)
    q('.kicker').textContent = this.labels.product
    q('strong').textContent = this.labels.assistant
    q('.collapse .sr-only').textContent = this.labels.collapse
    q('.timeline').setAttribute('aria-label', this.labels.timeline)
    q('label').textContent = this.labels.input
    q('textarea').placeholder = this.labels.placeholder
    q('.send .sr-only').textContent = this.labels.send
    q('.key-hint').textContent = this.labels.keyHint
    q('.stop').textContent = this.labels.stop
    q('.retry').textContent = this.labels.retry
    q('.dismiss').setAttribute('aria-label', this.labels.dismiss)
    q('.dismiss').textContent = '×'
  }

  _bind() {
    const q = selector => this.shadow.querySelector(selector)
    const stopHostPropagation = event => event.stopPropagation()
    q('.collapse').addEventListener('click', () => this.model.setCollapsed(!this.model.collapsed))
    q('.dismiss').addEventListener('click', () => this.clearError())
    q('.retry').addEventListener('click', () => { if (this.retryAction) void this.retryAction() })
    q('textarea').addEventListener('input', event => {
      event.stopPropagation()
      event.currentTarget.style.height = 'auto'
      event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 112)}px`
    })
    for (const name of ['beforeinput', 'change', 'compositionstart', 'compositionupdate',
      'compositionend', 'keypress', 'keyup']) {
      q('textarea').addEventListener(name, stopHostPropagation)
    }
    q('textarea').addEventListener('keydown', event => {
      event.stopPropagation()
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault()
        q('form').requestSubmit()
      }
    })
    q('form').addEventListener('submit', event => {
      event.preventDefault()
      event.stopPropagation()
      void this._submitFromComposer()
    })
    q('.stop').addEventListener('click', () => { void this._cancelFromPanel() })
  }

  _render(snapshot) {
    const panel = this.shadow.querySelector('.panel')
    panel.dataset.state = snapshot.state
    panel.dataset.tone = snapshot.tone
    panel.dataset.busy = String(snapshot.busy)
    panel.dataset.collapsed = String(snapshot.collapsed)
    const output = this.shadow.querySelector('output')
    output.textContent = snapshot.label
    const input = this.shadow.querySelector('textarea')
    input.disabled = snapshot.busy
    this.shadow.querySelector('.send').disabled = snapshot.busy
    this.shadow.querySelector('.stop').disabled = !snapshot.busy || snapshot.stopping
    const collapse = this.shadow.querySelector('.collapse')
    collapse.setAttribute('aria-expanded', String(!snapshot.collapsed))
    collapse.querySelector('.sr-only').textContent = snapshot.collapsed ? this.labels.expand : this.labels.collapse
  }

  _renderEmpty() {
    const empty = this.document.createElement('li')
    empty.className = 'empty'
    empty.setAttribute('data-empty', 'true')
    const title = this.document.createElement('b'); title.textContent = this.labels.emptyTitle
    const detail = this.document.createElement('span'); detail.textContent = this.labels.emptyDetail
    empty.append(title, detail)
    this.shadow.querySelector('.timeline').appendChild(empty)
  }

  async _submitFromComposer(valueOverride = '') {
    if (!this.taskHandler) return
    const input = this.shadow.querySelector('textarea')
    const value = String(valueOverride || input.value).trim()
    if (!value) return
    this.lastGoal = value
    this.clearError()
    input.value = ''
    input.style.height = 'auto'
    try {
      await this.taskHandler(value)
      this.retryAction = null
    } catch (error) {
      if (!input.value) input.value = value
      const retryable = !['session_closed', 'unknown_session', 'runtime_replaced',
        'unauthorized', 'principal_mismatch', 'origin_refused'].includes(String(error?.code || ''))
      const state = error?.code === 'runtime_replaced' ? 'replaced'
        : (['session_closed', 'unknown_session'].includes(error?.code) ? 'ended' : 'error')
      this.state(state)
      this.showError(panelErrorMessage(error), retryable ? () => this._submitFromComposer(value) : null)
      if (!this.model.busy) input.focus()
    }
  }

  async _cancelFromPanel() {
    if (!this.cancelHandler) return
    try {
      await this.cancelHandler()
    } catch (error) {
      this.showError(panelErrorMessage(error, 'cancel'), () => this._cancelFromPanel())
    }
  }

  state(name, label = '') { this.model.transition(name, label) }

  label(name) { return String(this.labels[name] || '') }

  setBusy(value) {
    this.model.setBusy(value)
    if (!value && this.focusWhenReady) {
      this.focusWhenReady = false
      queueMicrotask(() => this.focusComposer())
    }
  }

  setStopping(value) { this.model.setStopping(value) }

  clear() {
    this.shadow.querySelector('.timeline').replaceChildren()
    this.turns.clear()
    this.activeTurnId = ''
    this._renderEmpty()
  }

  beginTurn(turnId, goal) {
    const boundedId = String(turnId || '').slice(0, 200)
    if (!boundedId) throw new Error('assistant panel turn id is required')
    this.shadow.querySelector('[data-empty]')?.remove()
    let turn = this.turns.get(boundedId)
    if (!turn) {
      turn = this.document.createElement('li')
      turn.className = 'turn'
      turn.dataset.turnId = boundedId
      const goalItem = this._entry(`${this.labels.goalPrefix}${String(goal)}`, 'goal', 'neutral', '')
      const events = this.document.createElement('ol')
      events.className = 'turn-events'
      turn.append(goalItem, events)
      this.shadow.querySelector('.timeline').appendChild(turn)
      this.turns.set(boundedId, turn)
      while (this.turns.size > 8) {
        const [oldestId, oldestTurn] = this.turns.entries().next().value
        this.turns.delete(oldestId)
        oldestTurn.remove()
      }
    }
    this.activeTurnId = boundedId
    const timeline = this.shadow.querySelector('.timeline')
    timeline.scrollTop = timeline.scrollHeight
    return turn
  }

  _entry(value, kind, tone, timeLabel) {
    const item = this.document.createElement('li')
    item.className = 'entry'
    item.dataset.kind = kind
    item.dataset.tone = tone
    const content = this.document.createElement('span')
    content.textContent = String(value)
    const timestamp = this.document.createElement('time')
    const now = new Date()
    timestamp.dateTime = now.toISOString()
    timestamp.textContent = String(timeLabel || '') || new Intl.DateTimeFormat(undefined, {
      hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(now)
    item.append(content, timestamp)
    return item
  }

  add(value, {kind = 'progress', tone = 'neutral', timestamp: timeLabel = '', turnId = ''} = {}) {
    this.shadow.querySelector('[data-empty]')?.remove()
    const message = String(value)
    const selectedTurnId = String(turnId || this.activeTurnId || '')
    const turn = selectedTurnId ? this.turns.get(selectedTurnId) : null
    const list = turn?.querySelector('.turn-events') || this.shadow.querySelector('.timeline')
    const previous = list.lastElementChild
    if (kind === 'progress' && previous?.dataset.kind === 'progress'
        && previous.querySelector('span')?.textContent === message) {
      const previousTime = previous.querySelector('time')
      const now = new Date()
      previousTime.dateTime = now.toISOString()
      previousTime.textContent = String(timeLabel || '') || new Intl.DateTimeFormat(undefined, {
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).format(now)
      return
    }
    const item = this._entry(message, kind, tone, timeLabel)
    list.appendChild(item)
    while (list.children.length > 30) list.firstElementChild.remove()
    const timeline = this.shadow.querySelector('.timeline')
    timeline.scrollTop = timeline.scrollHeight
  }

  showError(message, retry = null) {
    const notice = this.shadow.querySelector('.notice')
    notice.querySelector('p').textContent = String(message).slice(0, 300)
    this.retryAction = typeof retry === 'function' ? retry : null
    notice.querySelector('.retry').hidden = !this.retryAction
    notice.dataset.visible = 'true'
    this.model.setCollapsed(false)
  }

  clearError() {
    this.retryAction = null
    this.shadow.querySelector('.notice').dataset.visible = 'false'
  }

  onTask(handler) { this.taskHandler = handler }

  onCancel(handler) { this.cancelHandler = handler }

  focusComposer() { if (!this.model.busy) this.shadow.querySelector('textarea').focus() }

  cancelApproval() { this.cancelPendingApproval() }

  requestApproval(request) {
    return new Promise(resolve => {
      this.shadow.querySelector('.approval')?.remove()
      this.model.setCollapsed(false)
      const priorFocus = this.shadow.activeElement
      const card = this.document.createElement('section')
      card.className = 'approval'
      card.setAttribute('role', 'alertdialog')
      card.setAttribute('aria-modal', 'false')
      const titleId = `woa-approval-title-${String(request.approval_id || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 40) || 'pending'}`
      const descriptionId = `${titleId}-description`
      card.setAttribute('aria-labelledby', titleId)
      card.setAttribute('aria-describedby', descriptionId)
      const head = this.document.createElement('div'); head.className = 'approval-head'
      const icon = this.document.createElement('span'); icon.setAttribute('aria-hidden', 'true'); icon.textContent = '!'
      const title = this.document.createElement('h2'); title.id = titleId; title.textContent = this.labels.approvalTitle
      head.append(icon, title)
      const effectLabel = this.document.createElement('span'); effectLabel.className = 'approval-label'; effectLabel.textContent = this.labels.approvalEffect
      const effect = this.document.createElement('p'); effect.id = descriptionId; effect.textContent = String(request.effect).slice(0, 300)
      const targetLabel = this.document.createElement('span'); targetLabel.className = 'approval-label'; targetLabel.textContent = this.labels.approvalTargets
      const targets = this.document.createElement('ul')
      for (const value of request.targets || []) {
        const item = this.document.createElement('li'); item.textContent = String(value).slice(0, 300); targets.appendChild(item)
      }
      const hint = this.document.createElement('p'); hint.className = 'hint'; hint.textContent = this.labels.approvalHint
      const menu = this.document.createElement('menu')
      const deny = this.document.createElement('button')
      deny.type = 'button'; deny.className = 'secondary deny'; deny.textContent = this.labels.deny
      const approve = this.document.createElement('button')
      approve.type = 'button'; approve.textContent = this.labels.approve
      menu.append(deny, approve)
      card.append(head, effectLabel, effect, targetLabel, targets, hint, menu)
      this.shadow.querySelector('.body').insertBefore(card, this.shadow.querySelector('form'))
      let settled = false
      const finish = decision => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        card.remove()
        this.cancelPendingApproval = () => {}
        if (decision === 'denied') {
          const composer = this.shadow.querySelector('textarea')
          if (!composer.value) composer.value = this.lastGoal
          this.focusWhenReady = true
        }
        if (priorFocus?.isConnected) priorFocus.focus()
        resolve(decision)
      }
      const onKeyDown = event => {
        if (event.key === 'Escape') { event.preventDefault(); finish('denied'); return }
        if (event.key !== 'Tab') return
        const first = deny; const last = approve
        if (event.shiftKey && this.shadow.activeElement === first) { event.preventDefault(); last.focus() }
        if (!event.shiftKey && this.shadow.activeElement === last) { event.preventDefault(); first.focus() }
      }
      card.addEventListener('keydown', onKeyDown)
      this.cancelPendingApproval = () => finish('denied')
      deny.addEventListener('click', () => finish('denied'), {once: true})
      approve.addEventListener('click', () => finish('approved'), {once: true})
      const remaining = Math.max(0, Number(request.expires_at || 0) - Date.now())
      const timer = setTimeout(() => finish('denied'), Math.min(remaining, 2_147_483_647))
      queueMicrotask(() => deny.focus())
    })
  }
}

export function createAssistantPanel(options = {}) { return new AssistantPanel(options) }

export function setPanelState(panel, state, label = '') {
  if (typeof panel?.state === 'function') panel.state(state, label)
  else panel?.status?.(state, label)
}

export function panelLabel(panel, name) {
  return String(panel?.label?.(name) || DEFAULT_PANEL_LABELS[name] || '')
}
