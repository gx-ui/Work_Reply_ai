// 智能客服助手插件 - 简洁版
(function() {
  'use strict';

  if (window.__csAssistAiInitialized) {
    return;
  }
  window.__csAssistAiInitialized = true;

  // ========== 核心配置 ==========

  // localStorage.removeItem('ai_api_base'); localStorage.removeItem('ai_api_mode');
  const CONFIG = {
    API_BASE: (() => {
      const DEFAULT_SERVER_URL = 'https://ai-gateway-show.yunzhonghe.com/cs_assist_ai';
      // 与根 content.js 一致：未开本地开关时用固定本地地址变量（仅 forceLocal 为 true 时生效）
      const localUrl = 'http://localhost:8003';
      const serverUrl = localStorage.getItem('ai_server_api_base') || DEFAULT_SERVER_URL;

      const forceLocal = localStorage.getItem('ai_use_local_api') === 'true';
      const forceServer = localStorage.getItem('ai_use_server_api') === 'true';

      // 与根 content.js：强制本地 > 强制远端（一般与默认一致可省略）> 默认网关
      if (forceLocal) {
        console.log('[AI助手] 使用本地API:', localUrl);
        return localUrl;
      }
      if (forceServer) {
        console.log('[AI助手] 强制使用服务器API:', serverUrl);
        return serverUrl;
      }
      console.log('[AI助手] 默认使用服务器API:', serverUrl);
      return serverUrl;
    })(),
    INIT_SCAN_INTERVAL: 250, // 首屏快速探测
    INIT_SCAN_TIMEOUT: 15000, // 首屏探测最多15秒
    HEARTBEAT_INTERVAL: 3000,
    MIN_MESSAGES: 1          // 最少需要1条消息
  };

  /** 与 cs_assist_ai 一致：API_BASE 已含网关前缀时只拼短路径；仅 host:port 时补全前缀 */
  function resolveChatApiUrl() {
    const base = String(CONFIG.API_BASE || '').trim().replace(/\/+$/, '');
    if (base.endsWith('/cs_assist_ai') || base.endsWith('/work_reply_ai')) {
      return `${base}/chat`;
    }
    return `${base}/cs_assist_ai/chat`;
  }

  /** 与 resolveChatApiUrl 对应，POST .../chat/stream（SSE） */
  function resolveChatStreamApiUrl() {
    const base = String(CONFIG.API_BASE || '').trim().replace(/\/+$/, '');
    if (base.endsWith('/cs_assist_ai') || base.endsWith('/work_reply_ai')) {
      return `${base}/chat/stream`;
    }
    return `${base}/cs_assist_ai/chat/stream`;
  }
  // ========== 全局变量 ==========
  // ========== 状态管理 ==========
  const AI_STATES = {
    IDLE: 'idle',
    GENERATING: 'generating',
    SHOWING: 'showing',
    AGENT_REPLIED: 'agent_replied',
    ERROR: 'error'
  };

  const DEBUG = localStorage.getItem('ai_debug') === 'true';
  const PANEL_KEY_ATTR = 'data-work-reply-panel-key';
  const PANEL_HOST_ATTR = 'data-work-reply-panel-host';
  const PANEL_STATE_GRACE_MS = 20000;
  const panelRegistry = new Map();
  let panelHostSeq = 0;

  function getElementArea(el) {
    if (!el || !(el instanceof Element) || !el.isConnected) return 0;
    try {
      const rect = el.getBoundingClientRect();
      return Math.max(0, rect.width) * Math.max(0, rect.height);
    } catch (e) {
      return 0;
    }
  }

  function isActuallyVisible(el) {
    if (!el || !(el instanceof Element) || !el.isConnected) return false;
    try {
      const style = window.getComputedStyle(el);
      if (
        style.display === 'none' ||
        style.visibility === 'hidden' ||
        Number(style.opacity || '1') === 0
      ) {
        return false;
      }
    } catch (e) {
      return false;
    }
    const area = getElementArea(el);
    return area > 16;
  }

  function isInteractiveWorksheetScope(scopeRoot) {
    if (!scopeRoot || !(scopeRoot instanceof Element)) return false;
    if (!isActuallyVisible(scopeRoot)) return false;
    const hasWorksheetUi = Boolean(
      scopeRoot.querySelector('.m-sheet-reply') ||
      scopeRoot.querySelector('.m-sheet-main') ||
      scopeRoot.querySelector('.m-sheet-propertes') ||
      scopeRoot.querySelector('.m-sheet-rich-cont') ||
      scopeRoot.querySelector('.ql-editor') ||
      scopeRoot.querySelector('.fishd-input, textarea, input')
    );
    return hasWorksheetUi;
  }

  function getScopeHostId(scopeRoot) {
    const scope = scopeRoot instanceof Element ? scopeRoot : document.body;
    if (!scope) return 'document-body';
    let hostId = scope.getAttribute(PANEL_HOST_ATTR);
    if (!hostId) {
      panelHostSeq += 1;
      hostId = `wr-panel-host-${panelHostSeq}`;
      scope.setAttribute(PANEL_HOST_ATTR, hostId);
    }
    return hostId;
  }

  function buildPanelKey(scopeRoot) {
    const scope = scopeRoot instanceof Element ? scopeRoot : document.body;
    const existing = scope && scope.getAttribute ? scope.getAttribute(PANEL_KEY_ATTR) : '';
    if (existing) return existing;
    const ticketIdentity = getCurrentWorksheetTicketIdentity(scope);
    const hostId = getScopeHostId(scope);
    const stablePart = ticketIdentity.value
      ? `${ticketIdentity.source || 'ticket'}:${ticketIdentity.value}`
      : `scope:${hostId}`;
    return `${stablePart}::${hostId}`;
  }

  function prunePanelRegistry(scopes) {
    const now = Date.now();
    const activeHosts = new Set(
      (scopes || [])
        .filter((scope) => scope instanceof Element)
        .map((scope) => getScopeHostId(scope))
    );
    for (const [panelKey, st] of panelRegistry.entries()) {
      const scope = st && st.scopeRoot;
      const hostId = st && st.hostId;
      if (scope instanceof Element && scope.isConnected && activeHosts.has(hostId)) {
        st.lastSeenAt = now;
        continue;
      }
      if (!st.lastSeenAt) {
        st.lastSeenAt = now;
      }
      if (now - st.lastSeenAt >= PANEL_STATE_GRACE_MS) {
        panelRegistry.delete(panelKey);
      }
    }
  }

  function findReusablePanelState(scopeRoot, panelKey, ticketIdentity, hostId) {
    const ticketValue =
      ticketIdentity && ticketIdentity.value ? String(ticketIdentity.value).trim() : '';
    if (!ticketValue) return null;
    for (const [existingKey, state] of panelRegistry.entries()) {
      if (!state || existingKey === panelKey) continue;
      const stateTicket = String(state.lastWorksheetTicketId || '').trim();
      if (!stateTicket || stateTicket !== ticketValue) continue;
      const existingScope = state.scopeRoot;
      const scopeReusable =
        !(existingScope instanceof Element) ||
        !existingScope.isConnected ||
        !isInteractiveWorksheetScope(existingScope);
      if (!scopeReusable) continue;
      panelRegistry.delete(existingKey);
      state.panelKey = panelKey;
      state.hostId = hostId;
      state.scopeRoot = scopeRoot;
      state.lastSeenAt = Date.now();
      return state;
    }
    return null;
  }

  /** 每个工单面板（.m-sheet-item 或独立 .m-sheet-reply 宿主）一份状态 */
  function panelState(scopeRoot) {
    const scope = scopeRoot instanceof Element ? scopeRoot : document.body;
    const ticketIdentity = getCurrentWorksheetTicketIdentity(scope);
    const hostId = getScopeHostId(scope);
    const panelKey = buildPanelKey(scope);
    if (scope && scope.setAttribute) {
      scope.setAttribute(PANEL_KEY_ATTR, panelKey);
    }
    let state = panelRegistry.get(panelKey);
    if (!state) {
      state = findReusablePanelState(scope, panelKey, ticketIdentity, hostId);
    }
    if (!state) {
      state = {
        panelKey,
        hostId,
        scopeRoot: scope,
        currentState: AI_STATES.IDLE,
        currentSuggestion: null,
        suggestionRequestToken: null,
        queryRequestToken: null,
        summaryRequestToken: null,
        lastConversationHash: '',
        lastConversationSnapshot: null,
        lastSuggestionHash: '',
        isManualRegenerate: false,
        currentKnowledgeSources: [],
        lastWorksheetTicketId: null,
        lastWorksheetTicketSource: '',
        pendingWorksheetTicketId: null,
        pendingWorksheetTicketCount: 0,
        worksheetUpdateTimer: null,
        lastQueryAnswer: '',
        lastQueryHtml: '',
        lastQuerySources: [],
        queryStatus: 'idle',
        queryError: '',
        lastSummaryInfoHtml: '--',
        lastSummaryReviewHtml: '--',
        lastSummarySources: [],
        summaryStatus: 'idle',
        summaryError: '',
        lastUpdatedAt: 0,
        lastSeenAt: Date.now(),
      };
      if (ticketIdentity && ticketIdentity.value) {
        state.lastWorksheetTicketId = String(ticketIdentity.value);
        state.lastWorksheetTicketSource = ticketIdentity.source || '';
      }
      panelRegistry.set(panelKey, state);
    }
    state.scopeRoot = scope;
    state.hostId = hostId;
    state.lastSeenAt = Date.now();
    return state;
  }

  /**
   * 多开工单 Tab 时，每个 Tab 对应一个 scope（优先 .m-sheet-item）。
   */
  function getWorksheetPanelScopes() {
    const out = [];
    const seen = new Set();
    function add(el) {
      if (!el || !(el instanceof Element)) return;
      const host =
        el.closest('.m-sheet-item') ||
        el.closest('.m-detail-content-wrapper') ||
        el;
      if (!(host instanceof Element) || seen.has(host)) return;
      if (!isInteractiveWorksheetScope(host)) return;
      const hostId = getScopeHostId(host);
      if (seen.has(hostId)) return;
      seen.add(host);
      seen.add(hostId);
      out.push(host);
    }
    const items = document.querySelectorAll('.m-sheet-item');
    if (items.length > 0) {
      items.forEach((item) => {
        if (
          item.querySelector(
            '.m-sheet-reply, .m-sheet-main, .m-sheet-propertes, .m-sheet-rich-cont'
          )
        ) {
          add(item);
        }
      });
    }
    if (out.length > 0) {
      prunePanelRegistry(out);
      return out;
    }
    const replies = document.querySelectorAll('.m-sheet-reply');
    if (replies.length > 1) {
      replies.forEach((r) => {
        add(r.closest('.m-sheet-item') || r.closest('.m-detail-content-wrapper') || r.parentElement || r);
      });
      prunePanelRegistry(out);
      return out;
    }
    const anchor = getWorksheetUiAnchor();
    if (anchor) {
      add(anchor.closest('.m-sheet-item') || anchor);
    } else if (document.body) {
      if (isInteractiveWorksheetScope(document.body)) add(document.body);
    }
    prunePanelRegistry(out);
    return out;
  }

  function getDefaultScope() {
    const scopes = getWorksheetPanelScopes();
    return scopes[0] || document.body;
  }

  function transitionTo(scopeRoot, newState, payload = {}) {
    const st = panelState(scopeRoot);
    if (st.currentState === newState) {
      log(`状态刷新: ${newState}`);
    } else {
      st.currentState = newState;
    }
    renderState(scopeRoot, payload);
  }

  // ========== 工具函数 ==========
  function log(msg, type = 'info') {
    if (!DEBUG && type !== 'error' && type !== 'warn') return;
    if (type === 'error') {
      console.error(`[AI助手] ${msg}`);
      return;
    }
    if (type === 'warn') {
      console.warn(`[AI助手] ${msg}`);
      return;
    }
    console.log(`[AI助手] ${msg}`);
  }

  // 解析时间字符串为Unix时间戳
  function parseTimeToTimestamp(timeStr) {
    if (!timeStr || typeof timeStr !== 'string') return null;

    // 清理时间字符串
    const cleanTimeStr = timeStr.trim().replace(/\s+/g, ' ');

    try {
      let date;

      // 格式1: "2025-10-31 20:59:41" 或 "2025/10/31 20:59:41"
      if (cleanTimeStr.match(/^\d{4}[-/]\d{1,2}[-/]\d{1,2}/)) {
        date = new Date(cleanTimeStr);
      }
      // 格式2: "10-31 20:59:41" (月-日 时间)
      else if (cleanTimeStr.match(/^\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}/)) {
        const currentYear = new Date().getFullYear();
        date = new Date(`${currentYear}-${cleanTimeStr}`);
      }
      // 格式3: "20:59:41" (时分秒)
      else if (cleanTimeStr.match(/^\d{1,2}:\d{2}:\d{2}$/)) {
        const today = new Date();
        const [hours, minutes, seconds] = cleanTimeStr.split(':').map(Number);
        date = new Date(today.getFullYear(), today.getMonth(), today.getDate(), hours, minutes, seconds);
      }
      // 格式4: "20:59" (时分)
      else if (cleanTimeStr.match(/^\d{1,2}:\d{2}$/)) {
        const today = new Date();
        const [hours, minutes] = cleanTimeStr.split(':').map(Number);
        date = new Date(today.getFullYear(), today.getMonth(), today.getDate(), hours, minutes, 0);
      }
      // 格式5: 处理 AM/PM 格式
      else if (cleanTimeStr.match(/am|pm/i)) {
        date = new Date(cleanTimeStr);
      }
      // 格式6: 相对时间 - 转换为绝对时间
      else if (cleanTimeStr.includes('刚刚') || cleanTimeStr.includes('刚才')) {
        date = new Date();
      }
      else if (cleanTimeStr.includes('昨天')) {
        // 处理昨天的时间 - 从字符串中提取具体时间
        const timeMatch = cleanTimeStr.match(/(\d{1,2}):(\d{2}):(\d{2})/);
        if (timeMatch) {
          const [, hours, minutes, seconds] = timeMatch.map(Number);
          const yesterday = new Date();
          yesterday.setDate(yesterday.getDate() - 1);
          date = new Date(yesterday.getFullYear(), yesterday.getMonth(), yesterday.getDate(), hours, minutes, seconds);
        } else {
          // 如果没有具体时间，设置为昨天的同一时间
          const yesterday = new Date();
          yesterday.setDate(yesterday.getDate() - 1);
          date = yesterday;
        }
      }
      else if (cleanTimeStr.includes('分钟前')) {
        const minutes = parseInt(cleanTimeStr.match(/\d+/)?.[0] || '1');
        date = new Date(Date.now() - minutes * 60 * 1000);
      }
      else if (cleanTimeStr.includes('小时前')) {
        const hours = parseInt(cleanTimeStr.match(/\d+/)?.[0] || '1');
        date = new Date(Date.now() - hours * 60 * 60 * 1000);
      }
      // 格式7: 尝试直接解析
      else {
        date = new Date(cleanTimeStr);
      }

      // 如果解析失败，尝试添加今天的日期
      if (isNaN(date.getTime())) {
        const today = new Date().toDateString();
        date = new Date(`${today} ${cleanTimeStr}`);
      }

      // 如果还是失败，尝试其他组合
      if (isNaN(date.getTime())) {
        // 尝试 "YYYY MM DD HH:mm:ss" 格式
        date = new Date(cleanTimeStr.replace(/[-/]/g, ' '));
      }

      // 返回Unix时间戳（秒）
      if (!isNaN(date.getTime())) {
        return Math.floor(date.getTime() / 1000);
      }

    } catch (error) {
      console.warn('时间解析失败:', cleanTimeStr, error);
    }

    return null;
  }

  // 获取替代时间戳（当消息没有时间时）
  function getAlternativeTimestamp(messageElement, messageIndex) {
    try {
      // 方法1: 使用当前时间，并根据消息位置调整
      const now = Math.floor(Date.now() / 1000);

      // 根据消息在列表中的位置，估算时间
      // 假设每条消息间隔约1-3分钟
      const estimatedMinutesAgo = (30 - messageIndex) * 2; // 最多30条消息，每条间隔2分钟
      const alternativeTimestamp = now - (estimatedMinutesAgo * 60);

      // 方法2: 尝试从页面标题或其他地方获取时间信息
      const pageTitle = document.title;
      const pageDateElement = document.querySelector('.date, .chat-date, .session-date');

      let baseTimestamp = now;

      // 如果找到日期元素，使用它作为基准
      if (pageDateElement) {
        const dateText = pageDateElement.textContent.trim();
        const parsedDate = parseTimeToTimestamp(dateText);
        if (parsedDate) {
          baseTimestamp = parsedDate;
        }
      }

      // 最终时间戳 = 基准时间 - 估算的偏移量
      const finalTimestamp = baseTimestamp - (estimatedMinutesAgo * 60);

      // 移除调试信息显示

      return finalTimestamp;

    } catch (error) {
      console.warn('获取替代时间戳失败:', error);
      return Math.floor(Date.now() / 1000); // 返回当前时间作为后备
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
  }

  /** 是否工单详情页（含从基础客服 iframe 打开的 ?fromBasicKefu=1&id=） */
  function isWorksheetDetailUrlContext() {
    try {
      const p = (window.location.pathname || '').toLowerCase();
      const q = new URLSearchParams(window.location.search || '');
      if (/\/worksheet\/page\/sheet\/detail/.test(p) || /\/sheet\/detail/.test(p)) return true;
      if (q.get('fromBasicKefu') === '1' && q.get('id')) return true;
      return false;
    } catch (e) {
      return false;
    }
  }

  /**
   * 基础客服壳层：工单在子 iframe（见 dom.txt #qiyu_iframe_worksheet），顶层无 m-sheet，不应初始化。
   */
  function isBasicKefuOuterShell() {
    if (window.top !== window) return false;
    return Boolean(
      document.querySelector('#qiyu_iframe_worksheet') ||
      document.querySelector('.m-basic-kefu iframe[src*="/worksheet/page/sheet/detail"]') ||
      document.querySelector('[data-id="worksheet"] iframe[src*="sheet/detail"]')
    );
  }

  /** 挂载 AI 建议框的锚点容器（旧版 .m-sheet-reply，新版为主内容区）；scopeRoot 限定在多 Tab 某一面板内 */
  function getWorksheetUiAnchor(scopeRoot = null) {
    const base = scopeRoot && scopeRoot instanceof Element ? scopeRoot : document;
    const reply = base.querySelector('.m-sheet-reply');
    if (reply) return reply;
    const main = base.querySelector('.m-sheet-main');
    if (main) return main;
    if (isWorksheetDetailUrlContext()) {
      return (
        base.querySelector('.ant-layout-content') ||
        base.querySelector('main.g-layout-content') ||
        base.querySelector('main.ant-layout-content') ||
        base.querySelector('main') ||
        (scopeRoot instanceof Element ? scopeRoot : document.body)
      );
    }
    return null;
  }

  /** 填充回复时的查找范围：旧 .m-sheet-reply，否则在工单页主区域内找 .ql-editor 等 */
  function getReplyEditorRoot(scopeRoot = null) {
    const base = scopeRoot && scopeRoot instanceof Element ? scopeRoot : document;
    const legacy = base.querySelector('.m-sheet-reply');
    if (legacy) return legacy;
    if (isWorksheetDetailUrlContext()) {
      return (
        base.querySelector('.m-sheet-main') ||
        base.querySelector('.ant-layout-content') ||
        base.querySelector('main') ||
        (scopeRoot instanceof Element ? scopeRoot : document.body)
      );
    }
    return null;
  }

  /** 是否指定面板内具备工单 UI（多 Tab 时用） */
  function isWorksheetModeForScope(scopeRoot) {
    if (!scopeRoot || scopeRoot === document.body) {
      return isWorksheetMode();
    }
    return Boolean(
      scopeRoot.querySelector('.m-sheet-main .m-sheet-rich-cont') ||
        scopeRoot.querySelector('.m-sheet-reply') ||
        scopeRoot.querySelector('.m-sheet-propertes') ||
        (isWorksheetDetailUrlContext() &&
          (scopeRoot.querySelector('.m-sheet-main') ||
            scopeRoot.querySelector('.ant-layout-content') ||
            scopeRoot.querySelector('.fishd-form-item, .ant-form-item') ||
            scopeRoot.querySelector('.ql-editor')))
    );
  }

  /** 知识库检索 file_name 列表展示（空则隐藏容器） */
  function renderKbSourceNames(el, title, names) {
    if (!el) return;
    const arr = (names || []).map((x) => String(x || '').trim()).filter(Boolean);
    const signature = arr.length ? `${title}::${arr.join('\n')}` : '';
    if ((el.dataset.aiSourceSignature || '') === signature) {
      return;
    }
    if (arr.length === 0) {
      if (el.innerHTML) {
        el.innerHTML = '';
      }
      if (el.classList.contains('visible')) {
        el.classList.remove('visible');
      }
      el.dataset.aiSourceSignature = '';
      return;
    }
    el.innerHTML = `<strong>${escapeHtml(title)}</strong><br/>${arr.map((s) => `• ${escapeHtml(s)}`).join('<br/>')}`;
    el.classList.add('visible');
    el.dataset.aiSourceSignature = signature;
  }

  function setInnerHtmlIfChanged(el, html) {
    if (!el) return false;
    const next = String(html || '');
    if ((el.dataset.aiRenderHtml || '') === next) {
      return false;
    }
    el.innerHTML = next;
    el.dataset.aiRenderHtml = next;
    el.dataset.aiRenderText = '';
    return true;
  }

  function setTextIfChanged(el, text) {
    if (!el) return false;
    const next = String(text || '');
    if ((el.dataset.aiRenderText || '') === next) {
      return false;
    }
    el.textContent = next;
    el.dataset.aiRenderText = next;
    el.dataset.aiRenderHtml = '';
    return true;
  }

  function setButtonState(button, text, disabled) {
    if (!button) return;
    const nextText = String(text || '');
    const nextDisabled = Boolean(disabled);
    if (button.disabled !== nextDisabled) {
      button.disabled = nextDisabled;
    }
    if (button.innerHTML !== nextText) {
      button.innerHTML = nextText;
    }
    button.dataset.aiRenderHtml = nextText;
    button.dataset.aiRenderText = '';
  }

  /** 从 Model 正文中解析「[来源: 文件名]」与「[来源:文件名]」（与后端摘要 RAG 输出格式一致） */
  function extractSourceNamesFromBracketTags(text) {
    const re = /\[\s*来源\s*:\s*([^\]]+?)\s*\]/g;
    const names = [];
    const seen = new Set();
    const s = String(text || '');
    let m;
    while ((m = re.exec(s)) !== null) {
      const n = m[1].replace(/\ufeff/g, '').trim();
      if (n && !seen.has(n)) {
        seen.add(n);
        names.push(n);
      }
    }
    return names;
  }

  /** 左栏「信息总结」下已展示的检索来源（建议接口未带 knowledge_sources 时回退展示） */
  function parseKbSourceNamesFromSummaryColumn(box) {
    const el = box && box.querySelector('.ai-summary-kb-sources');
    if (!el || !el.classList.contains('visible')) return [];
    const lines = (el.innerText || '').split(/\r?\n/);
    const out = [];
    const seen = new Set();
    for (const line of lines) {
      const idx = line.indexOf('•');
      if (idx === -1) continue;
      const n = line.slice(idx + 1).trim();
      if (n && !seen.has(n)) {
        seen.add(n);
        out.push(n);
      }
    }
    return out;
  }

  // 将纯文本格式化为可读的 HTML（分段、分点）
  function formatSummaryText(text) {
    if (!text || text === '--' || text === '待确认' || text === '无') return escapeHtml(text);
    let s = String(text).trim();

    // 先按已有的换行符拆分
    let lines = s.split(/\n+/);

    // 如果只有一行（未换行），尝试按编号模式拆分：1. 2. 3. 或 1、2、3、
    if (lines.length === 1) {
      lines = s.split(/(?=\d+[.、．]\s*)/).filter(l => l.trim());
    }

    // 如果仍只有一行，尝试按中文句号/分号拆分长段落为更短的语义段
    if (lines.length === 1 && s.length > 80) {
      // 按句号+空格、分号、句号拆分，保留分隔符
      const parts = s.split(/(?<=[。；;])\s*/).filter(l => l.trim());
      if (parts.length > 1) {
        // 将相邻短句合并，避免过度碎片化（目标每段 40-120 字）
        lines = [];
        let buf = '';
        for (const part of parts) {
          if (buf && (buf + part).length > 100) {
            lines.push(buf);
            buf = part;
          } else {
            buf += (buf ? '' : '') + part;
          }
        }
        if (buf) lines.push(buf);
      }
    }

    // 构建 HTML：编号行加粗，普通行分段
    const htmlParts = lines.map(line => {
      const trimmed = line.trim();
      if (!trimmed) return '';
      const escaped = escapeHtml(trimmed);
      // 匹配 "1." "2、" "3．" 等编号开头
      if (/^\d+[.、．]/.test(trimmed)) {
        return `<div style="margin:4px 0;padding-left:4px;"><b>${escaped.replace(/^(\d+[.、．]\s*)/, '<span style="color:#e8ddd4;">$1</span>')}</b></div>`;
      }
      return `<div style="margin:3px 0;">${escaped}</div>`;
    });

    return htmlParts.filter(Boolean).join('');
  }

  function renderState(scopeRoot, payload = {}) {
    const st = panelState(scopeRoot);
    switch (st.currentState) {
      case AI_STATES.IDLE:
        renderIdleState(scopeRoot);
        break;
      case AI_STATES.GENERATING:
        renderGeneratingState(scopeRoot, payload);
        break;
      case AI_STATES.SHOWING:
        renderSuggestionState(scopeRoot, payload);
        break;
      case AI_STATES.AGENT_REPLIED:
        renderAgentRepliedState(scopeRoot, payload);
        break;
      case AI_STATES.ERROR:
        renderErrorState(scopeRoot, payload);
        break;
      default:
        renderIdleState(scopeRoot);
    }
  }

  /** 单个 scope（工单 Tab 容器）是否已可挂载助手 */
  function isScopeDocumentReady(scopeRoot) {
    const replyContainer = scopeRoot.querySelector('.m-sheet-reply');
    const propertiesContainer = scopeRoot.querySelector('.m-sheet-propertes');
    if (replyContainer || propertiesContainer) return true;
    if (!isWorksheetDetailUrlContext()) return false;
    return Boolean(
      scopeRoot.querySelector('.m-sheet-main') ||
        scopeRoot.querySelector('.ant-layout-content') ||
        scopeRoot.querySelector('main.ant-layout-content') ||
        scopeRoot.querySelector('.g-layout-content')
    );
  }

  // 检查页面是否就绪（任一面板就绪即可）
  function isPageReady() {
    const scopes = getWorksheetPanelScopes();
    if (!scopes.length) return false;
    return scopes.some((s) => isScopeDocumentReady(s));
  }

  function hasWorksheetContextHint() {
    if (isWorksheetDetailUrlContext()) return true;
    return Boolean(
      document.querySelector('.m-sheet-item') ||
      document.querySelector('.m-sheet-reply') ||
      document.querySelector('.m-sheet-main') ||
      document.querySelector('.m-sheet-propertes')
    );
  }

  // 提取对话消息
  function extractMessages(scopeRoot = document.body) {
    const root = scopeRoot instanceof Element ? scopeRoot : document.body;
    const messages = [];
    const seenMessages = new Set(); // 用于去重
    const msgElements = root.querySelectorAll('.msg');

    // 过滤掉可能的重复元素（同时具有多个class的元素）
    const uniqueElements = Array.from(msgElements).filter(element => {
      // 跳过同时具有两个class的元素，避免重复处理
      return !element.classList.contains('m-ct-message');
    });

    uniqueElements.forEach(element => {
      // 跳过系统消息和无价值内容（减少过滤条件）
      if (element.classList.contains('msg-sys') ||
          element.classList.contains('msg-ainvalid') ||
          element.classList.contains('withdraw') ||
          element.textContent.includes('已撤回') ||
          element.textContent.includes('重新编辑') ||
          element.textContent.includes('超级管理员（不分析）')) {
        return;
      }

      // 提取发言者姓名 - 使用标准class
      const nameElement = element.querySelector('.chater-name');
      const speakerName = nameElement ? nameElement.textContent.trim() : '';

      // 判断角色：msg-right 是客服，msg-left 是客户
      const isAgent = element.classList.contains('msg-right');

      // 简化逻辑：不区分客服和客户，所有人的消息都记录

      // 提取消息内容 - 使用标准class
      const contentElement = element.querySelector('.msg-text-content') || element.querySelector('.m-cnt p');
      const text = contentElement ? contentElement.textContent.trim() : '';

      if (text.length < 2) return;

      // 提取时间信息 - 尝试多种选择器
      let timeElement = element.querySelector('.time');
      if (!timeElement) {
        // 尝试其他可能的时间选择器
        const alternativeSelectors = [
          '.msg-footer .time',      // 在消息footer中的时间
          '.msg-time',
          '.message-time',
          '.timestamp',
          '.send-time',
          '[data-time]',
          '.time-info',
          '.date-time',
          '[data-test="time"]'      // 使用data-test属性查找时间
        ];

        for (const selector of alternativeSelectors) {
          timeElement = element.querySelector(selector);
          if (timeElement) break;
        }
      }

      // 如果在当前消息元素中没找到时间，尝试在父级消息容器中查找
      if (!timeElement) {
        const parentMsg = element.closest('.msg');
        if (parentMsg) {
          timeElement = parentMsg.querySelector('.msg-footer .time, .time');
        }
      }

      const timeStr = timeElement ? timeElement.textContent.trim() : '';

      // 解析时间戳
      let timestamp = null;
      if (timeStr) {
        timestamp = parseTimeToTimestamp(timeStr);
      }

      // 如果没有找到时间戳，尝试从页面其他地方获取时间
      if (timestamp === null) {
        timestamp = getAlternativeTimestamp(element, messages.length);
      }

      // 移除调试信息显示

      // 根据 isAgent 判断角色：客服或客户
      // 如果是群聊，仍然需要区分客服和客户，不能都标记为"群聊成员"
      let roleName = isAgent ? '客服' : '客户';
      let senderName = speakerName || '';

      // 构建包含发言人信息的消息内容
      let finalContent = text;
      if (speakerName) {
        // 所有消息：在内容前加上发言人姓名
        finalContent = `[${speakerName}]: ${text}`;
      }

      // 创建消息唯一标识用于去重 - 改进版本，包含发送者信息和内容
      const messageKey = `${roleName}:${senderName}:${finalContent}:${timeStr}`;

      // 简单去重：如果消息已存在，跳过
      if (seenMessages.has(messageKey)) {
        return;
      }
      seenMessages.add(messageKey);

      // 只保留后端期望的字段
      const message = {
        role: roleName,
        content: finalContent,
        sender: senderName, // 发送者名称（可选）
        timestamp: timestamp // Unix时间戳（可选，float类型）
      };

      messages.push(message);

    });

    // 按时间戳排序（优先使用时间戳，回退到字符串比较）
    if (messages.length > 0) {
      messages.sort((a, b) => {
        // 优先使用Unix时间戳排序
        if (a.timestamp !== null && b.timestamp !== null) {
          return a.timestamp - b.timestamp;
        }
        // 如果只有一个有时间戳，有时间戳的排在后面（假设无时间戳的更早）
        if (a.timestamp !== null) return 1;
        if (b.timestamp !== null) return -1;
        // 回退到字符串时间比较
        if (a.time && b.time) {
          return a.time.localeCompare(b.time);
        }
        // 最后按原始顺序
        return 0;
      });
    }

    // 移除调试统计显示，功能静默运行

    return messages.slice(-50); // 增加到最近50条有效消息
  }

  function isWorksheetMode() {
    // 视以下任一容器存在为工单页面：
    // - `.m-sheet-main .m-sheet-rich-cont`：旧版工单富文本区域
    // - `.m-sheet-reply`：旧版回复区域
    // - `.m-sheet-propertes`：新版工单属性/信息区域（见 yemian.txt）
    // - 工单详情 URL + ant/fishd 表单（嵌入 iframe 后类名可能变化）
    if (
      document.querySelector('.m-sheet-main .m-sheet-rich-cont') ||
      document.querySelector('.m-sheet-reply') ||
      document.querySelector('.m-sheet-propertes')
    ) {
      return true;
    }
    if (isWorksheetDetailUrlContext()) {
      return Boolean(
        document.querySelector('.m-sheet-main') ||
        document.querySelector('.ant-layout-content') ||
        document.querySelector('.fishd-form-item, .ant-form-item') ||
        document.querySelector('.ql-editor')
      );
    }
    return false;
  }

  let worksheetObserver = null;
  let worksheetObserverTarget = null;
  let worksheetHeartbeatTimer = null;
  let worksheetObserverDebounceTimer = null;
  let worksheetModeStarted = false;

  function scheduleWorksheetUpdate(scopeRoot) {
    const st = panelState(scopeRoot);
    if (st.worksheetUpdateTimer) {
      clearTimeout(st.worksheetUpdateTimer);
    }
    st.worksheetUpdateTimer = setTimeout(() => {
      st.worksheetUpdateTimer = null;
      if (!isInteractiveWorksheetScope(scopeRoot)) return;
      if (!isWorksheetModeForScope(scopeRoot)) return;

      const snapshot = buildConversationSnapshot(scopeRoot);
      if (!snapshot || !snapshot.hash) return;

      if (!st.lastConversationHash) {
        st.lastConversationSnapshot = snapshot;
        st.lastConversationHash = snapshot.hash;
        return;
      }

      if (snapshot.hash === st.lastConversationHash) return;

      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;

      if (hasInFlightRequests(st)) {
        return;
      }

      if (st.currentState === AI_STATES.SHOWING && st.currentSuggestion) {
        renderSuggestionState(scopeRoot, {
          suggestion: st.currentSuggestion,
          hint: '检测到工单内容更新，可重新生成建议保持准确'
        });
        return;
      }

      const box = getOrCreateSuggestionBox(scopeRoot);
      if (!box) return;
      const suggContent = box.querySelector('.ai-suggestion-content');
      const generateBtn = box.querySelector('.ai-generate-btn');
      const acceptBtn = box.querySelector('.ai-accept-btn');
      if (suggContent) {
        setInnerHtmlIfChanged(
          suggContent,
          '<div class="ai-subtle-hint">检测到工单内容更新，可点击“重新生成”获取最新建议</div>'
        );
      }
      if (generateBtn) {
        setTextIfChanged(generateBtn, '重新生成');
        generateBtn.disabled = false;
      }
      if (acceptBtn) {
        acceptBtn.style.display = st.currentSuggestion ? 'inline-flex' : 'none';
      }
    }, 400);
  }

  function setupWorksheetObserver() {
    const target = document.body;
    if (!target) return;
    if (worksheetObserver && worksheetObserverTarget === target) {
      return;
    }
    if (worksheetObserver) {
      try {
        worksheetObserver.disconnect();
      } catch (e) {
      }
      worksheetObserver = null;
      worksheetObserverTarget = null;
    }
    worksheetObserver = new MutationObserver(() => {
      if (worksheetObserverDebounceTimer) return;
      worksheetObserverDebounceTimer = setTimeout(() => {
        worksheetObserverDebounceTimer = null;
        if (document.hidden || !hasWorksheetContextHint()) return;
        ensureWorksheetAssistantMounted();
        getWorksheetPanelScopes().forEach((scope) => scheduleWorksheetUpdate(scope));
      }, 120);
    });
    worksheetObserverTarget = target;
    worksheetObserver.observe(target, { subtree: true, childList: true });
  }

  function getCurrentWorksheetTicketIdentity(scopeRoot = document.body) {
    const domId = extractTicketIdFromFishdForm(scopeRoot);
    if (domId) {
      return { value: String(domId), source: 'dom' };
    }
    const titleEl = scopeRoot.querySelector('.m-sheet-main .sheet-title .fishd-ellipsis-ellipsis');
    const titleInput = scopeRoot.querySelector('.m-sheet-main .sheet-title input.fishd-input');
    const title = extractNodeText(titleEl) || (titleInput && titleInput.value ? titleInput.value.trim() : '');
    if (title) {
      return { value: `title:${title}`, source: 'title' };
    }
    try {
      const params = new URLSearchParams(window.location.search);
      const id = params.get('id');
      const scopeCount = getWorksheetPanelScopes().length;
      if (scopeCount <= 1 && id && id !== 'undefined' && id !== 'null') {
        return { value: String(id), source: 'url' };
      }
    } catch (e) {
    }
    return { value: '', source: '' };
  }

  function getCurrentWorksheetTicketId(scopeRoot = document.body) {
    return getCurrentWorksheetTicketIdentity(scopeRoot).value;
  }

  function getScopeTicketIdentityValue(scopeRoot) {
    const ticketIdentity = getCurrentWorksheetTicketIdentity(scopeRoot);
    return ticketIdentity && ticketIdentity.value
      ? String(ticketIdentity.value)
      : '';
  }

  function isSameTicketContext(scopeRoot, requestTicketId) {
    const expected = String(requestTicketId || '').trim();
    if (!expected) return true;
    const current = getScopeTicketIdentityValue(scopeRoot) || panelState(scopeRoot).lastWorksheetTicketId || '';
    return !current || current === expected;
  }

  function hasInFlightRequests(st) {
    return Boolean(
      st &&
      (st.suggestionRequestToken || st.queryRequestToken || st.summaryRequestToken)
    );
  }

  function commitTicketIdentity(scopeRoot, ticketIdentity) {
    const st = panelState(scopeRoot);
    const ticketId = ticketIdentity && ticketIdentity.value ? String(ticketIdentity.value) : '';
    st.lastWorksheetTicketId = ticketId || null;
    st.lastWorksheetTicketSource = ticketIdentity && ticketIdentity.source ? ticketIdentity.source : '';
    st.pendingWorksheetTicketId = null;
    st.pendingWorksheetTicketCount = 0;
  }

  function detectConfirmedTicketSwitch(scopeRoot, ticketIdentity) {
    const st = panelState(scopeRoot);
    const nextTicketId = ticketIdentity && ticketIdentity.value ? String(ticketIdentity.value) : '';
    if (!nextTicketId) {
      st.pendingWorksheetTicketId = null;
      st.pendingWorksheetTicketCount = 0;
      return false;
    }
    if (!st.lastWorksheetTicketId) {
      commitTicketIdentity(scopeRoot, ticketIdentity);
      return false;
    }
    if (nextTicketId === st.lastWorksheetTicketId) {
      st.pendingWorksheetTicketId = null;
      st.pendingWorksheetTicketCount = 0;
      return false;
    }
    if (st.pendingWorksheetTicketId === nextTicketId) {
      st.pendingWorksheetTicketCount += 1;
    } else {
      st.pendingWorksheetTicketId = nextTicketId;
      st.pendingWorksheetTicketCount = 1;
    }
    return st.pendingWorksheetTicketCount >= 2;
  }

  function resetWorksheetRuntimeState(scopeRoot) {
    const st = panelState(scopeRoot);
    st.currentSuggestion = null;
    st.currentKnowledgeSources = [];
    st.suggestionRequestToken = null;
    st.queryRequestToken = null;
    st.summaryRequestToken = null;
    st.lastConversationHash = '';
    st.lastConversationSnapshot = null;
    st.lastSuggestionHash = '';
    st.isManualRegenerate = false;
    st.queryStatus = 'idle';
    st.queryError = '';
    st.lastQueryAnswer = '';
    st.lastQueryHtml = '';
    st.lastQuerySources = [];
    st.summaryStatus = 'idle';
    st.summaryError = '';
    st.lastSummaryInfoHtml = '--';
    st.lastSummaryReviewHtml = '--';
    st.lastSummarySources = [];
    st.pendingWorksheetTicketId = null;
    st.pendingWorksheetTicketCount = 0;
    transitionTo(scopeRoot, AI_STATES.IDLE);
  }

  function recoverScopeAfterInterruptedRequest(scopeRoot, reason = '') {
    const st = panelState(scopeRoot);
    if (st.currentState !== AI_STATES.GENERATING) return;
    if (st.currentSuggestion) {
      log(`请求中断后恢复已有建议${reason ? `: ${reason}` : ''}`, 'warn');
      transitionTo(scopeRoot, AI_STATES.SHOWING, {
        suggestion: st.currentSuggestion,
        knowledgeSources: st.currentKnowledgeSources
      });
      return;
    }
    log(`请求中断后恢复空闲态${reason ? `: ${reason}` : ''}`, 'warn');
    transitionTo(scopeRoot, AI_STATES.IDLE);
  }

  function recoverQueryPaneAfterInterruptedRequest(scopeRoot, reason = '') {
    const st = panelState(scopeRoot);
    if (st.queryStatus !== 'loading') return;
    st.queryStatus = st.lastQueryHtml ? 'showing' : 'idle';
    st.queryError = '';
    log(`查询请求中断后恢复${reason ? `: ${reason}` : ''}`, 'warn');
    syncAuxiliaryPanels(scopeRoot);
  }

  function recoverSummaryPaneAfterInterruptedRequest(scopeRoot, reason = '') {
    const st = panelState(scopeRoot);
    if (st.summaryStatus !== 'loading') return;
    const hasSummary =
      (st.lastSummaryInfoHtml && st.lastSummaryInfoHtml !== '--') ||
      (st.lastSummaryReviewHtml && st.lastSummaryReviewHtml !== '--');
    st.summaryStatus = hasSummary ? 'showing' : 'idle';
    st.summaryError = '';
    log(`总结请求中断后恢复${reason ? `: ${reason}` : ''}`, 'warn');
    syncAuxiliaryPanels(scopeRoot);
  }

  function ensureWorksheetAssistantMounted() {
    setupWorksheetObserver();
    getWorksheetPanelScopes().forEach((scopeRoot) => {
      if (!isWorksheetModeForScope(scopeRoot)) return;
      const st = panelState(scopeRoot);
      const hadBox = Boolean(scopeRoot.querySelector('.ai-suggestion-box'));
      const ticketIdentity = getCurrentWorksheetTicketIdentity(scopeRoot);
      const ticketId = ticketIdentity.value;
      const confirmedTicketSwitch = detectConfirmedTicketSwitch(scopeRoot, ticketIdentity);
      if (ticketId && confirmedTicketSwitch) {
        if (hasInFlightRequests(st)) {
          log(
            `检测到工单标识变化，当前生成中，暂不重置。old=${st.lastWorksheetTicketId} new=${ticketId}`,
            'warn'
          );
        } else {
          log(
            `工单标识变化，重置面板状态。old=${st.lastWorksheetTicketId} new=${ticketId}`,
            'warn'
          );
          resetWorksheetRuntimeState(scopeRoot);
          commitTicketIdentity(scopeRoot, ticketIdentity);
        }
      } else if (ticketId && !st.lastWorksheetTicketId) {
        commitTicketIdentity(scopeRoot, ticketIdentity);
      } else if (ticketId && ticketId === st.lastWorksheetTicketId) {
        commitTicketIdentity(scopeRoot, ticketIdentity);
      }
      getOrCreateSuggestionBox(scopeRoot);
      if (!hadBox) {
        renderState(scopeRoot);
        syncAuxiliaryPanels(scopeRoot);
        const result = shouldGenerateSuggestion(scopeRoot);
        Promise.resolve(handleSuggestionDecision(result, { scopeRoot })).catch((err) => {
          log(`面板初始化失败: ${err && err.message ? err.message : err}`, 'error');
        });
      }
    });
  }

  function extractNodeText(el) {
    if (!el) return '';
    const clone = el.cloneNode(true);
    const removable = clone.querySelectorAll ? clone.querySelectorAll('style,script') : [];
    if (removable && removable.length) {
      removable.forEach(n => n.remove());
    }
    const text = (clone.innerText || clone.textContent || '').trim();
    return text;
  }

  function normalizeText(text) {
    return String(text || '')
      .replace(/\r\n/g, '\n')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function clipText(text, maxLen = 2000) {
    const value = String(text || '');
    if (value.length <= maxLen) return value;
    return `${value.slice(0, maxLen)}…`;
  }

  function normalizeFieldKey(key) {
    return String(key || '').replace(/[：:\s]/g, '').toLowerCase();
  }

  function readRichText(el) {
    if (!el) return '';
    const clone = el.cloneNode(true);
    const removable = clone.querySelectorAll ? clone.querySelectorAll('style,script') : [];
    if (removable && removable.length) {
      removable.forEach(n => n.remove());
    }
    const listItems = clone.querySelectorAll ? clone.querySelectorAll('li') : [];
    if (listItems && listItems.length) {
      listItems.forEach(li => {
        const raw = normalizeText(li.textContent || '');
        li.textContent = raw ? `• ${raw}` : '';
      });
    }
    const text = normalizeText(clone.innerText || clone.textContent || '');
    return text;
  }

  function collectTicketFieldMap(scopeRoot = document.body) {
    const fieldMap = {};
    const dls = scopeRoot.querySelectorAll('.m-sheet-main dl');
    dls.forEach(dl => {
      const dt = dl.querySelector('dt');
      const dd = dl.querySelector('dd');
      const keyRaw = normalizeText((dt && (dt.innerText || dt.textContent)) || '');
      const valueRaw = normalizeText((dd && (dd.innerText || dd.textContent)) || '');
      if (!keyRaw || !valueRaw) return;
      const key = normalizeFieldKey(keyRaw);
      if (!key) return;
      if (!fieldMap[key]) {
        fieldMap[key] = clipText(valueRaw, 240);
      }
    });
    return fieldMap;
  }

  function pickFieldValue(fieldMap, candidates = []) {
    for (const key of candidates) {
      const normalized = normalizeFieldKey(key);
      if (fieldMap[normalized]) {
        return fieldMap[normalized];
      }
    }
    const entries = Object.entries(fieldMap);
    for (const [k, v] of entries) {
      if (candidates.some(candidate => k.includes(normalizeFieldKey(candidate)))) {
        return v;
      }
    }
    return '';
  }

  function pickValueFromText(mainText, labels = []) {
    const source = normalizeText(mainText);
    if (!source) return '';
    for (const label of labels) {
      const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const reg = new RegExp(`${escaped}\\s*[：:]\\s*([^\\n\\r]{1,120})`, 'i');
      const match = source.match(reg);
      if (match && match[1]) {
        return clipText(normalizeText(match[1]), 120);
      }
    }
    return '';
  }

  function pickValueByLabelsFromRoot(root, labels = []) {
    if (!root || !labels || labels.length === 0) return '';
    const normalizedLabels = labels.map(label => normalizeFieldKey(label)).filter(Boolean);
    if (normalizedLabels.length === 0) return '';
    
    // 策略1：直接在 label[title] 中查找，然后从同一行的 input/textarea 获取值
    for (const labelKey of normalizedLabels) {
      const labelElements = root.querySelectorAll('label[title]');
      for (const labelEl of labelElements) {
        const labelTitle = normalizeFieldKey(labelEl.getAttribute('title') || '');
        if (labelTitle === labelKey || labelTitle.includes(labelKey)) {
          // 找到匹配的 label，现在从同一行的 .fishd-form-item-control 中获取值
          const row = labelEl.closest('.fishd-row, .fishd-form-item');
          if (!row) continue;
          
          // 在这一行中查找 input 或 textarea
          const input = row.querySelector('input.fishd-input');
          const textarea = row.querySelector('textarea.ant-input');
          
          if (input && input.value) {
            return clipText(normalizeText(input.value), 240);
          }
          if (textarea && textarea.value) {
            return clipText(normalizeText(textarea.value), 240);
          }
          
          // 如果没有 input/textarea，尝试从 .value-show 或其他文本节点获取
          const valueShow = row.querySelector('.value-show');
          if (valueShow) {
            const text = normalizeText(valueShow.innerText || valueShow.textContent || '');
            if (text) return clipText(text, 240);
          }
        }
      }
    }
    
    // 策略2：回退到原有逻辑（兼容其他 DOM 结构）
    const nodes = root.querySelectorAll('dt,th,label,span,div,strong,b');
    for (const node of nodes) {
      const nodeText = normalizeText(node.innerText || node.textContent || '');
      if (!nodeText || nodeText.length > 20) continue;
      const nodeKey = normalizeFieldKey(nodeText);
      if (!nodeKey) continue;
      const matched = normalizedLabels.some(labelKey => nodeKey === labelKey || nodeKey.includes(labelKey));
      if (!matched) continue;

      let row =
        node.closest('.fishd-row, .fishd-form-item, .ant-form-item, dl, tr, li, .m-item, .item, .row') ||
        node.closest('div');
      if (!row) continue;

      if (row.classList.contains('fishd-form-item-label')) {
        const parentRow = row.closest('.fishd-row, .fishd-form-item, .ant-form-item');
        if (parentRow) {
          row = parentRow;
        }
      }

      const candidates = row.querySelectorAll('dd,td,.value,.content,.txt,.text,span,div,a,p,input,textarea');
      let best = '';
      candidates.forEach(el => {
        if (el === node) return;

        let raw = '';
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
          raw = el.value || el.getAttribute('value') || '';
        } else {
          raw = el.innerText || el.textContent || '';
        }

        const value = clipText(normalizeText(raw), 240);
        if (!value) return;
        const valueKey = normalizeFieldKey(value);
        if (!valueKey || valueKey === nodeKey) return;
        if (value.length > best.length) {
          best = value;
        }
      });
      if (best) {
        return best;
      }
    }
    return '';
  }

  // 按固定字段 id 读取输入/文本域的值（你提供的工单页面字段）
  function pickValueByFieldId(root, fieldId) {
    if (!fieldId) return '';
    const selectors = [
      `#${fieldId}`,
      `input#${fieldId}`,
      `textarea#${fieldId}`
    ];
    for (const selector of selectors) {
      const el = (root && root.querySelector && root.querySelector(selector)) || document.querySelector(selector);
      if (!el) continue;
      const raw =
        (typeof el.value === 'string' && el.value) ||
        el.getAttribute('value') ||
        el.getAttribute('title') ||
        el.textContent ||
        '';
      const text = clipText(normalizeText(raw), 240);
      if (text) return text;
    }
    return '';
  }

  // 从新版表单 DOM 中提取优先级（你提供的 priority-select 结构）
  function pickPriorityFromDom(root) {
    const scope = root || document;
    const direct = scope.querySelector('.fishd-select.priority-select .fishd-select-option-single') ||
      document.querySelector('.fishd-select.priority-select .fishd-select-option-single');
    if (direct) {
      const value = clipText(normalizeText(direct.textContent || ''), 80);
      if (value) return value;
    }
    return '';
  }

  // 从新版表单 DOM 中提取工单状态（优先按状态标签行，其次按状态词兜底）
  function pickStatusFromDom(root) {
    const scopes = [root, document].filter((s, idx, arr) => s && arr.indexOf(s) === idx);
    const statusWords = ['受理中', '处理中', '待处理', '待审核', '已完结', '已完成', '已关闭', '已驳回', '挂起', '回复中'];

    for (const scope of scopes) {
      // 1) 强匹配：按“状态”所在行读取同一行 value-show（与你提供 DOM 结构一致）
      const rows = scope.querySelectorAll('.fishd-row.fishd-form-item, .fishd-form-item, .ant-form-item');
      for (const row of rows) {
        const labelEl = row.querySelector('.fishd-form-item-label, label[title], .ant-form-item-label');
        const labelText = normalizeFieldKey(
          (labelEl && (labelEl.getAttribute && labelEl.getAttribute('title'))) ||
            (labelEl && (labelEl.innerText || labelEl.textContent)) ||
            ''
        );
        if (!(labelText.includes('工单状态') || labelText === '状态' || labelText.includes('处理状态') || labelText.includes('当前状态'))) {
          continue;
        }
        const valueEl = row.querySelector('.fishd-form-item-control .value-show, .fishd-form-item-control-wrapper .value-show, .value-show, input.fishd-input');
        if (!valueEl) continue;
        const raw = (typeof valueEl.value === 'string' && valueEl.value) || valueEl.textContent || '';
        const value = clipText(normalizeText(raw), 80);
        if (value) return value;
      }

      // 2) 兜底：扫描 value-show，按状态词命中
      const candidates = scope.querySelectorAll('.fishd-form-item-control .value-show, .fishd-form-item-control-wrapper .value-show, .value-show');
      for (const el of candidates) {
        const value = clipText(normalizeText(el.textContent || ''), 80);
        if (!value) continue;
        if (statusWords.some(word => value.includes(word))) return value;
      }
    }
    return '';
  }

  function isVisibleForHistory(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    return true;
  }

  function extractHistorySummary(maxItems = 5, scopeRoot = document.body) {
    const selectors = [
      '.m-detail-content-wrapper [class*="record"] [class*="item"]',
      '.m-detail-content-wrapper [class*="record"] li',
      '.m-detail-content-wrapper [class*="log"] [class*="item"]',
      '.m-detail-content-wrapper [class*="history"] [class*="item"]',
      '.m-detail-content-wrapper .m-ct-message .msg',
      '.m-sheet-main [class*="record"] [class*="item"]',
      '.m-sheet-main [class*="record"] li',
      '.m-sheet-main [class*="log"] [class*="item"]',
      '.m-sheet-main [class*="history"] [class*="item"]',
      '.m-sheet-main [class*="timeline"] [class*="item"]',
      '.m-sheet-main [class*="timeline"] li',
      '.m-sheet-main .m-ct-message .msg'
    ];
    const lineIgnoreRegs = [
      /全部记录|回复记录|流转记录|催单记录|操作记录|操作日志/,
      /AI建议助手|手动生成建议|下载全部附件/,
      /^回复$|^采购$|^隐藏$|^取消$/,
      /工单系统|员工中心|服务中心/
    ];
    const headerReg = /(今天|昨天|\d{1,2}:\d{2}|\d{4}[-/]\d{1,2}[-/]\d{1,2})/;
    const actionReg = /(回复|回访|备注|流转|催单|挂起|转交|完结|操作|日志|处理|跟进|补发|退款|退货|换货|发货|催发|物流|查询|更新|关闭|完成|创建|指派|变更|状态)/;

    const seen = new Set();
    const items = [];
    
    const nodes = scopeRoot.querySelectorAll(selectors.join(','));
    nodes.forEach(node => {
      // 增加工单ID隔离判断
      const ticketItem = node.closest('[data-id], [id^="ticket-"], .m-sheet-main');
      if (ticketItem) {
         // 尝试从最近的容器获取工单ID线索，如果明确属于其他工单则跳过
         // 注意：DOM结构可能没有明确的data-id绑定，这里更多是防御性检查
         // 更好的方式是检查是否在当前激活的tab或view中
         const parentTab = node.closest('.m-sheet-item, .m-detail-content-wrapper');
         if (parentTab && parentTab.style.display === 'none') return;
      }

      if (!isVisibleForHistory(node)) return;
      const lines = normalizeText(node.innerText || node.textContent || '')
        .split('\n')
        .map(line => normalizeText(line))
        .filter(Boolean)
        .filter(line => line.length >= 2 && line.length <= 140)
        .filter(line => !lineIgnoreRegs.some(reg => reg.test(line)));
      if (lines.length === 0) return;

      let header = '';
      let headerIndex = -1;
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (headerReg.test(line) && actionReg.test(line)) {
          header = line;
          headerIndex = i;
          break;
        }
      }
      if (!header) return;

      let content = '';
      for (let i = headerIndex + 1; i < lines.length; i++) {
        const line = lines[i];
        if (headerReg.test(line) && actionReg.test(line)) continue;
        if (line.length < 4) continue;
        if (/^回复了工单$|^回访备注$|^操作日志$/.test(line)) continue;
        content = line;
        break;
      }

      const summary = content
        ? `${clipText(header, 80)}  回复内容：${clipText(content, 140)}`
        : clipText(header, 120);
      if (seen.has(summary)) return;
      seen.add(summary);
      items.push(summary);
    });

    if (items.length < maxItems) {
      const fallbackSeen = new Set(items);
      const fallbackNodes = scopeRoot.querySelectorAll([
        '.m-detail-content-wrapper',
        '.m-sheet-main',
        '.m-detail'
      ].join(','));
      fallbackNodes.forEach(root => {
        // 增加工单ID隔离判断
        const ticketItem = root.closest('[data-id], [id^="ticket-"]');
        if (ticketItem) {
           const parentTab = root.closest('.m-sheet-item, .m-detail-content-wrapper');
           if (parentTab && parentTab.style.display === 'none') return;
        }
        if (!root || !root.isConnected) return;
        
        // 排除侧边栏、工单详情表单区域，仅关注可能包含历史记录的区域
        // 避免抓取到 "工单模板" 等表单字段文本
        const text = normalizeText(root.innerText || root.textContent || '');
        if (!text) return;
        
        const lines = text
          .split('\n')
          .map(line => normalizeText(line))
          .filter(Boolean)
          .filter(line => line.length >= 2 && line.length <= 140)
          .filter(line => !lineIgnoreRegs.some(reg => reg.test(line)));
          
        for (let i = 0; i < lines.length; i++) {
          const line = lines[i];
          if (!headerReg.test(line)) continue;
          
          // 兜底提取时，header行必须包含"回复"等动作词，避免抓取到"工单内容: 359:11:48"这种纯时间或异常文本
          if (!actionReg.test(line)) continue;
          
          const header = line;
          let content = '';
          for (let j = i + 1; j < Math.min(lines.length, i + 6); j++) {
            const candidate = lines[j];
            if (headerReg.test(candidate)) break;
            if (candidate.length < 4) continue;
            // 增强过滤：排除 "工单模板" 等常见表单标签干扰
            if (/^回复$|^回访备注$|^操作日志$|^工单模板$|^工单分类$|^工单状态$/.test(candidate)) continue;
            content = candidate;
            break;
          }
          if (!content) continue; // 如果没提取到内容，也不要这个header
          
          const summary = `${clipText(header, 80)}  回复内容：${clipText(content, 140)}`;
          if (fallbackSeen.has(summary)) continue;
          fallbackSeen.add(summary);
          items.push(summary);
          if (items.length >= maxItems * 3) break;
        }
      });
    }

    if (items.length === 0) {
      return { history_items: [], history_summary: '' };
    }

    const lastItems = items.slice(-maxItems);
    const historySummary = lastItems.map((item, index) => `${index + 1}. ${item}`).join('\n');
    return {
      history_items: lastItems,
      history_summary: clipText(historySummary, 900)
    };
  }

  function debugTicketExtraction(ticketData) {
    if (!ticketData) return;
    const payload = {
      ticket_id: ticketData.ticket_id || null,
      ticket_no: ticketData.ticket_no || null,
      title: ticketData.title || '',
      desc_preview: clipText(ticketData.desc || '', 200),
      category: ticketData.category || null,
      sub_category: ticketData.sub_category || null,
      priority: ticketData.priority || null,
      status: ticketData.status || null,
      history_count: Array.isArray(ticketData.history_items) ? ticketData.history_items.length : 0
    };
    console.log(payload);
    if (ticketData.history_items && ticketData.history_items.length > 0) {
      console.table(ticketData.history_items.map((item, i) => ({ index: i + 1, summary: item })));
    }
    console.groupEnd();
  }

  /**
   * 从工单属性区抓取工单 ID（新版 fishd 表单：.fishd-form-item-control → .value-show）
   */
  function extractTicketIdFromFishdForm(scopeRoot = document.body) {
    const base = scopeRoot instanceof Element ? scopeRoot : document.body;
    const scope =
      base.querySelector('.m-sheet-propertes') ||
      base.querySelector('.m-detail-content-wrapper') ||
      base;
    const labelMatches = (text) => {
      const t = String(text || '').trim();
      if (!t) return false;
      return (
        /工单\s*(?:ID|id|编号|单号)/i.test(t) ||
        /^工单编号$/i.test(t) ||
        /^工单号$/i.test(t) ||
        /^ID$/i.test(t)
      );
    };
    const labels = scope.querySelectorAll(
      'label[title], .fishd-form-item-label label, label.fishd-form-item-required'
    );
    for (let i = 0; i < labels.length; i++) {
      const lab = labels[i];
      const labelText = (lab.getAttribute('title') || lab.innerText || lab.textContent || '').trim();
      if (!labelMatches(labelText)) continue;
      const row = lab.closest('.fishd-row') || lab.closest('.fishd-form-item');
      if (!row) continue;
      const valueEl = row.querySelector(
        '.fishd-form-item-control .fishd-form-item-children span.value-show, .fishd-form-item-control span.value-show'
      );
      if (valueEl) {
        const v = (valueEl.textContent || '').trim();
        if (v) return v;
      }
    }
    const valueShows = scope.querySelectorAll(
      '.fishd-form-item-control .fishd-form-item-children span.value-show'
    );
    for (let j = 0; j < valueShows.length; j++) {
      const v = (valueShows[j].textContent || '').trim();
      if (/^\d{5,24}$/.test(v)) return v;
    }
    return '';
  }

  function extractTicketData(scopeRoot = document.body) {
    const root = scopeRoot instanceof Element ? scopeRoot : document.body;
    const ticketIdFromQuery = new URLSearchParams(window.location.search).get('id');
    // 适配新版 DOM，将 `.m-sheet-propertes` 视为主要工单容器
    const mainRoot =
      root.querySelector('.m-detail-content-wrapper') ||
      root.querySelector('.m-detail') ||
      root.querySelector('.m-sheet-main') ||
      root.querySelector('.m-sheet-propertes') ||
      root;
    const sheetRoot =
      root.querySelector('.m-sheet-main') ||
      root.querySelector('.m-sheet-propertes') ||
      mainRoot;
    const titleEl = root.querySelector('.m-sheet-main .sheet-title .fishd-ellipsis-ellipsis');
    const titleInput = root.querySelector('.m-sheet-main .sheet-title input.fishd-input');
    const title = extractNodeText(titleEl) || (titleInput && titleInput.value ? titleInput.value.trim() : '');
    const descNode =
      root.querySelector('.m-sheet-main .sheet-cont .m-sheet-rich-cont') ||
      // 新版页面中，工单描述通常位于富文本/多行输入中，这里兜底从属性区域内查找
      (sheetRoot && sheetRoot.querySelector('.m-sheet-rich-cont, textarea, .native-scrollbar'));
    const desc = clipText(readRichText(descNode), 3000);
    const fieldMap = collectTicketFieldMap(root);
    const mainText = extractNodeText(sheetRoot);

    const ticketNo =
      pickFieldValue(fieldMap, ['工单号', '工单编号', '流水号', 'ticket_no', 'ticket id']) ||
      pickValueByLabelsFromRoot(mainRoot, ['工单号', '工单编号', '流水号', 'ticket_no', 'ticket id']) ||
      pickValueFromText(mainText, ['工单号', '工单编号', '流水号']);
    const category =
      pickFieldValue(fieldMap, ['工单分类', '分类', '问题分类', '工单类型', '类型']) ||
      pickValueByLabelsFromRoot(mainRoot, ['工单分类', '分类', '问题分类', '工单类型', '类型']) ||
      pickValueFromText(mainText, ['工单分类', '分类', '问题分类', '工单类型']);
    const subCategory =
      pickFieldValue(fieldMap, ['子类目', '子分类', '二级分类', '三级分类']) ||
      pickValueByLabelsFromRoot(mainRoot, ['子类目', '子分类', '二级分类', '三级分类']) ||
      pickValueFromText(mainText, ['子类目', '子分类', '二级分类', '三级分类']);
    const priority =
      pickPriorityFromDom(root) ||
      pickFieldValue(fieldMap, ['优先级', '紧急程度', '严重程度', '优先等级']) ||
      pickValueByLabelsFromRoot(mainRoot, ['优先级', '紧急程度', '严重程度', '优先等级']) ||
      pickValueFromText(mainText, ['优先级', '紧急程度', '严重程度', '优先等级']);
    const status =
      pickStatusFromDom(root) ||
      pickFieldValue(fieldMap, ['工单状态', '状态', '处理状态', '当前状态']) ||
      pickValueByLabelsFromRoot(mainRoot, ['工单状态', '状态', '处理状态', '当前状态']) ||
      pickValueFromText(mainText, ['工单状态', '状态', '处理状态', '当前状态']);
    
    // 直接从 .m-sheet-propertes 容器中提取核心字段（最可靠的方式）
    const propertiesContainer = root.querySelector('.m-sheet-propertes');
    
    // Core Info Extraction - 优先从 .m-sheet-propertes 中直接查找
    const customerName =
      pickValueByFieldId(root, 'field_5827173') ||
      (propertiesContainer && propertiesContainer.querySelector('label[title*="客户名称"]')?.closest('.fishd-row')?.querySelector('input.fishd-input')?.value) ||
      pickFieldValue(fieldMap, ['客户名称', '客户姓名', '客户']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['客户名称', '客户姓名', '客户']) || '';
      
    const projectName =
      pickValueByFieldId(root, 'field_5827630') ||
      (propertiesContainer && propertiesContainer.querySelector('label[title*="项目名称"]')?.closest('.fishd-row')?.querySelector('input.fishd-input')?.value) ||
      pickFieldValue(fieldMap, ['项目名称', '所属项目', '项目']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['项目名称', '所属项目', '项目']) || '';
      
    const mallName =
      pickValueByFieldId(root, 'field_5821564') ||
      (propertiesContainer && propertiesContainer.querySelector('label[title*="商城名称"]')?.closest('.fishd-row')?.querySelector('input.fishd-input')?.value) ||
      pickFieldValue(fieldMap, ['商城名称', '店铺名称', '来源商城']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['商城名称', '店铺名称', '来源商城']) || '';

    // Attention Info Extraction - 优先从 .m-sheet-propertes 中直接查找
    const projectAttention =
      pickValueByFieldId(root, 'field_4678642') ||
      (propertiesContainer && propertiesContainer.querySelector('label[title*="项目注意事项"]')?.closest('.fishd-row')?.querySelector('textarea.ant-input')?.value) ||
      pickFieldValue(fieldMap, ['项目注意事项', '项目备注']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['项目注意事项', '项目备注']) || '';

    const supplierAttention = 
      (propertiesContainer && propertiesContainer.querySelector('label[title*="供应商注意事项"]')?.closest('.fishd-row')?.querySelector('textarea.ant-input')?.value) ||
      pickFieldValue(fieldMap, ['供应商注意事项', '供应商备注']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['供应商注意事项', '供应商备注']) || '';

    const historyData = extractHistorySummary(5, root);

    const parts = [];
    // 移除已删除字段的文本拼接逻辑
    if (title) parts.push(`工单标题：${title}`);
    if (desc) parts.push(`工单描述：${desc}`);
    if (priority) parts.push(`优先级：${priority}`);
    if (status) parts.push(`当前状态：${status}`);
    if (customerName) parts.push(`客户名称：${customerName}`);
    if (projectName) parts.push(`项目名称：${projectName}`);
    if (mallName) parts.push(`商城名称：${mallName}`);
    if (projectAttention) parts.push(`项目注意事项：${projectAttention}`);
    if (supplierAttention) parts.push(`供应商注意事项：${supplierAttention}`);

    const ticketIdFromDom = extractTicketIdFromFishdForm(root);
    const ticket_id = (
      (ticketIdFromDom && String(ticketIdFromDom).trim()) ||
      (ticketNo && String(ticketNo).trim()) ||
      (ticketIdFromQuery && String(ticketIdFromQuery).trim()) ||
      ''
    );

    return {
      ticket_id,
      title,
      desc,
      priority: priority || null,
      status: status || null,
      history_items: historyData.history_items,
      core_info: {
        customer_name: customerName,
        project_name: projectName,
        mall_name: mallName
      },
      attention_info: {
        project_attention: projectAttention,
        supplier_attention: supplierAttention
      }
    };
  }

  function extractWorksheetMessages(scopeRoot = document.body) {
    const ticket = extractTicketData(scopeRoot);
    
    // 只保留标题和描述作为主要内容，避免 query 过长且包含重复信息
    const parts = [];
    if (ticket.title) parts.push(`工单标题：${ticket.title}`);
    if (ticket.desc) parts.push(`工单描述：${ticket.desc}`);
    
    const content = parts.join('\n');
    
    if (content.length < 2) return [];

    return [{
      role: '客户',
      content: content,
      sender: '工单',
      timestamp: Math.floor(Date.now() / 1000)
    }];
  }

  function buildConversationSnapshot(scopeRoot = document.body) {
    const root = scopeRoot instanceof Element ? scopeRoot : document.body;
    let messages = [];
    const worksheetMessages = extractWorksheetMessages(root);
    if (worksheetMessages && worksheetMessages.length > 0) {
      messages = worksheetMessages;
    } else {
      messages = extractMessages(root);
    }
    const hash = messages.map(msg => `${msg.role}:${msg.content}`).join('|');
    const lastMessage = messages.length > 0 ? messages[messages.length - 1] : null;
    let lastAgentMessage = '';
    let lastCustomerMessage = '';

    // 定义客服关键词
    const agentKeywords = ['客服', '售后', '顾问', '助理', '小助手', '机器人'];

    // 找到最后一条客服消息和最后一条客户消息
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      let isAgentMessage = false;

      // 检查role字段
      if (msg.role === '客服' || msg.role === 'agent') {
        isAgentMessage = true;
      }

      // 检查sender字段
      if (msg.sender && agentKeywords.some(keyword => msg.sender.includes(keyword))) {
        isAgentMessage = true;
      }

      // 检查content中的客服标识
      if (agentKeywords.some(keyword => msg.content.toLowerCase().includes(keyword))) {
        // 进一步检查是否为客服身份标识
        const agentPatterns = ['[客服]', '[售后]', '[顾问]', '客服:', '售后:'];
        if (agentPatterns.some(pattern => msg.content.includes(pattern))) {
          isAgentMessage = true;
        }
      }

      // 记录最后一条客服消息
      if (isAgentMessage && !lastAgentMessage) {
        lastAgentMessage = msg.content;
      }

      // 记录最后一条客户消息（非客服）
      if (!isAgentMessage && !lastCustomerMessage) {
        lastCustomerMessage = msg.content;
      }

      // 如果都已找到，提前结束循环
      if (lastAgentMessage && lastCustomerMessage) {
        break;
      }
    }

    return {
      messages,
      hash,
      lastMessage,
      lastAgentMessage,
      lastCustomerMessage
    };
  }

  // 填充建议到输入框
  function fillSuggestion(text, scopeRoot = null, options = {}) {
    const scope = scopeRoot || getDefaultScope();
    function isVisibleElement(el) {
      if (!el) return false;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
      const rects = el.getClientRects ? el.getClientRects() : null;
      if (!rects || rects.length === 0) return false;
      const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (!rect) return false;
      if (rect.width <= 1 || rect.height <= 1) return false;
      return true;
    }

    function setContentEditablePlainText(el, plainText) {
      if (!el) return false;
      el.focus();

      const lines = String(plainText || '').replace(/\r\n/g, '\n').split('\n');
      el.innerHTML = '';
      for (const line of lines) {
        const p = document.createElement('p');
        if (line) {
          p.textContent = line;
        } else {
          p.appendChild(document.createElement('br'));
        }
        el.appendChild(p);
      }

      try {
        const selection = window.getSelection();
        if (selection) {
          selection.removeAllRanges();
          const range = document.createRange();
          range.selectNodeContents(el);
          range.collapse(false);
          selection.addRange(range);
        }
      } catch (e) {
      }

      try {
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: plainText }));
      } catch (e) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ' ' }));
      return true;
    }

    const replyContainer = getReplyEditorRoot(scope);
    const snippet = String(text || '').trim().slice(0, 20);

    function elementArea(el) {
      try {
        const rect = el.getBoundingClientRect();
        return Math.max(0, rect.width) * Math.max(0, rect.height);
      } catch (e) {
        return 0;
      }
    }

    function setNativeValue(el, value) {
      const v = String(value ?? '');
      const proto = el && el.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
      if (descriptor && typeof descriptor.set === 'function') {
        descriptor.set.call(el, v);
      } else {
        el.value = v;
      }
    }

    function dispatchTextInputEvents(el, value) {
      try {
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: String(value ?? '') }));
      } catch (e) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ' ' }));
    }

    let wroteToEditor = false;

    if (replyContainer) {
      const qleFirst =
        replyContainer.querySelector('.ql-editor[contenteditable="true"]') ||
        scope.querySelector('.m-sheet-reply .ql-editor[contenteditable="true"]');
      if (qleFirst && isVisibleElement(qleFirst)) {
        wroteToEditor = setContentEditablePlainText(qleFirst, text);
        if (wroteToEditor && snippet) {
          const after = (qleFirst.innerText || qleFirst.textContent || '').trim();
          if (!after.includes(snippet)) wroteToEditor = false;
        }
      }
    }

    const textControls = replyContainer
      ? Array.from(replyContainer.querySelectorAll('textarea, input'))
      : [];

    const visibleTextControls = textControls
      .filter(el => el && !el.disabled && !el.readOnly && isVisibleElement(el))
      .sort((a, b) => {
        const aIsTa = a.tagName === 'TEXTAREA' ? 1 : 0;
        const bIsTa = b.tagName === 'TEXTAREA' ? 1 : 0;
        if (aIsTa !== bIsTa) return bIsTa - aIsTa;
        return elementArea(b) - elementArea(a);
      });

    if (!wroteToEditor) {
      for (const el of visibleTextControls) {
        el.focus();
        setNativeValue(el, text);
        dispatchTextInputEvents(el, text);
        if (!snippet || String(el.value || '').includes(snippet)) {
          wroteToEditor = true;
          break;
        }
      }
    }

    if (!wroteToEditor) {
      const ceCandidates = replyContainer
        ? Array.from(replyContainer.querySelectorAll('[contenteditable="true"], .ql-editor'))
        : Array.from(scope.querySelectorAll('.m-sheet-reply [contenteditable="true"], .m-sheet-reply .ql-editor'));

      const visibleEditors = ceCandidates
        .filter(el => el && el.isContentEditable && isVisibleElement(el))
        .sort((a, b) => elementArea(b) - elementArea(a));

      for (const richEditor of visibleEditors) {
        wroteToEditor = setContentEditablePlainText(richEditor, text);
        if (!wroteToEditor) continue;
        if (snippet) {
          const after = (richEditor.innerText || richEditor.textContent || '').trim();
          if (!after.includes(snippet)) {
            wroteToEditor = false;
            continue;
          }
        }
        break;
      }
    }

    if (!wroteToEditor) {
      const replyRoot = replyContainer
        ? replyContainer.querySelector('.fishd-input')
        : scope.querySelector('.fishd-input');
      if (replyRoot) {
        const editable =
          replyRoot.matches('textarea, input')
            ? replyRoot
            : replyRoot.querySelector('textarea, input, [contenteditable="true"]');

        if (editable) {
          if (editable.matches('textarea, input')) {
            editable.focus();
            setNativeValue(editable, text);
            dispatchTextInputEvents(editable, text);
          } else {
            editable.focus();
            editable.textContent = text;
            editable.dispatchEvent(new Event('input', { bubbles: true }));
          }
        } else if (replyRoot.isContentEditable) {
          replyRoot.focus();
          replyRoot.textContent = text;
          replyRoot.dispatchEvent(new Event('input', { bubbles: true }));
        }
      }
    }

    const st = panelState(scope);
    st.isManualRegenerate = false;
    st.currentSuggestion = text;
    if (options && options.keepState) {
      transitionTo(scope, AI_STATES.SHOWING, {
        suggestion: text,
        knowledgeSources: st.currentKnowledgeSources
      });
      return;
    }
    transitionTo(scope, AI_STATES.IDLE);
  }

  // 请求AI建议
  async function requestSuggestion(options = {}) {
    const scopeRoot = options.scopeRoot || getDefaultScope();
    const st = panelState(scopeRoot);
    if (st.currentState === AI_STATES.GENERATING && !st.suggestionRequestToken) {
      recoverScopeAfterInterruptedRequest(scopeRoot, 'stale_generating_state');
    }
    if (st.suggestionRequestToken || st.currentState === AI_STATES.GENERATING) {
      log('已在生成中，忽略新的生成请求');
      return;
    }

    const snapshot = options.snapshot || buildConversationSnapshot(scopeRoot);
    const messages = snapshot.messages;
    const trigger = options.trigger || 'auto';

    try {
      const worksheetMode =
        (messages &&
          messages.length === 1 &&
          messages[0] &&
          messages[0].sender === '工单') ||
        isWorksheetModeForScope(scopeRoot);
      const ticketData = worksheetMode ? extractTicketData(scopeRoot) : null;
      if (worksheetMode) {
        debugTicketExtraction(ticketData);
      }

      if (messages.length < CONFIG.MIN_MESSAGES) {
        log('消息不足，暂不生成建议');
        st.lastConversationSnapshot = snapshot;
        st.lastConversationHash = snapshot.hash;
        transitionTo(scopeRoot, AI_STATES.IDLE);
        return;
      }

      const requestToken = Symbol('request');
      const requestTicketId = getScopeTicketIdentityValue(scopeRoot) || st.lastWorksheetTicketId || '';
      st.suggestionRequestToken = requestToken;
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;

      // 显示加载状态
      showLoadingState(scopeRoot, options.message);

      const ticketIdStr =
        ticketData && ticketData.ticket_id ? String(ticketData.ticket_id).trim() : '';

      const requestData = {
        intent: "suggestion",
        ...(ticketIdStr ? { session_id: ticketIdStr } : {}),
        query_info: {
          query: messages.length > 0 ? messages[messages.length - 1].content : "",
        },
        works_info: {
          ticket_id: ticketIdStr,
          title: ticketData?.title || "",
          desc: ticketData?.desc || (messages.length > 0 ? messages[messages.length - 1].content : ""),
          history: Array.isArray(ticketData?.history_items)
            ? ticketData.history_items
              .map((item, index) => ({
                index: index + 1,
                summary: String(item || '').trim(),
              }))
              .filter(row => row.summary)
            : [],
          priority: ticketData?.priority ?? null,
          status: ticketData?.status ?? null
        },
        core_info: ticketData?.core_info || {
          customer_name: "",
          project_name: "",
          mall_name: ""
        },
        attention_info: ticketData?.attention_info || {
          project_attention: "",
          supplier_attention: ""
        }
      };

      const useStream = localStorage.getItem('ai_use_stream_chat') === 'true';
      let apiResponse;

      if (useStream) {
        const streamUrl = resolveChatStreamApiUrl();
        apiResponse = await new Promise((resolve, reject) => {
          const port = chrome.runtime.connect({ name: 'chatStream' });
          let doneData = null;
          port.onMessage.addListener(function onMsg(msg) {
            if (!msg.ok) {
              port.onMessage.removeListener(onMsg);
              try {
                port.disconnect();
              } catch (e) {}
              reject(new Error(msg.error || '流式请求失败'));
              return;
            }
            if (msg.finished) {
              port.onMessage.removeListener(onMsg);
              try {
                port.disconnect();
              } catch (e) {}
              if (doneData) {
                resolve(doneData);
              } else {
                reject(new Error('流式结束但未收到有效结果'));
              }
              return;
            }
            const ev = msg.event;
            if (!ev || !ev.event) {
              return;
            }
            if (ev.event === 'done' && ev.data) {
              doneData = ev.data;
            }
            if (ev.event === 'error') {
              port.onMessage.removeListener(onMsg);
              try {
                port.disconnect();
              } catch (e2) {}
              reject(new Error(ev.detail || '流式错误'));
            }
          });
          port.postMessage({
            action: 'start',
            url: streamUrl,
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: requestData,
          });
        });
      } else {
        const apiUrl = resolveChatApiUrl();
        apiResponse = await new Promise((resolve, reject) => {
          chrome.runtime.sendMessage({
            action: 'apiRequest',
            url: apiUrl,
            method: 'POST',
            headers: {
              'Content-Type': 'application/json'
            },
            body: requestData
          }, (response) => {
            if (chrome.runtime.lastError) {
              reject(new Error(chrome.runtime.lastError.message));
              return;
            }
            if (response.success) {
              resolve(response.data);
            } else {
              reject(new Error(response.error || 'API请求失败'));
            }
          });
        });
      }

      const data = apiResponse;
      if (st.suggestionRequestToken !== requestToken) {
        log(
          `请求结果已过期，忽略返回结果。ticket=${st.lastWorksheetTicketId || 'unknown'}`,
          'warn'
        );
        if (st.suggestionRequestToken === null) {
          recoverScopeAfterInterruptedRequest(scopeRoot, 'stale_response');
        }
        return;
      }
      if (!isSameTicketContext(scopeRoot, requestTicketId)) {
        st.suggestionRequestToken = null;
        recoverScopeAfterInterruptedRequest(scopeRoot, 'ticket_context_changed');
        return;
      }

      st.suggestionRequestToken = null;

      if (data.suggestion) {
        st.currentSuggestion = data.suggestion;
        // 保存知识源信息
        st.currentKnowledgeSources = data.knowledge_sources || [];
        st.lastUpdatedAt = Date.now();
        transitionTo(scopeRoot, AI_STATES.SHOWING, {
          suggestion: data.suggestion,
          knowledgeSources: st.currentKnowledgeSources
        });
        st.lastSuggestionHash = st.lastConversationSnapshot ? st.lastConversationSnapshot.hash : snapshot.hash;
        
        // 如果是手动重新生成，在成功显示后保持标志，直到用户采纳或隐藏
        // 如果不是手动重新生成，清除标志
        if (options.trigger !== 'regenerate') {
          st.isManualRegenerate = false;
        }
      } else {
        throw new Error('API返回数据格式错误');
      }

    } catch (error) {
      if (st.suggestionRequestToken === null) {
        log(`生成已取消或已恢复空闲: ${error.message}`);
        recoverScopeAfterInterruptedRequest(scopeRoot, 'request_cancelled');
        return;
      }
      st.suggestionRequestToken = null;
      log(`生成建议失败: ${error.message}`, 'error');

      // 根据错误类型显示不同的用户友好消息
      let userMessage = error.message;

      // 检查是否是502错误或网络连接问题
      if (error.message && error.message.includes('502')) {
        userMessage = "后端模型不可用，请稍后重试";
      } else if (error.message && (error.message.includes('Failed to fetch') || error.message.includes('fetch'))) {
        userMessage = "后端服务不可用，请检查网络连接";
      } else if (error.name === 'TypeError' && error.message.includes('fetch')) {
        userMessage = "无法连接到后端服务";
      } else if (error.message.includes('API返回数据格式错误')) {
        userMessage = "后端服务返回异常，请稍后重试";
      }

      transitionTo(scopeRoot, AI_STATES.ERROR, { errorMessage: userMessage });
    }
  }

  // 检查是否需要生成建议
  function shouldGenerateSuggestion(scopeRoot = document.body) {
    const st = panelState(scopeRoot);
    const snapshot = buildConversationSnapshot(scopeRoot);
    const { messages, lastMessage, hash, lastAgentMessage, lastCustomerMessage } = snapshot;

    if (isWorksheetModeForScope(scopeRoot)) {
      if (messages.length === 0) {
        return { shouldAutoGenerate: false, shouldShowManual: false, snapshot };
      }
      return {
        shouldAutoGenerate: false,
        shouldShowManual: false,
        snapshot,
        hasConversationChange: hash !== st.lastConversationHash
      };
    }

    log(`提取到 ${messages.length} 条消息`);

    if (messages.length === 0) {
      log('没有有效消息，跳过建议生成');
      return { shouldAutoGenerate: false, shouldShowManual: false, snapshot };
    }

    if (!lastMessage) {
      return { shouldAutoGenerate: false, shouldShowManual: false, snapshot };
    }

    log(`最后一条消息: role=${lastMessage.role}, content="${lastMessage.content}"`);
    log(`最后一条客户消息: "${lastCustomerMessage || ''}"`);
    log(`最后一条客服消息: "${lastAgentMessage || ''}"`);

    const result = {
      shouldAutoGenerate: false,
      shouldShowManual: false,
      snapshot,
      hasConversationChange: hash !== st.lastConversationHash
    };

    // 优先判断最后一条消息是否为客服消息（使用更精确的识别）
    const agentKeywords = ['客服', '售后', '顾问', '助理', '小助手', '机器人'];
    let isLastMessageAgent = false;

    if (lastMessage.role === '客服' || lastMessage.role === 'agent') {
      isLastMessageAgent = true;
    } else if (lastMessage.sender && agentKeywords.some(keyword => lastMessage.sender.includes(keyword))) {
      isLastMessageAgent = true;
    } else if (agentKeywords.some(keyword => lastMessage.content.toLowerCase().includes(keyword))) {
      const agentPatterns = ['[客服]', '[售后]', '[顾问]', '客服:', '售后:'];
      if (agentPatterns.some(pattern => lastMessage.content.includes(pattern))) {
        isLastMessageAgent = true;
      }
    }

    if (isLastMessageAgent) {
      log('检测到客服消息，显示手动触发按钮');
      result.shouldShowManual = true;
      result.lastAgentMessage = lastAgentMessage;
      return result;
    }

    // 如果最后一条不是客服消息，自动生成建议
    if (lastCustomerMessage) {
      log('检测到客户消息，自动生成建议');
      result.shouldAutoGenerate = true;
      return result;
    }

    return result;
  }

  async function handleSuggestionDecision(result, options = {}) {
    const scopeRoot = options.scopeRoot || getDefaultScope();
    const st = panelState(scopeRoot);
    const snapshot = result.snapshot;
    if (!snapshot) return;

    const fromPolling = options.fromPolling || false;

    if (st.currentState === AI_STATES.GENERATING) {
      log('生成中，忽略本次检查');
      return;
    }

    if (st.currentState === AI_STATES.ERROR && fromPolling) {
      log('错误状态等待用户处理，轮询暂不干预');
      return;
    }

    if (fromPolling && isWorksheetModeForScope(scopeRoot) && st.currentState === AI_STATES.SHOWING) {
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
      return;
    }

    // 如果当前正在显示建议，且是手动重新生成的结果，保护它不被轮询覆盖
    if (st.currentState === AI_STATES.SHOWING && fromPolling && st.isManualRegenerate) {
      log('手动重新生成的建议正在显示，轮询不覆盖');
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
      return;
    }

    if (st.currentState === AI_STATES.SHOWING && fromPolling && result.hasConversationChange) {
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
      const hint = '检测到新的对话更新，可重新生成建议保持准确';
      renderSuggestionState(scopeRoot, { suggestion: st.currentSuggestion || '', hint });
      return;
    }

    if (result.shouldAutoGenerate) {
      if (snapshot.hash === st.lastSuggestionHash) {
        st.lastConversationSnapshot = snapshot;
        st.lastConversationHash = snapshot.hash;
        log('当前客户消息已生成建议，跳过自动生成');
        return;
      }
      // 更新快照，避免重复触发
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
      await requestSuggestion({ trigger: 'auto', snapshot, scopeRoot });
      return;
    }

    // 如果当前正在显示建议（可能是手动重新生成的），且来自轮询，不覆盖
    if (result.shouldShowManual) {
      // 如果当前状态是 SHOWING 且来自轮询，说明用户已经手动重新生成了建议，不应该覆盖
      if (st.currentState === AI_STATES.SHOWING && fromPolling) {
        log('当前正在显示建议（可能是手动重新生成的），轮询不覆盖为手动触发状态');
        st.lastConversationSnapshot = snapshot;
        st.lastConversationHash = snapshot.hash;
        return;
      }
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
      transitionTo(scopeRoot, AI_STATES.AGENT_REPLIED, { lastAgentMessage: result.lastAgentMessage });
      return;
    }

    if (fromPolling) {
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
    } else if (!result.shouldAutoGenerate && !result.shouldShowManual) {
      st.lastConversationSnapshot = snapshot;
      st.lastConversationHash = snapshot.hash;
    }

    if (st.currentState !== AI_STATES.IDLE) {
      transitionTo(scopeRoot, AI_STATES.IDLE);
    }
  }

  function getDefaultQueryDisplayHtml() {
    return '<div class="ai-suggestion-text">可输入查询内容...</div>';
  }

  function getRegisteredPanelScopes() {
    const scopes = [];
    for (const st of panelRegistry.values()) {
      if (!st || !(st.scopeRoot instanceof Element)) continue;
      const scope = st.scopeRoot;
      if (!scope.isConnected) continue;
      if (!isInteractiveWorksheetScope(scope)) continue;
      scopes.push(scope);
    }
    return scopes;
  }

  function syncAuxiliaryPanels(scopeRoot = getDefaultScope()) {
    const st = panelState(scopeRoot);
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;

    const display = box.querySelector('.ai-suggestion-display');
    const queryBtn = box.querySelector('.ai-query-btn');
    const ragContainer = box.querySelector('.ai-rag-references');
    if (display) {
      let displayHtml = '';
      if (st.queryStatus === 'loading') {
        displayHtml = '<div class="ai-loading"><span class="ai-loading-spinner"></span>AI 正在查询中...</div>';
      } else if (st.lastQueryHtml) {
        displayHtml = st.lastQueryHtml;
      } else {
        displayHtml = getDefaultQueryDisplayHtml();
      }
      setInnerHtmlIfChanged(display, displayHtml);
    }
    if (queryBtn) {
      setButtonState(queryBtn, st.queryStatus === 'loading' ? '查询中...' : '查询', st.queryStatus === 'loading');
    }
    renderKbSourceNames(ragContainer, '检索来源：', st.lastQuerySources || []);

    const summaryBtn = box.querySelector('.ai-summary-btn');
    const infoSummaryEl = box.querySelector('.ai-summary-info-summary');
    const reviewEl = box.querySelector('.ai-summary-review');
    if (summaryBtn) {
      if (st.summaryStatus === 'loading') {
        setButtonState(summaryBtn, '<span class="ai-loading-spinner"></span>生成中...', true);
      } else {
        setButtonState(summaryBtn, '信息总结', false);
      }
    }
    if (infoSummaryEl) {
      if (st.summaryStatus === 'loading') {
        setInnerHtmlIfChanged(infoSummaryEl, '<span class="ai-loading-spinner"></span>加载中...');
      } else if (st.summaryError) {
        setInnerHtmlIfChanged(infoSummaryEl, `<span style="color:#ff6b6b">${escapeHtml(st.summaryError)}</span>`);
      } else {
        setInnerHtmlIfChanged(infoSummaryEl, st.lastSummaryInfoHtml || '--');
      }
    }
    if (reviewEl) {
      if (st.summaryStatus === 'loading') {
        setInnerHtmlIfChanged(reviewEl, '<span class="ai-loading-spinner"></span>加载中...');
      } else if (st.summaryError) {
        setInnerHtmlIfChanged(reviewEl, `<span style="color:#ff6b6b">${escapeHtml(st.summaryError)}</span>`);
      } else {
        setInnerHtmlIfChanged(reviewEl, st.lastSummaryReviewHtml || '--');
      }
    }
    renderKbSourceNames(
      box.querySelector('.ai-summary-kb-sources'),
      '检索来源：',
      st.lastSummarySources || []
    );
  }

  // 请求信息总结
  async function requestSummary(scopeRoot = getDefaultScope()) {
    const st = panelState(scopeRoot);
    if (st.summaryRequestToken) {
      log('当前面板信息总结生成中，忽略重复点击');
      return;
    }
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    const requestToken = Symbol('summary');
    const requestTicketId = getScopeTicketIdentityValue(scopeRoot) || st.lastWorksheetTicketId || '';
    st.summaryRequestToken = requestToken;
    st.summaryStatus = 'loading';
    st.summaryError = '';
    syncAuxiliaryPanels(scopeRoot);

    try {
      // Build request data
      const snapshot = buildConversationSnapshot(scopeRoot);
      const messages = snapshot.messages;
      
      const worksheetMode = isWorksheetModeForScope(scopeRoot);
      const ticketData = worksheetMode ? extractTicketData(scopeRoot) : null;
      const ticketIdStr =
        ticketData && ticketData.ticket_id ? String(ticketData.ticket_id).trim() : '';

      const requestData = {
        intent: "summary",
        ...(ticketIdStr ? { session_id: ticketIdStr } : {}),
        query_info: {
          query: messages.length > 0 ? messages[messages.length - 1].content : "",
        },
        works_info: {
          ticket_id: ticketIdStr,
          title: ticketData?.title || "",
          desc: ticketData?.desc || "",
          history: Array.isArray(ticketData?.history_items)
            ? ticketData.history_items
              .map((item, index) => ({
                index: index + 1,
                summary: String(item || '').trim(),
              }))
              .filter(row => row.summary)
            : [],
          priority: ticketData?.priority ?? null,
          status: ticketData?.status ?? null
        },
        core_info: ticketData?.core_info || {
          customer_name: "",
          project_name: "",
          mall_name: ""
        },
        attention_info: ticketData?.attention_info || {
          project_attention: "",
          supplier_attention: ""
        }
      };

      // API Path
      const apiUrl = resolveChatApiUrl();

      // Call API
      const response = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage({
          action: 'apiRequest',
          url: apiUrl,
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: requestData
        }, (res) => {
          if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
          else if (res.success) resolve(res.data);
          else reject(new Error(res.error || 'API请求失败'));
        });
      });

      // Render Result
      const data = response;
      if (st.summaryRequestToken !== requestToken) {
         if (st.summaryRequestToken === null) {
           recoverSummaryPaneAfterInterruptedRequest(scopeRoot, 'stale_response');
         }
         return;
      }
      if (!isSameTicketContext(scopeRoot, requestTicketId)) {
         st.summaryRequestToken = null;
         recoverSummaryPaneAfterInterruptedRequest(scopeRoot, 'ticket_context_changed');
         return;
      }
      st.summaryRequestToken = null;
      if (data && data.summary) {
         const { info_summary, review, reviews, summary_sources } = data.summary;
         st.lastSummaryInfoHtml = formatSummaryText(info_summary || '待确认');
         st.lastSummaryReviewHtml = formatSummaryText(reviews || review || '无');
         st.lastSummarySources = Array.isArray(summary_sources) ? summary_sources : [];
         st.summaryStatus = 'showing';
         st.summaryError = '';
         st.lastUpdatedAt = Date.now();
         syncAuxiliaryPanels(scopeRoot);
      } else {
         throw new Error('返回数据格式错误');
      }

    } catch (error) {
      if (st.summaryRequestToken === null) {
        recoverSummaryPaneAfterInterruptedRequest(scopeRoot, 'request_cancelled');
        return;
      }
      st.summaryRequestToken = null;
      st.summaryStatus = 'error';
      st.summaryError = '生成失败';
      log(`生成总结失败: ${error.message}`, 'error');
      st.lastUpdatedAt = Date.now();
      syncAuxiliaryPanels(scopeRoot);
    }
  }

  // 获取或创建常驻建议框
  function getOrCreateSuggestionBox(scopeRoot = getDefaultScope()) {
    const scope = scopeRoot || getDefaultScope();
    const st = panelState(scope);
    let box = scope.querySelector('.ai-suggestion-box');

    // 如果找到了隐藏的插件，重新显示它
    if (box && box.style.display === 'none') {
      box.style.display = 'block';
      box.dataset.panelKey = st.panelKey;
      return box;
    }

    // 如果没有找到，创建新的
    if (!box) {
      box = document.createElement('div');
      box.className = 'ai-suggestion-box';
      box.dataset.panelKey = st.panelKey;
      box.innerHTML = `
        <div class="ai-close-icon" title="收起">−</div>
        
        <div class="ai-minimized-view" title="点击展开">
          <div style="display:flex;align-items:center;gap:8px;">
            <span>💡 AI 建议助手</span>
          </div>
          <span style="font-size:18px;line-height:1;opacity:0.8;">+</span>
        </div>

        <div class="ai-layout-container">
          <!-- 左栏 40%：信息总结区 -->
          <div class="ai-left-column">
            <div class="ai-column-header">信息总结</div>
            <div class="ai-summary-content">
              <div class="ai-summary-section">
                  <div class="ai-summary-label">信息总结：</div>
                  <div class="ai-summary-value ai-summary-info-summary">--</div>
              </div>
              <div class="ai-summary-section">
                  <div class="ai-summary-label">注意事项：</div>
                  <div class="ai-summary-value ai-summary-review">--</div>
              </div>
            </div>
            <div class="ai-actions-row ai-actions-row-summary">
              <button class="ai-btn ai-btn-secondary ai-summary-btn">信息总结</button>
              <div class="ai-kb-sources ai-summary-kb-sources"></div>
            </div>
          </div>

          <!-- 中栏 40%：查询任务区 -->
            <div class="ai-center-column">
            <div class="ai-column-header">查询结果：</div>
            <div class="ai-suggestion-display">
               <div class="ai-suggestion-text">可输入查询内容...</div>
            </div>
            
            <div class="ai-input-label">输入框：</div>
            <textarea class="ai-custom-input" placeholder="知识库查询：输入问题后点击「查询」"></textarea>
            
            <div class="ai-actions-row ai-actions-row-query">
              <button class="ai-btn ai-btn-primary ai-query-btn">查询</button>
              <div class="ai-kb-sources ai-rag-references"></div>
            </div>
          </div>

          <!-- 右栏 20%：工单回复建议区 -->
          <div class="ai-right-column">
            <div class="ai-column-header">工单回复建议</div>
            <div class="ai-suggestion-content"></div>
            <div class="ai-input-label ai-suggestion-block-label">工单回复建议：</div>
            <div class="ai-summary-value ai-summary-suggestion">--</div>
            <div class="ai-kb-sources ai-suggestion-kb-sources"></div>
            <div class="ai-actions-row ai-actions-row-suggestion">
              <div class="ai-suggestion-actions" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <button class="ai-btn ai-btn-primary ai-generate-btn ai-btn-compact">生成建议</button>
                <button class="ai-btn ai-btn-secondary ai-accept-btn ai-btn-compact" style="display:none;">采纳</button>
              </div>
            </div>
          </div>
        </div>
      `;

      // Event Listeners
      const closeIcon = box.querySelector('.ai-close-icon');
      const generateBtn = box.querySelector('.ai-generate-btn');
      const acceptBtn = box.querySelector('.ai-accept-btn');
      const queryBtn = box.querySelector('.ai-query-btn');
      const summaryBtn = box.querySelector('.ai-summary-btn');
      const customInput = box.querySelector('.ai-custom-input');
      const minimizedView = box.querySelector('.ai-minimized-view');

      const toggleMinimize = (e) => {
        if (e) e.stopPropagation();
        box.classList.toggle('minimized');
      };

      closeIcon.addEventListener('click', toggleMinimize);
      minimizedView.addEventListener('click', toggleMinimize);
      
      generateBtn.addEventListener('click', () => handleManualGenerate(scope));
      if (acceptBtn) acceptBtn.addEventListener('click', () => handleAcceptSuggestion(scope));
      summaryBtn.addEventListener('click', () => requestSummary(scope));
      if (queryBtn) queryBtn.addEventListener('click', () => handleQueryRequest(scope));

      box.dataset.listenersAttached = 'true';

      // Input persistence
      if (customInput) {
        const getDraftKey = () => {
          const ticketId = getCurrentWorksheetTicketId(scope) || panelState(scope).lastWorksheetTicketId || 'default';
          return 'ai_kb_query_draft_' + ticketId;
        };
        const key = getDraftKey();
        const savedInput = sessionStorage.getItem(key);
        if (savedInput) customInput.value = savedInput;
        customInput.addEventListener('input', () => {
          sessionStorage.setItem(getDraftKey(), customInput.value);
        });
      }

      // 插入到 DOM：旧版挂在 .m-sheet-reply 内；新版挂在主内容区顶部
      const anchor = getWorksheetUiAnchor(scope);
      if (anchor) {
        if (anchor.classList && anchor.classList.contains('m-sheet-reply')) {
          anchor.style.position = 'relative';
          anchor.style.zIndex = '1';
        }
        const firstDiv = anchor.querySelector ? anchor.querySelector('div') : null;
        if (firstDiv && anchor !== document.body) anchor.insertBefore(box, firstDiv);
        else anchor.appendChild(box);
      } else {
        if (box && box.parentNode) box.parentNode.removeChild(box);
        return null;
      }
    }
    
    // Ensure event listeners are attached (for existing box)
    if (box && !box.dataset.listenersAttached) {
       const closeIcon = box.querySelector('.ai-close-icon');
       const minimizedView = box.querySelector('.ai-minimized-view');
       const generateBtn = box.querySelector('.ai-generate-btn');
       const acceptBtn = box.querySelector('.ai-accept-btn');
       const queryBtn = box.querySelector('.ai-query-btn');
       const summaryBtn = box.querySelector('.ai-summary-btn');
       
       const toggleMinimize = (e) => {
         if (e) e.stopPropagation();
         box.classList.toggle('minimized');
       };

       if (closeIcon) {
           const newClose = closeIcon.cloneNode(true);
           closeIcon.parentNode.replaceChild(newClose, closeIcon);
           newClose.addEventListener('click', toggleMinimize);
       }
       if (minimizedView) {
           // Replace minimized view to clear listeners if any
           const newView = minimizedView.cloneNode(true);
           minimizedView.parentNode.replaceChild(newView, minimizedView);
           newView.addEventListener('click', toggleMinimize);
       }
       if (generateBtn) {
           const newBtn = generateBtn.cloneNode(true);
           generateBtn.parentNode.replaceChild(newBtn, generateBtn);
           newBtn.addEventListener('click', () => handleManualGenerate(scope));
       }
       if (acceptBtn) {
           const newBtn = acceptBtn.cloneNode(true);
           acceptBtn.parentNode.replaceChild(newBtn, acceptBtn);
           newBtn.addEventListener('click', () => handleAcceptSuggestion(scope));
       }
       if (queryBtn) {
           const newBtn = queryBtn.cloneNode(true);
           queryBtn.parentNode.replaceChild(newBtn, queryBtn);
           newBtn.addEventListener('click', () => handleQueryRequest(scope));
       }
       if (summaryBtn) {
           const newBtn = summaryBtn.cloneNode(true);
           summaryBtn.parentNode.replaceChild(newBtn, summaryBtn);
           newBtn.addEventListener('click', () => requestSummary(scope));
       }
       
       box.dataset.listenersAttached = 'true';
    }
    if (box) {
      box.dataset.panelKey = st.panelKey;
    }
    
    return box;
  }

  async function handleQueryRequest(scopeRoot = getDefaultScope()) {
    const st = panelState(scopeRoot);
    if (st.queryRequestToken) {
      log('当前面板查询中，忽略重复点击');
      return;
    }
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    const customInput = box.querySelector('.ai-custom-input');

    const query = customInput ? customInput.value.trim() : '';
    if (!query) {
      st.queryStatus = 'error';
      st.queryError = '请先在输入框中输入查询内容';
      st.lastQueryHtml = '<div class="ai-error" style="color:#ffcccb">⚠️ 请先在输入框中输入查询内容</div>';
      st.lastQuerySources = [];
      syncAuxiliaryPanels(scopeRoot);
      return;
    }

    const requestToken = Symbol('query');
    const requestTicketId = getScopeTicketIdentityValue(scopeRoot) || st.lastWorksheetTicketId || '';
    st.queryRequestToken = requestToken;
    st.queryStatus = 'loading';
    st.queryError = '';
    syncAuxiliaryPanels(scopeRoot);

    try {
      const ticketData = isWorksheetModeForScope(scopeRoot) ? extractTicketData(scopeRoot) : null;
      const ticketIdStr =
        ticketData && ticketData.ticket_id ? String(ticketData.ticket_id).trim() : '';

      const requestData = {
        intent: "query",
        ...(ticketIdStr ? { session_id: ticketIdStr } : {}),
        query_info: { query },
        works_info: {
          ticket_id: ticketIdStr,
          title: ticketData?.title || "",
          desc: ticketData?.desc || "",
          history: [],
          priority: ticketData?.priority ?? null,
          status: ticketData?.status ?? null
        },
        core_info: ticketData?.core_info || {
          customer_name: "",
          project_name: "",
          mall_name: ""
        },
        attention_info: ticketData?.attention_info || {
          project_attention: "",
          supplier_attention: ""
        }
      };

      const apiUrl = resolveChatApiUrl();

      const apiResponse = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage({
          action: 'apiRequest',
          url: apiUrl,
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: requestData
        }, (response) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
            return;
          }
          if (response.success) {
            resolve(response.data);
          } else {
            reject(new Error(response.error || 'API请求失败'));
          }
        });
      });

      if (st.queryRequestToken !== requestToken) {
        if (st.queryRequestToken === null) {
          recoverQueryPaneAfterInterruptedRequest(scopeRoot, 'stale_response');
        }
        return;
      }
      if (!isSameTicketContext(scopeRoot, requestTicketId)) {
        st.queryRequestToken = null;
        recoverQueryPaneAfterInterruptedRequest(scopeRoot, 'ticket_context_changed');
        return;
      }
      st.queryRequestToken = null;
      const answer = apiResponse.answer || '';
      const sources = apiResponse.sources || [];

      st.lastQueryAnswer = answer;
      st.lastQueryHtml = answer
        ? `<div class="ai-suggestion-text">${escapeHtml(answer)}</div>`
        : '<div class="ai-suggestion-text">未找到相关信息，请尝试换个关键词查询。</div>';
      st.lastQuerySources = Array.isArray(sources) ? sources : [];
      st.queryStatus = 'showing';
      st.queryError = '';
      st.lastUpdatedAt = Date.now();
      syncAuxiliaryPanels(scopeRoot);

    } catch (err) {
      if (st.queryRequestToken === null) {
        recoverQueryPaneAfterInterruptedRequest(scopeRoot, 'request_cancelled');
        return;
      }
      st.queryRequestToken = null;
      st.queryStatus = 'error';
      log(`查询失败: ${err.message}`, 'error');
      let userMessage = err.message;
      if (err.message && err.message.includes('502')) {
        userMessage = "后端模型不可用，请稍后重试";
      } else if (err.message && (err.message.includes('Failed to fetch') || err.message.includes('fetch'))) {
        userMessage = "后端服务不可用，请检查网络连接";
      }
      st.queryError = userMessage;
      st.lastQueryHtml = `<div class="ai-error" style="color:#ffcccb">⚠️ 查询失败：${escapeHtml(userMessage)}</div>`;
      st.lastQuerySources = [];
      st.lastUpdatedAt = Date.now();
      syncAuxiliaryPanels(scopeRoot);
    }
  }

  function renderIdleState(scopeRoot = getDefaultScope()) {
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    
    // Reset generate button
    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    const suggContent = box.querySelector('.ai-suggestion-content');
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (generateBtn) {
        setTextIfChanged(generateBtn, '生成建议');
        generateBtn.disabled = false;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'none';
        acceptBtn.disabled = false;
    }
    if (suggContent) {
      setInnerHtmlIfChanged(suggContent, '');
    }
    if (summarysuggEl) {
      setTextIfChanged(summarysuggEl, '--');
    }
    renderKbSourceNames(box.querySelector('.ai-suggestion-kb-sources'), '检索来源：', []);
    syncAuxiliaryPanels(scopeRoot);
  }

  function renderAgentRepliedState(scopeRoot = getDefaultScope(), options = {}) {
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (summarysuggEl) setTextIfChanged(summarysuggEl, '检测到客服已回复。点击“生成建议”可获取 AI 建议。');
    syncAuxiliaryPanels(scopeRoot);
  }

  function renderGeneratingState(scopeRoot = getDefaultScope(), options = {}) {
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (summarysuggEl) {
      setInnerHtmlIfChanged(
        summarysuggEl,
        `<div class="ai-loading" style="color:#1f2a3d;"><span class="ai-loading-spinner" style="border-color:rgba(31,42,61,0.25);border-top-color:#1f2a3d;"></span>${escapeHtml(options.message || 'AI 正在思考中...')}</div>`
      );
    }
    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    if (generateBtn) {
        setTextIfChanged(generateBtn, '生成中...');
        generateBtn.disabled = true;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'none';
    }
    renderKbSourceNames(box.querySelector('.ai-suggestion-kb-sources'), '检索来源：', []);
    syncAuxiliaryPanels(scopeRoot);
  }

  function renderSuggestionState(scopeRoot = getDefaultScope(), options = {}) {
    const st = panelState(scopeRoot);
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    
    const suggestionText = options.suggestion || st.currentSuggestion || '';

    // 收起态下 .ai-layout-container 为 display:none，正文已写入也看不见；有有效内容时自动展开
    if (suggestionText && suggestionText !== '--') {
      box.classList.remove('minimized');
    }

    // 建议结果展示在右侧「工单回复建议」区域
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (summarysuggEl) setTextIfChanged(summarysuggEl, suggestionText || '--');

    let knowledgeSources = [];
    const fromApi = options.knowledgeSources;
    if (Array.isArray(fromApi) && fromApi.length) {
      knowledgeSources = fromApi.map((x) => String(x || '').trim()).filter(Boolean);
    } else if (Array.isArray(st.currentKnowledgeSources) && st.currentKnowledgeSources.length) {
      knowledgeSources = st.currentKnowledgeSources.map((x) => String(x || '').trim()).filter(Boolean);
    }
    if (!knowledgeSources.length) {
      knowledgeSources = extractSourceNamesFromBracketTags(suggestionText);
    }
    if (!knowledgeSources.length) {
      knowledgeSources = parseKbSourceNamesFromSummaryColumn(box);
    }
    renderKbSourceNames(
      box.querySelector('.ai-suggestion-kb-sources'),
      '检索来源：',
      knowledgeSources
    );

    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    const suggContent = box.querySelector('.ai-suggestion-content');
    if (generateBtn) {
        setTextIfChanged(generateBtn, '重新生成');
        generateBtn.disabled = false;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'inline-flex';
        acceptBtn.disabled = false;
    }
    if (suggContent) {
      const hintHtml = options.hint
        ? `<div class="ai-subtle-hint">${escapeHtml(options.hint)}</div>`
        : '';
      setInnerHtmlIfChanged(suggContent, hintHtml);
    }
    syncAuxiliaryPanels(scopeRoot);
  }

  function renderErrorState(scopeRoot = getDefaultScope(), options = {}) {
    const box = getOrCreateSuggestionBox(scopeRoot);
    if (!box) return;
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    const message = options.errorMessage || '暂无信息，请稍后重试';
    if (summarysuggEl) {
      setInnerHtmlIfChanged(summarysuggEl, `<span style="color:#ff6b6b">⚠️ ${escapeHtml(message)}</span>`);
    }
    
    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    if (generateBtn) {
        setTextIfChanged(generateBtn, '重试');
        generateBtn.disabled = false;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'none';
    }
    renderKbSourceNames(box.querySelector('.ai-suggestion-kb-sources'), '检索来源：', []);
    syncAuxiliaryPanels(scopeRoot);
  }

  function showLoadingState(scopeRoot = getDefaultScope(), message) {
    transitionTo(scopeRoot, AI_STATES.GENERATING, { message });
  }

  function handleManualGenerate(scopeRoot = getDefaultScope()) {
    // 手动生成时，重新构建快照以确保是最新的对话状态
    const currentSnapshot = buildConversationSnapshot(scopeRoot);
    requestSuggestion({ trigger: 'manual', snapshot: currentSnapshot, scopeRoot });
  }

  function handleAcceptSuggestion(scopeRoot = getDefaultScope()) {
    const st = panelState(scopeRoot);
    if (!st.currentSuggestion) return;
    fillSuggestionKeepDisplay(st.currentSuggestion, scopeRoot);
  }

  // 将建议填入输入框，但保留「工单回复建议」（右栏）、「信息总结/注意事项」（左栏）区域的内容不清空
  function fillSuggestionKeepDisplay(text, scopeRoot = getDefaultScope()) {
    fillSuggestion(text, scopeRoot, { keepState: true });
    syncAuxiliaryPanels(scopeRoot);
  }

  function startWorksheetMode() {
    if (worksheetModeStarted) return;
    worksheetModeStarted = true;
    setupWorksheetObserver();
    ensureWorksheetAssistantMounted();
    getWorksheetPanelScopes().forEach((scopeRoot) => scheduleWorksheetUpdate(scopeRoot));
    if (!worksheetHeartbeatTimer) {
      worksheetHeartbeatTimer = setInterval(() => {
        if (document.hidden || !hasWorksheetContextHint()) return;
        ensureWorksheetAssistantMounted();
        getRegisteredPanelScopes().forEach((scopeRoot) => {
          const st = panelState(scopeRoot);
          const hasBox = Boolean(scopeRoot.querySelector('.ai-suggestion-box'));
          if (!hasBox && !hasInFlightRequests(st)) {
            getOrCreateSuggestionBox(scopeRoot);
            renderState(scopeRoot);
          }
        });
      }, CONFIG.HEARTBEAT_INTERVAL);
    }
  }

  // 主初始化函数
  async function init() {
    log('开始初始化...');

    if (isBasicKefuOuterShell()) {
      log('基础客服壳页：工单在子 iframe 内，本帧不初始化', 'warn');
      return;
    }

    if (window.top !== window) {
      if (!isWorksheetDetailUrlContext()) {
        const maybeWorksheet = document.querySelector('.m-sheet-reply') || document.querySelector('.m-sheet-main');
        if (!maybeWorksheet) {
          return;
        }
      }
    }

    const tryStart = async () => {
      if (!document.body) return false;
      if (!isPageReady()) {
        if (hasWorksheetContextHint()) {
          setupWorksheetObserver();
          ensureWorksheetAssistantMounted();
        }
        return false;
      }

      if (!isWorksheetMode()) {
        const defaultScope = getDefaultScope();
        getOrCreateSuggestionBox(defaultScope);
        renderState(defaultScope);
        const result = shouldGenerateSuggestion(defaultScope);
        await handleSuggestionDecision(result, { scopeRoot: defaultScope });
        log('当前页面不是工单模式，停止初始化', 'warn');
        return true;
      }

      ensureWorksheetAssistantMounted();
      for (const scopeRoot of getWorksheetPanelScopes()) {
        getOrCreateSuggestionBox(scopeRoot);
        renderState(scopeRoot);
        const result = shouldGenerateSuggestion(scopeRoot);
        await handleSuggestionDecision(result, { scopeRoot });
      }
      startWorksheetMode();
      return true;
    };

    if (await tryStart()) {
      return;
    }

    const deadline = Date.now() + CONFIG.INIT_SCAN_TIMEOUT;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, CONFIG.INIT_SCAN_INTERVAL));
      if (await tryStart()) {
        return;
      }
    }

    if (window.top === window && hasWorksheetContextHint()) {
      log('初始化超时：页面未能及时挂载，已保留观察器等待后续 DOM', 'warn');
    }
  }

  // ========== 样式注入 ==========
  const style = document.createElement('style');
  style.textContent = `
    /* 字体层级：L1 栏标题 16/700 · L2 字段标签 13/600 · L3 正文 13/400~500 · L4 辅助 12/500 · 按钮 13/600 */
    .ai-suggestion-box {
      background: linear-gradient(135deg, #6F63E9 0%, #A85AD8 100%);
      border-radius: 12px;
      padding: 24px;
      margin: 16px 0;
      color: white;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      font-size: 13px;
      line-height: 1.5;
      font-weight: 400;
      box-shadow: 0 8px 24px rgba(0,0,0,0.15);
      position: relative;
      z-index: 1000;
      width: 100%;
      box-sizing: border-box;
      transition: all 0.3s ease;
    }

    .ai-suggestion-box.minimized {
        width: auto;
        padding: 10px 16px;
        display: inline-block;
        cursor: pointer;
        min-width: 200px;
      }

      .ai-suggestion-box.minimized .ai-layout-container,
      .ai-suggestion-box.minimized .ai-close-icon {
        display: none;
      }

      .ai-minimized-view {
        display: none;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        font-weight: 600;
        color: white;
        font-size: 13px;
        width: 100%;
      }

      .ai-suggestion-box.minimized .ai-minimized-view {
        display: flex;
      }

      .ai-close-icon {
        position: absolute;
        top: 12px;
        right: 12px;
        width: 24px;
        height: 24px;
        line-height: 24px;
        text-align: center;
        cursor: pointer;
        font-size: 24px;
        font-weight: 300;
        color: rgba(255,255,255,0.8);
        transition: all 0.2s;
        border-radius: 4px;
      }
      .ai-close-icon:hover {
        color: white;
        background: rgba(255,255,255,0.15);
      }

    .ai-layout-container {
      display: flex;
      gap: 20px;
      align-items: stretch;
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
    }

    /* 左 40% / 中 40% / 右 20%（flex 2:2:1） */
    .ai-left-column {
      flex: 2 1 0;
      display: flex;
      flex-direction: column;
      gap: 16px;
      min-height: 0;
      padding-right: 10px;
      border-right: 1px solid rgba(255,255,255,0.15);
      min-width: 0;
      box-sizing: border-box;
    }

    .ai-center-column {
      flex: 2 1 0;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 0;
      padding: 0 10px;
      border-right: 1px solid rgba(255,255,255,0.15);
      min-width: 0;
      box-sizing: border-box;
    }

    .ai-suggestion-content {
      line-height: 1.5;
      color: rgba(255,255,255,0.95);
      min-height: 0;
    }
    .ai-suggestion-content:empty {
      display: none;
    }

    .ai-column-header {
      font-size: 16px;
      font-weight: 700;
      line-height: 1.35;
      margin-bottom: 4px;
      color: white;
      letter-spacing: 0.5px;
    }

    .ai-suggestion-display {
      background: rgba(255,255,255,0.15);
      border-radius: 8px;
      padding: 8px;
      min-height: 80px;
      max-height: 180px;
      overflow-y: auto;
      overflow-x: hidden;
      font-size: 13px;
      font-weight: 400;
      line-height: 1.55;
      backdrop-filter: blur(4px);
      color: white;
      white-space: pre-wrap;
      word-break: break-word;
      box-shadow: inset 0 1px 4px rgba(0,0,0,0.05);
    }
    .ai-suggestion-display::-webkit-scrollbar {
      width: 4px;
    }
    .ai-suggestion-display::-webkit-scrollbar-track {
      background: rgba(255,255,255,0.1);
      border-radius: 2px;
    }
    .ai-suggestion-display::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.35);
      border-radius: 2px;
    }
    .ai-suggestion-display::-webkit-scrollbar-thumb:hover {
      background: rgba(255,255,255,0.55);
    }
    
    .ai-input-label {
      font-size: 13px;
      font-weight: 600;
      line-height: 1.4;
      color: white;
      margin-top: 4px;
    }

    .ai-custom-input {
      width: 100%;
      background: rgba(255,255,255,0.95);
      border: 1px solid rgba(255,255,255,0.3);
      border-radius: 8px;
      padding: 8px;
      color: #333;
      font-size: 13px;
      font-weight: 400;
      resize: vertical;
      min-height: 80px;
      max-height: 180px;
      overflow-y: auto;
      font-family: inherit;
      box-sizing: border-box;
      transition: all 0.2s;
    }
    .ai-custom-input:focus {
      outline: none;
      background: white;
      box-shadow: 0 0 0 3px rgba(255,255,255,0.2);
    }
    .ai-custom-input::placeholder {
      color: #999;
    }

    .ai-actions-row {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: flex-start;
    }

    /* 三栏等高（stretch）时，主操作行沉底，与「_query」按钮同一基准线对齐 */
    .ai-actions-row-query,
    .ai-actions-row-summary,
    .ai-actions-row-suggestion {
      margin-top: auto;
      padding-top: 2px;
    }

    .ai-actions-row-query .ai-kb-sources,
    .ai-actions-row-summary .ai-kb-sources {
      flex: 1 1 auto;
      min-width: 0;
    }

    .ai-suggestion-block-label {
      margin-top: 4px;
      margin-bottom: 0;
    }

    /* 三栏知识库 file_name 来源（与 L4 辅助字号一致） */
    .ai-kb-sources {
      font-size: 12px;
      font-weight: 500;
      color: rgba(255,255,255,0.88);
      background: rgba(0,0,0,0.1);
      padding: 6px 10px;
      border-radius: 6px;
      display: none;
      max-width: 100%;
      max-height: 72px;
      overflow-y: auto;
      line-height: 1.4;
      box-sizing: border-box;
    }
    .ai-kb-sources.visible {
      display: block;
    }
    .ai-kb-sources::-webkit-scrollbar {
      width: 4px;
    }
    .ai-kb-sources::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.3);
      border-radius: 2px;
    }

    .ai-suggestion-kb-sources {
      width: 100%;
      margin-top: 2px;
      flex-shrink: 0;
    }

    .ai-btn {
      padding: 8px 18px;
      border-radius: 6px;
      border: none;
      cursor: pointer;
      font-size: 13px;
      font-weight: 600;
      transition: all 0.2s;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 34px;
      box-sizing: border-box;
    }

    .ai-btn-compact {
      padding: 6px 10px;
      height: 32px;
      font-weight: 600;
    }

    .ai-loading {
      font-size: inherit;
      font-weight: 400;
    }

    .ai-subtle-hint {
      font-size: 12px;
      line-height: 1.5;
      color: rgba(255,255,255,0.86);
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.14);
      border-radius: 8px;
      padding: 6px 8px;
    }

    .ai-btn:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    
    .ai-btn-primary {
      background: white;
      color: #764ba2;
      font-weight: 600;
      box-shadow: 0 2px 6px rgba(0,0,0,0.1);
    }
    .ai-btn-primary:hover:not(:disabled) {
      background: #f8f9fa;
      transform: translateY(-1px);
      box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }

    .ai-btn-secondary {
      background: rgba(255,255,255,0.2);
      color: white;
      border: 1px solid rgba(255,255,255,0.3);
      font-weight: 500;
    }
    .ai-btn-secondary:hover:not(:disabled) {
      background: rgba(255,255,255,0.3);
      border-color: rgba(255,255,255,0.4);
    }

    .ai-right-column {
      flex: 1 1 0;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 0;
      padding-left: 10px;
      min-width: 0;
      box-sizing: border-box;
    }

    .ai-summary-content {
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 0;
    }

    .ai-summary-section {
      border-bottom: 1px solid rgba(255,255,255,0.15);
      padding-bottom: 6px;
    }
    .ai-summary-section:last-child {
      border-bottom: none;
    }

    .ai-summary-label {
      font-size: 13px;
      font-weight: 600;
      line-height: 1.4;
      color: rgb(227, 221, 212);
      margin-bottom: 6px;
      opacity: 1;
    }

    .ai-summary-value {
      font-size: 13px;
      font-weight: 400;
      color: #1f2a3d;
      line-height: 1.55;
      min-height: 20px;
    }

    /* 信息总结和注意事项内容区：超出时出现滚动条，不撑大插件 */
    .ai-summary-info-summary,
    .ai-summary-review {
      max-height: 80px;
      overflow-y: auto;
      padding-right: 4px;
      white-space: pre-wrap;
      word-break: break-word;
    }

    /* 滚动条样式 */
    .ai-summary-info-summary::-webkit-scrollbar,
    .ai-summary-review::-webkit-scrollbar {
      width: 4px;
    }
    .ai-summary-info-summary::-webkit-scrollbar-track,
    .ai-summary-review::-webkit-scrollbar-track {
      background: rgba(255,255,255,0.1);
      border-radius: 2px;
    }
    .ai-summary-info-summary::-webkit-scrollbar-thumb,
    .ai-summary-review::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.35);
      border-radius: 2px;
    }
    .ai-summary-info-summary::-webkit-scrollbar-thumb:hover,
    .ai-summary-review::-webkit-scrollbar-thumb:hover {
      background: rgba(255,255,255,0.55);
    }
    
    .ai-summary-suggestion {
      font-size: 13px;
      color: rgb(58, 234, 96) !important;
      font-weight: 500;
      line-height: 1.55;
      max-height: 110px;
      overflow-y: auto;
      padding-right: 4px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .ai-summary-suggestion::-webkit-scrollbar {
      width: 4px;
    }
    .ai-summary-suggestion::-webkit-scrollbar-track {
      background: rgba(255,255,255,0.1);
      border-radius: 2px;
    }
    .ai-summary-suggestion::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.35);
      border-radius: 2px;
    }
    .ai-summary-suggestion::-webkit-scrollbar-thumb:hover {
      background: rgba(255,255,255,0.55);
    }
    
    .ai-loading-spinner {
      display: inline-block;
      width: 16px;
      height: 16px;
      border: 2px solid rgba(255,255,255,0.3);
      border-radius: 50%;
      border-top-color: white;
      animation: spin 0.8s linear infinite;
      margin-right: 8px;
    }
    
    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    @media (max-width: 900px) {
      .ai-layout-container {
        flex-direction: column;
        gap: 20px;
      }
      .ai-left-column,
      .ai-center-column,
      .ai-right-column {
        flex: 1 1 auto;
        max-width: 100%;
        border-right: none;
        padding-left: 0;
        padding-right: 0;
        border-bottom: 1px solid rgba(255,255,255,0.15);
        padding-bottom: 16px;
      }
      .ai-right-column {
        border-bottom: none;
        padding-bottom: 0;
      }
    }
  `;
  document.head.appendChild(style);

  // 启动插件
  init();

})();
