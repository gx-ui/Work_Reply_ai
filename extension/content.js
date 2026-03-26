// 智能客服助手插件 - 简洁版
(function() {
  'use strict';

  if (window.__csAssistAiInitialized) {
    return;
  }
  window.__csAssistAiInitialized = true;

  // ========== 核心配置 ==========
  // API地址：优先使用服务器地址，本地开发时可手动修改
  // 在浏览器 F12 使用以下命令
  // localStorage.setItem('ai_use_local_api', 'true');
  // location.reload();
  // 后续删除已有配置：
  // localStorage.removeItem('ai_use_local_api');
  const CONFIG = {
    API_BASE: (() => {
      // 优先使用服务器地址
      // const serverUrl = 'https://ai-gateway-show.yunzhonghe.com/cs_assist_ai';
      // 本地开发地址
      const localUrl = 'http://localhost:8003';
      const serverUrl = localStorage.getItem('ai_server_api_base') || localUrl;
      
      // 检查是否强制使用本地API（通过localStorage配置）
      const forceLocal = localStorage.getItem('ai_use_local_api') === 'true';
      // 检查是否强制使用服务器API（通过localStorage配置）
      const forceServer = localStorage.getItem('ai_use_server_api') === 'true';
      
      // 优先级：强制配置 > 默认服务器地址
      if (forceLocal) {
        console.log('[AI助手] 使用本地API:', localUrl);
        return localUrl;
      }
      if (forceServer) {
        console.log('[AI助手] 强制使用服务器API:', serverUrl);
        return serverUrl;
      }

      return serverUrl;
    })(),
    CHECK_INTERVAL: 3000,    // 检查间隔3秒
    MAX_RETRIES: 20,         // 最多重试20
    MIN_MESSAGES: 1          // 最少需要1条消息
  };

  function resolveChatApiUrl() {
    const base = String(CONFIG.API_BASE || '').trim().replace(/\/+$/, '');
    if (base.endsWith('/work_reply_ai')) {
      return `${base}/chat`;
    }
    return `${base}/work_reply_ai/chat`;
  }
  // ========== 全局变量 ==========
  let retryCount = 0;
  let isActive = false;
  
  // 当前建议的知识源信息
  let currentKnowledgeSources = [];

  // ========== 状态管理 ==========
  const AI_STATES = {
    IDLE: 'idle',
    GENERATING: 'generating',
    SHOWING: 'showing',
    AGENT_REPLIED: 'agent_replied',
    ERROR: 'error'
  };

  let currentState = AI_STATES.IDLE;
  let currentSuggestion = null;
  let currentRequestToken = null;
  let lastConversationHash = '';
  let lastConversationSnapshot = null;
  let lastSuggestionHash = '';
  let isManualRegenerate = false; // 标记是否手动重新生成，用于防止轮询覆盖
  let isSummaryGenerating = false;
  const DEBUG = localStorage.getItem('ai_debug') === 'true';

  function transitionTo(newState, payload = {}) {
    if (currentState === newState) {
      log(`状态刷新: ${newState}`);
    } else {
      currentState = newState;
    }
    renderState(payload);
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

  // 在页面上显示调试信息
  function showDebugMessage(message) {
    if (!DEBUG) return;
    // 创建或获取调试信息容器
    let debugBox = document.getElementById('ai-debug-info');
    if (!debugBox) {
      debugBox = document.createElement('div');
      debugBox.id = 'ai-debug-info';
      debugBox.style.cssText = `
        position: fixed;
        top: 10px;
        right: 10px;
        background: rgba(0, 0, 0, 0.8);
        color: white;
        padding: 10px;
        border-radius: 5px;
        font-size: 12px;
        z-index: 9999;
        max-width: 300px;
        font-family: monospace;
        cursor: pointer;
      `;

      // 点击关闭调试框
      debugBox.onclick = function() {
        debugBox.remove();
      };
      document.body.appendChild(debugBox);
    }

    // 添加调试信息
    const time = new Date().toLocaleTimeString();
    debugBox.innerHTML += `<div>[${time}] ${message}</div>`;

    // 5秒后自动清除
    setTimeout(() => {
      const lines = debugBox.innerHTML.split('</div>');
      if (lines.length > 1) {
        // 优化 DOM 操作，避免 innerHTML 导致的重绘性能问题
        if (debugBox.firstChild) {
          debugBox.removeChild(debugBox.firstChild);
        }
      }
    }, 5000);
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

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
  }

  function renderState(payload = {}) {
    switch (currentState) {
      case AI_STATES.IDLE:
        renderIdleState();
        break;
      case AI_STATES.GENERATING:
        renderGeneratingState(payload);
        break;
      case AI_STATES.SHOWING:
        renderSuggestionState(payload);
        break;
      case AI_STATES.AGENT_REPLIED:
        renderAgentRepliedState(payload);
        break;
      case AI_STATES.ERROR:
        renderErrorState(payload);
        break;
      default:
        renderIdleState();
    }
  }

  // 检查页面是否就绪
  function isPageReady() {
    // 兼容新版工单详情 DOM 结构：
    // - 旧版：存在 `.m-sheet-reply` 回复区域
    // - 新版：仅存在 `.m-sheet-propertes` 工单信息区域
    const replyContainer = document.querySelector('.m-sheet-reply');
    const propertiesContainer = document.querySelector('.m-sheet-propertes');
    return Boolean(replyContainer || propertiesContainer);
  }

  // 提取对话消息
  function extractMessages() {
    const messages = [];
    const seenMessages = new Set(); // 用于去重
    const msgElements = document.querySelectorAll('.msg');

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
    return Boolean(document.querySelector('.m-sheet-main .m-sheet-rich-cont')) ||
           Boolean(document.querySelector('.m-sheet-reply')) ||
           Boolean(document.querySelector('.m-sheet-propertes'));
  }

  let worksheetObserver = null;
  let worksheetObserverTarget = null;
  let worksheetUpdateTimer = null;
  let worksheetHeartbeatTimer = null;
  let lastWorksheetTicketId = null;

  function scheduleWorksheetUpdate() {
    if (worksheetUpdateTimer) {
      clearTimeout(worksheetUpdateTimer);
    }
    worksheetUpdateTimer = setTimeout(() => {
      worksheetUpdateTimer = null;
      if (!isWorksheetMode()) return;

      const snapshot = buildConversationSnapshot();
      if (!snapshot || !snapshot.hash) return;

      if (!lastConversationHash) {
        lastConversationSnapshot = snapshot;
        lastConversationHash = snapshot.hash;
        return;
      }

      if (snapshot.hash === lastConversationHash) return;

      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;

      if (currentState === AI_STATES.SHOWING && currentSuggestion) {
        renderSuggestionState({
          suggestion: currentSuggestion,
          hint: '检测到工单内容更新，可重新生成建议保持准确'
        });
        return;
      }

      const box = getOrCreateSuggestionBox();
      if (!box) return;
      box.querySelector('.ai-suggestion-content').innerHTML = `
        检测到工单内容更新，可点击“手动生成建议”获取最新建议
      `;
      box.querySelector('.ai-suggestion-actions').innerHTML = `
        <button class="ai-manual-btn">手动生成建议</button>
      `;
      const manualBtn = box.querySelector('.ai-manual-btn');
      manualBtn.addEventListener('click', () => handleManualGenerate());
    }, 400);
  }

  function setupWorksheetObserver() {
    const target = document.body;
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
      scheduleWorksheetUpdate();
    });
    worksheetObserverTarget = target;
    worksheetObserver.observe(target, { subtree: true, childList: true, characterData: true });
  }

  function getCurrentWorksheetTicketId() {
    try {
      const params = new URLSearchParams(window.location.search);
      const id = params.get('id');
      if (id && id !== 'undefined' && id !== 'null') return String(id);
    } catch (e) {
    }
    const titleEl = document.querySelector('.m-sheet-main .sheet-title .fishd-ellipsis-ellipsis');
    const title = titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '';
    return title ? `title:${title}` : '';
  }

  function resetWorksheetRuntimeState(reason) {

    resetSessionCache();
    currentSuggestion = null;
    currentKnowledgeSources = [];
    currentRequestToken = null;
    lastConversationHash = '';
    lastConversationSnapshot = null;
    lastSuggestionHash = '';
    isManualRegenerate = false;
    transitionTo(AI_STATES.IDLE);
  }

  function ensureWorksheetAssistantMounted() {
    if (!isWorksheetMode()) return;
    setupWorksheetObserver();
    const ticketId = getCurrentWorksheetTicketId();
    if (ticketId && lastWorksheetTicketId && ticketId !== lastWorksheetTicketId) {
      resetWorksheetRuntimeState('ticket_id_changed');
    }
    if (ticketId) {
      lastWorksheetTicketId = ticketId;
    }
    getOrCreateSuggestionBox();
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

  function collectTicketFieldMap() {
    const fieldMap = {};
    const dls = document.querySelectorAll('.m-sheet-main dl');
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

  function splitTags(raw) {
    if (!raw) return [];
    return String(raw)
      .split(/[，,、;；|\n\r\t]+/)
      .map(item => normalizeText(item))
      .filter(Boolean);
  }

  function sanitizeTags(tags) {
    const invalidRegs = [/^标签$/, /^全部$/, /^更多$/, /^编辑$/, /^添加$/, /^无$/];
    const seen = new Set();
    const result = [];
    tags.forEach(tag => {
      let value = clipText(normalizeText(tag), 30);
      value = value.replace(/\s*[×✕✖]\s*$/, '').trim();
      if (!value) return;
      if (value.length < 1 || value.length > 30) return;
      if (invalidRegs.some(reg => reg.test(value))) return;
      if (seen.has(value)) return;
      seen.add(value);
      result.push(value);
    });
    return result.slice(0, 20);
  }

  function extractTicketTags(mainRoot, fieldMap) {
    const tags = [];
    const fieldTagValue = pickFieldValue(fieldMap, ['工单标签', '标签', '标签列表', '问题标签', '客户标签']);
    tags.push(...splitTags(fieldTagValue));
    const rowTagValue = pickValueByLabelsFromRoot(mainRoot, ['工单标签', '标签', '标签列表', '问题标签', '客户标签']);
    tags.push(...splitTags(rowTagValue));

    const roots = [mainRoot, document.querySelector('.m-app-worksheet'), document.body].filter(Boolean);
    roots.forEach(root => {
      const labelNodes = root.querySelectorAll('label[title], .fishd-form-item-label label');
      labelNodes.forEach(labelNode => {
        const labelText = normalizeText(labelNode.innerText || labelNode.textContent || '');
        if (!labelText || !labelText.includes('工单标签')) return;
        const row = labelNode.closest('.fishd-row, .fishd-form-item, .ant-form-item, dl, tr, li, div');
        if (!row) return;
        const rowTagNodes = row.querySelectorAll('.m-ws-tags .tags-item, .m-ws-tags .ant-tag, .ant-tag .tags-item');
        rowTagNodes.forEach(tagNode => {
          const raw = normalizeText(tagNode.innerText || tagNode.textContent || '');
          if (raw) tags.push(raw);
        });
      });

      const tagNodes = root.querySelectorAll(
        '.m-ws-tags .tags-item, .m-ws-tags .ant-tag, .fishd-tag, .ant-tag, [class*="ticket-tag"], [class*="tag-item"], [class*="tagItem"], [class*="u-tag"], [class*="m-tag"]'
      );
      tagNodes.forEach(node => {
        const text = normalizeText(node.innerText || node.textContent || '');
        if (!text) return;
        tags.push(text);
      });
    });

    return sanitizeTags(tags);
  }

  function isVisibleForHistory(el) {
    if (!el || !el.isConnected) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    return true;
  }

  function extractHistorySummary(maxItems = 5) {
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
    const currentTicketId = new URLSearchParams(window.location.search).get('id'); // 获取当前工单ID
    
    const nodes = document.querySelectorAll(selectors.join(','));
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
      const fallbackNodes = document.querySelectorAll([
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
      tags: ticketData.tags || [],
      tags_count: Array.isArray(ticketData.tags) ? ticketData.tags.length : 0,
      history_count: Array.isArray(ticketData.history_items) ? ticketData.history_items.length : 0
    };
    console.log(payload);
    if (ticketData.history_items && ticketData.history_items.length > 0) {
      console.table(ticketData.history_items.map((item, i) => ({ index: i + 1, summary: item })));
    }
    console.groupEnd();
  }

  function extractTicketData() {
    const ticketIdFromQuery = new URLSearchParams(window.location.search).get('id');
    // 适配新版 DOM，将 `.m-sheet-propertes` 视为主要工单容器
    const mainRoot =
      document.querySelector('.m-detail-content-wrapper') ||
      document.querySelector('.m-detail') ||
      document.querySelector('.m-sheet-main') ||
      document.querySelector('.m-sheet-propertes') ||
      document.body;
    const sheetRoot =
      document.querySelector('.m-sheet-main') ||
      document.querySelector('.m-sheet-propertes') ||
      mainRoot;
    const titleEl = document.querySelector('.m-sheet-main .sheet-title .fishd-ellipsis-ellipsis');
    const titleInput = document.querySelector('.m-sheet-main .sheet-title input.fishd-input');
    const title = extractNodeText(titleEl) || (titleInput && titleInput.value ? titleInput.value.trim() : '');
    const descNode =
      document.querySelector('.m-sheet-main .sheet-cont .m-sheet-rich-cont') ||
      // 新版页面中，工单描述通常位于富文本/多行输入中，这里兜底从属性区域内查找
      (sheetRoot && sheetRoot.querySelector('.m-sheet-rich-cont, textarea, .native-scrollbar'));
    const desc = clipText(readRichText(descNode), 3000);
    const fieldMap = collectTicketFieldMap();
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
      pickFieldValue(fieldMap, ['优先级', '紧急程度', '严重程度', '优先等级']) ||
      pickValueByLabelsFromRoot(mainRoot, ['优先级', '紧急程度', '严重程度', '优先等级']) ||
      pickValueFromText(mainText, ['优先级', '紧急程度', '严重程度', '优先等级']);
    const status =
      pickFieldValue(fieldMap, ['工单状态', '状态', '处理状态', '当前状态']) ||
      pickValueByLabelsFromRoot(mainRoot, ['工单状态', '状态', '处理状态', '当前状态']) ||
      pickValueFromText(mainText, ['工单状态', '状态', '处理状态', '当前状态']);
    
    // 直接从 .m-sheet-propertes 容器中提取核心字段（最可靠的方式）
    const propertiesContainer = document.querySelector('.m-sheet-propertes');
    
    // Core Info Extraction - 优先从 .m-sheet-propertes 中直接查找
    const customerName = 
      (propertiesContainer && propertiesContainer.querySelector('label[title*="客户名称"]')?.closest('.fishd-row')?.querySelector('input.fishd-input')?.value) ||
      pickFieldValue(fieldMap, ['客户名称', '客户姓名', '客户']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['客户名称', '客户姓名', '客户']) || '';
      
    const projectName = 
      (propertiesContainer && propertiesContainer.querySelector('label[title*="项目名称"]')?.closest('.fishd-row')?.querySelector('input.fishd-input')?.value) ||
      pickFieldValue(fieldMap, ['项目名称', '所属项目', '项目']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['项目名称', '所属项目', '项目']) || '';
      
    const mallName = 
      (propertiesContainer && propertiesContainer.querySelector('label[title*="商城名称"]')?.closest('.fishd-row')?.querySelector('input.fishd-input')?.value) ||
      pickFieldValue(fieldMap, ['商城名称', '店铺名称', '来源商城']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['商城名称', '店铺名称', '来源商城']) || '';

    // Attention Info Extraction - 优先从 .m-sheet-propertes 中直接查找
    const projectAttention = 
      (propertiesContainer && propertiesContainer.querySelector('label[title*="项目注意事项"]')?.closest('.fishd-row')?.querySelector('textarea.ant-input')?.value) ||
      pickFieldValue(fieldMap, ['项目注意事项', '项目备注']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['项目注意事项', '项目备注']) || '';

    const supplierAttention = 
      (propertiesContainer && propertiesContainer.querySelector('label[title*="供应商注意事项"]')?.closest('.fishd-row')?.querySelector('textarea.ant-input')?.value) ||
      pickFieldValue(fieldMap, ['供应商注意事项', '供应商备注']) || 
      pickValueByLabelsFromRoot(propertiesContainer || mainRoot, ['供应商注意事项', '供应商备注']) || '';

    const tags = extractTicketTags(mainRoot, fieldMap);
    const historyData = extractHistorySummary(5);

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
    if (tags.length > 0) parts.push(`工单标签：${tags.join('、')}`);

    return {
      title,
      desc,
      priority: priority || null,
      status: status || null,
      tags,
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

  function extractWorksheetMessages() {
    const ticket = extractTicketData();
    
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

  function buildConversationSnapshot() {
    let messages = [];
    const worksheetMessages = extractWorksheetMessages();
    if (worksheetMessages && worksheetMessages.length > 0) {
      messages = worksheetMessages;
    } else {
      messages = extractMessages();
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

  function resetSessionCache() {
  }

  // 填充建议到输入框
  function fillSuggestion(text) {
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

    const replyContainer = document.querySelector('.m-sheet-reply');
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

    for (const el of visibleTextControls) {
      el.focus();
      setNativeValue(el, text);
      dispatchTextInputEvents(el, text);
      if (!snippet || String(el.value || '').includes(snippet)) {
        wroteToEditor = true;
        break;
      }
    }

    if (!wroteToEditor) {
      const ceCandidates = replyContainer
        ? Array.from(replyContainer.querySelectorAll('[contenteditable="true"], .ql-editor'))
        : Array.from(document.querySelectorAll('.m-sheet-reply [contenteditable="true"], .m-sheet-reply .ql-editor'));

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
      const replyRoot = replyContainer ? replyContainer.querySelector('.fishd-input') : document.querySelector('.fishd-input');
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

    // 清除手动重新生成标志
    isManualRegenerate = false;

    // 移除建议框
    const box = document.querySelector('.ai-suggestion-box');
    if (box) box.remove();

    transitionTo(AI_STATES.IDLE);
  }

  // 请求AI建议
  async function requestSuggestion(options = {}) {
    if (isSummaryGenerating) {
      log('信息总结生成中，暂停建议生成');
      return;
    }
    if (currentState === AI_STATES.GENERATING) {
      log('已在生成中，忽略新的生成请求');
      return;
    }

    const snapshot = options.snapshot || buildConversationSnapshot();
    const messages = snapshot.messages;
    const trigger = options.trigger || 'auto';

    try {
      const worksheetMode =
        (messages &&
          messages.length === 1 &&
          messages[0] &&
          messages[0].sender === '工单') ||
        isWorksheetMode();
      const ticketData = worksheetMode ? extractTicketData() : null;
      if (worksheetMode) {
        debugTicketExtraction(ticketData);
      }

      if (messages.length < CONFIG.MIN_MESSAGES) {
        log('消息不足，暂不生成建议');
        lastConversationSnapshot = snapshot;
        lastConversationHash = snapshot.hash;
        transitionTo(AI_STATES.IDLE);
        return;
      }

      const requestToken = Symbol('request');
      currentRequestToken = requestToken;
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;

      // 显示加载状态
      showLoadingState(options.message);

      // 获取客服输入的补充信息
      const customInputEl = document.querySelector('.ai-custom-input');
      const customInputText = customInputEl ? (customInputEl.value || '').trim() : '';

      const requestData = {
        intent: "suggestion",
        query: messages.length > 0 ? messages[messages.length - 1].content : "",
        works_info: {
          title: ticketData?.title || "",
          desc: ticketData?.desc || (messages.length > 0 ? messages[messages.length - 1].content : ""),
          tags: ticketData?.tags || [],
          history: Array.isArray(ticketData?.history_items)
            ? ticketData.history_items
              .map((item, index) => ({
                index: index + 1,
                summary: String(item || '').trim(),
              }))
              .filter(row => row.summary)
            : [],
          custom_input: customInputText,
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

      // 构建统一 /chat API 路径
      const apiUrl = resolveChatApiUrl();

      // 通过 background script 调用 API（绕过 CORS）
      const apiResponse = await new Promise((resolve, reject) => {
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

      const data = apiResponse;
      if (currentRequestToken !== requestToken) {
        log('请求已被取消，忽略返回结果');
        return;
      }

      currentRequestToken = null;

      if (data.suggestion) {
        currentSuggestion = data.suggestion;
        // 保存知识源信息
        currentKnowledgeSources = data.knowledge_sources || [];
        transitionTo(AI_STATES.SHOWING, {
          suggestion: data.suggestion,
          knowledgeSources: currentKnowledgeSources
        });
        lastSuggestionHash = lastConversationSnapshot ? lastConversationSnapshot.hash : snapshot.hash;
        
        // 如果是手动重新生成，在成功显示后保持标志，直到用户采纳或隐藏
        // 如果不是手动重新生成，清除标志
        if (options.trigger !== 'regenerate') {
          isManualRegenerate = false;
        }
      } else {
        throw new Error('API返回数据格式错误');
      }

    } catch (error) {
      if (currentRequestToken === null) {
        log(`生成已取消或已恢复空闲: ${error.message}`);
        return;
      }
      currentRequestToken = null;
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

      transitionTo(AI_STATES.ERROR, { errorMessage: userMessage });
    }
  }

  // 检查是否需要生成建议
  function shouldGenerateSuggestion() {
    const snapshot = buildConversationSnapshot();
    const { messages, lastMessage, hash, lastAgentMessage, lastCustomerMessage } = snapshot;

    if (isWorksheetMode()) {
      if (messages.length === 0) {
        return { shouldAutoGenerate: false, shouldShowManual: false, snapshot };
      }
      return {
        shouldAutoGenerate: false,
        shouldShowManual: false,
        snapshot,
        hasConversationChange: hash !== lastConversationHash
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
      hasConversationChange: hash !== lastConversationHash
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
    const snapshot = result.snapshot;
    if (!snapshot) return;
    if (isSummaryGenerating) {
      return;
    }

    const fromPolling = options.fromPolling || false;

    if (currentState === AI_STATES.GENERATING) {
      log('生成中，忽略本次检查');
      return;
    }

    if (currentState === AI_STATES.ERROR && fromPolling) {
      log('错误状态等待用户处理，轮询暂不干预');
      return;
    }

    if (fromPolling && isWorksheetMode() && currentState === AI_STATES.SHOWING) {
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
      return;
    }

    // 如果当前正在显示建议，且是手动重新生成的结果，保护它不被轮询覆盖
    if (currentState === AI_STATES.SHOWING && fromPolling && isManualRegenerate) {
      log('手动重新生成的建议正在显示，轮询不覆盖');
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
      return;
    }

    if (currentState === AI_STATES.SHOWING && fromPolling && result.hasConversationChange) {
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
      const hint = '检测到新的对话更新，可重新生成建议保持准确';
      renderSuggestionState({ suggestion: currentSuggestion || '', hint });
      return;
    }

    if (result.shouldAutoGenerate) {
      if (snapshot.hash === lastSuggestionHash) {
        lastConversationSnapshot = snapshot;
        lastConversationHash = snapshot.hash;
        log('当前客户消息已生成建议，跳过自动生成');
        return;
      }
      // 更新快照，避免重复触发
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
      await requestSuggestion({ trigger: 'auto', snapshot });
      return;
    }

    // 如果当前正在显示建议（可能是手动重新生成的），且来自轮询，不覆盖
    if (result.shouldShowManual) {
      // 如果当前状态是 SHOWING 且来自轮询，说明用户已经手动重新生成了建议，不应该覆盖
      if (currentState === AI_STATES.SHOWING && fromPolling) {
        log('当前正在显示建议（可能是手动重新生成的），轮询不覆盖为手动触发状态');
        lastConversationSnapshot = snapshot;
        lastConversationHash = snapshot.hash;
        return;
      }
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
      transitionTo(AI_STATES.AGENT_REPLIED, { lastAgentMessage: result.lastAgentMessage });
      return;
    }

    if (fromPolling) {
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
    } else if (!result.shouldAutoGenerate && !result.shouldShowManual) {
      lastConversationSnapshot = snapshot;
      lastConversationHash = snapshot.hash;
    }

    if (currentState !== AI_STATES.IDLE) {
      transitionTo(AI_STATES.IDLE);
    }
  }

  // 请求信息总结
  async function requestSummary() {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    isSummaryGenerating = true;

    const summaryContent = box.querySelector('.ai-summary-content');
    const summaryBtn = box.querySelector('.ai-summary-btn');
    
    // Set loading state for right column
    if (summaryBtn) {
        summaryBtn.disabled = true;
        summaryBtn.innerHTML = '<span class="ai-loading-spinner"></span>生成中...';
    }
    
    // Show loading in right column fields
    const fields = ['info-summary', 'review'];
    fields.forEach(field => {
        const el = box.querySelector(`.ai-summary-${field}`);
        if (el) el.innerHTML = '<span class="ai-loading-spinner"></span>加载中...';
    });

    try {
      // Build request data
      const snapshot = buildConversationSnapshot();
      const messages = snapshot.messages;
      
      const worksheetMode = isWorksheetMode();
      const ticketData = worksheetMode ? extractTicketData() : null;

      const requestData = {
        intent: "summary",
        query: messages.length > 0 ? messages[messages.length - 1].content : "",
        works_info: {
          title: ticketData?.title || "",
          desc: ticketData?.desc || "",
          tags: ticketData?.tags || [],
          history: Array.isArray(ticketData?.history_items)
            ? ticketData.history_items
              .map((item, index) => ({
                index: index + 1,
                summary: String(item || '').trim(),
              }))
              .filter(row => row.summary)
            : [],
          custom_input: "",
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
      if (data && data.summary) {
         const { info_summary, review, reviews } = data.summary;
         const sEl = box.querySelector('.ai-summary-info-summary');
         const rEl = box.querySelector('.ai-summary-review');
         
         if (sEl) sEl.textContent = info_summary || '待确认';
         if (rEl) rEl.textContent = reviews || review || '无';
      } else {
         throw new Error('返回数据格式错误');
      }

    } catch (error) {
      log(`生成总结失败: ${error.message}`, 'error');
      fields.forEach(field => {
        const el = box.querySelector(`.ai-summary-${field}`);
        if (el) el.innerHTML = '<span style="color:#ff6b6b">生成失败</span>';
      });
    } finally {
      isSummaryGenerating = false;
      if (summaryBtn) {
        summaryBtn.disabled = false;
        summaryBtn.textContent = '信息总结';
      }
    }
  }

  // 获取或创建常驻建议框
  function getOrCreateSuggestionBox() {
    let box = document.querySelector('.ai-suggestion-box');

    // 如果找到了隐藏的插件，重新显示它
    if (box && box.style.display === 'none') {
      box.style.display = 'block';
      return box;
    }

    // 如果没有找到，创建新的
    if (!box) {
      box = document.createElement('div');
      box.className = 'ai-suggestion-box';
      box.innerHTML = `
        <div class="ai-close-icon" title="收起">−</div>
        
        <div class="ai-minimized-view" title="点击展开">
          <div style="display:flex;align-items:center;gap:8px;">
            <span>💡 AI 建议助手</span>
          </div>
          <span style="font-size:18px;line-height:1;opacity:0.8;">+</span>
        </div>

        <div class="ai-layout-container">
          <!-- Left Column -->
          <div class="ai-left-column">
            <div class="ai-column-header">查询结果：</div>
            <div class="ai-suggestion-display">
               <div class="ai-suggestion-text">可为您生成查询内容</div>
            </div>
            
            <div class="ai-input-label">输入框：</div>
            <textarea class="ai-custom-input" placeholder="在此输入补充信息/限制..."></textarea>
            
            <div class="ai-actions-row">
              <button class="ai-btn ai-btn-primary ai-query-btn">查询</button>
              <button class="ai-btn ai-btn-secondary ai-summary-btn">信息总结</button>
              <button class="ai-btn ai-btn-text ai-hide-btn" style="border:none; background:transparent; opacity:0.8; color:white;">隐藏</button>
              
              <div class="ai-rag-references"></div>
            </div>
          </div>

          <!-- Right Column -->
          <div class="ai-right-column">
            <div class="ai-column-header">工单信息总结</div>
            <div class="ai-summary-content">
              <div class="ai-summary-section">
                  <div class="ai-summary-label" style="display:flex;align-items:center;justify-content:space-between;">
                    <span>工单回复建议：</span>
                    <div class="ai-suggestion-actions" style="display:flex;align-items:center;gap:8px;">
                      <button class="ai-btn ai-btn-primary ai-generate-btn" style="font-size:12px;padding:3px 10px;">生成建议</button>
                      <button class="ai-btn ai-btn-secondary ai-accept-btn" style="font-size:12px;padding:3px 10px;display:none;">采纳</button>
                    </div>
                  </div>
                  <div class="ai-summary-value ai-summary-suggestion">--</div>
              </div>
              <div class="ai-summary-section">
                  <div class="ai-summary-label">信息总结：</div>
                  <div class="ai-summary-value ai-summary-info-summary">--</div>
              </div>
              <div class="ai-summary-section">
                  <div class="ai-summary-label">注意事项：</div>
                  <div class="ai-summary-value ai-summary-review">--</div>
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
      const hideBtn = box.querySelector('.ai-hide-btn');
      const customInput = box.querySelector('.ai-custom-input');
      const minimizedView = box.querySelector('.ai-minimized-view');

      const toggleMinimize = (e) => {
        if (e) e.stopPropagation();
        box.classList.toggle('minimized');
      };

      closeIcon.addEventListener('click', toggleMinimize);
      hideBtn.addEventListener('click', toggleMinimize);
      minimizedView.addEventListener('click', toggleMinimize);
      
      generateBtn.addEventListener('click', () => handleManualGenerate());
      if (acceptBtn) acceptBtn.addEventListener('click', () => handleAcceptSuggestion());
      summaryBtn.addEventListener('click', () => requestSummary());
      if (queryBtn) queryBtn.addEventListener('click', () => handleQueryRequest());

      box.dataset.listenersAttached = 'true';

      // Input persistence
      if (customInput) {
        const key = 'ai_custom_input_' + (lastWorksheetTicketId || 'default');
        const savedInput = sessionStorage.getItem(key);
        if (savedInput) customInput.value = savedInput;
        customInput.addEventListener('input', () => {
          sessionStorage.setItem(key, customInput.value);
        });
      }

      // 插入到 DOM
      const replyContainer = document.querySelector('.m-sheet-reply');
      if (replyContainer) {
        replyContainer.style.position = 'relative';
        replyContainer.style.zIndex = '1';
        const firstDiv = replyContainer.querySelector('div');
        if (firstDiv) replyContainer.insertBefore(box, firstDiv);
        else replyContainer.appendChild(box);
        
        // Ensure visibility
        setTimeout(() => {
          box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }, 100);
      } else {
         if (box && box.parentNode) box.parentNode.removeChild(box);
         return null;
      }
    }
    
    // Ensure event listeners are attached (for existing box)
    if (box && !box.dataset.listenersAttached) {
       const closeIcon = box.querySelector('.ai-close-icon');
       const hideBtn = box.querySelector('.ai-hide-btn');
       const minimizedView = box.querySelector('.ai-minimized-view');
       
       const toggleMinimize = (e) => {
         if (e) e.stopPropagation();
         box.classList.toggle('minimized');
       };

       if (closeIcon) {
           const newClose = closeIcon.cloneNode(true);
           closeIcon.parentNode.replaceChild(newClose, closeIcon);
           newClose.addEventListener('click', toggleMinimize);
       }
       if (hideBtn) {
           const newHide = hideBtn.cloneNode(true);
           hideBtn.parentNode.replaceChild(newHide, hideBtn);
           newHide.addEventListener('click', toggleMinimize);
       }
       if (minimizedView) {
           // Replace minimized view to clear listeners if any
           const newView = minimizedView.cloneNode(true);
           minimizedView.parentNode.replaceChild(newView, minimizedView);
           newView.addEventListener('click', toggleMinimize);
       }
       
       box.dataset.listenersAttached = 'true';
    }
    
    return box;
  }

  async function handleQueryRequest() {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    const customInput = box.querySelector('.ai-custom-input');
    const queryBtn = box.querySelector('.ai-query-btn');
    const display = box.querySelector('.ai-suggestion-display');

    const query = customInput ? customInput.value.trim() : '';
    if (!query) {
      if (display) display.innerHTML = '<div class="ai-error" style="color:#ffcccb">⚠️ 请先在输入框中输入查询内容</div>';
      return;
    }

    // Loading state
    if (display) display.innerHTML = '<div class="ai-loading"><span class="ai-loading-spinner"></span>AI 正在查询中...</div>';
    if (queryBtn) { queryBtn.disabled = true; queryBtn.textContent = '查询中...'; }

    try {
      const snapshot = buildConversationSnapshot();
      const payload = {
        ...snapshot,
        custom_input: query,
      };
      const backendUrl = (await getBackendUrl()).replace(/\/$/, '');
      const resp = await fetch(`${backendUrl}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const result = data.result || data.content || data.answer || JSON.stringify(data);
      if (display) display.innerHTML = `<div class="ai-suggestion-text">${escapeHtml(result)}</div>`;
    } catch (err) {
      log(`查询失败: ${err.message}`, 'error');
      if (display) display.innerHTML = `<div class="ai-error" style="color:#ffcccb">⚠️ 查询失败：${escapeHtml(err.message)}</div>`;
    } finally {
      if (queryBtn) { queryBtn.disabled = false; queryBtn.textContent = '查询'; }
    }
  }

  function renderIdleState() {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    const display = box.querySelector('.ai-suggestion-display');
    if (display) display.innerHTML = '<div class="ai-suggestion-text">可输入查询内容...</div>';
    
    // Reset generate button
    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    if (generateBtn) {
        generateBtn.textContent = '生成建议';
        generateBtn.disabled = false;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'none';
        acceptBtn.disabled = false;
    }
  }

  function renderAgentRepliedState(options = {}) {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (summarysuggEl) summarysuggEl.textContent = '检测到客服已回复。点击“生成建议”可获取 AI 建议。';
  }

  function renderGeneratingState(options = {}) {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (summarysuggEl) summarysuggEl.innerHTML = `<div class="ai-loading" style="color:#1f2a3d;"><span class="ai-loading-spinner" style="border-color:rgba(31,42,61,0.25);border-top-color:#1f2a3d;"></span>${escapeHtml(options.message || 'AI 正在思考中...')}</div>`;
    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    if (generateBtn) {
        generateBtn.textContent = '生成中...';
        generateBtn.disabled = true;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'none';
    }
  }

  function renderSuggestionState(options = {}) {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    
    const suggestionText = options.suggestion || currentSuggestion || '';

    // 建议结果展示在右侧「工单回复建议」区域
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    if (summarysuggEl) summarysuggEl.textContent = suggestionText || '--';
    
    // RAG References（保留在左侧查询区）
    const ragContainer = box.querySelector('.ai-rag-references');
    const knowledgeSources = options.knowledgeSources || currentKnowledgeSources || [];
    if (ragContainer) {
        if (knowledgeSources.length > 0) {
            ragContainer.innerHTML = `<strong>参考依据：</strong><br/>${knowledgeSources.map(s => `• ${escapeHtml(s)}`).join('<br/>')}`;
            ragContainer.classList.add('visible');
        } else {
            ragContainer.classList.remove('visible');
        }
    }

    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    if (generateBtn) {
        generateBtn.textContent = '重新生成';
        generateBtn.disabled = false;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'inline-flex';
        acceptBtn.disabled = false;
    }
  }

  function renderErrorState(options = {}) {
    const box = getOrCreateSuggestionBox();
    if (!box) return;
    const summarysuggEl = box.querySelector('.ai-summary-suggestion');
    const message = options.errorMessage || '暂无信息，请稍后重试';
    if (summarysuggEl) summarysuggEl.innerHTML = `<span style="color:#ff6b6b">⚠️ ${escapeHtml(message)}</span>`;
    
    const generateBtn = box.querySelector('.ai-generate-btn');
    const acceptBtn = box.querySelector('.ai-accept-btn');
    if (generateBtn) {
        generateBtn.textContent = '重试';
        generateBtn.disabled = false;
    }
    if (acceptBtn) {
        acceptBtn.style.display = 'none';
    }
  }

  function showLoadingState(message) {
    transitionTo(AI_STATES.GENERATING, { message });
  }

  function handleManualGenerate() {
    // 手动生成时，重新构建快照以确保是最新的对话状态
    const currentSnapshot = buildConversationSnapshot();
    requestSuggestion({ trigger: 'manual', snapshot: currentSnapshot });
  }

  function handleAcceptSuggestion() {
    if (!currentSuggestion) return;
    fillSuggestion(currentSuggestion);
  }

  function handleCancelGenerate() {
    log('用户取消生成');
    currentRequestToken = null;
    isManualRegenerate = false; // 清除手动重新生成标志
    transitionTo(AI_STATES.IDLE);
  }

  // 处理重新生成请求
  function handleRegenerate() {

    isManualRegenerate = true; // 标记为手动重新生成，防止轮询覆盖
    
    // 重新构建快照以确保是最新的对话状态
    const currentSnapshot = buildConversationSnapshot();
    requestSuggestion({ trigger: 'regenerate', snapshot: currentSnapshot, message: '正在重新生成建议...' });
  }

  function handleRetry() {
    if (lastConversationSnapshot) {
      requestSuggestion({ trigger: 'retry', message: '正在重新生成建议...', snapshot: lastConversationSnapshot });
    } else {
      requestSuggestion({ trigger: 'retry', message: '正在重新生成建议...' });
    }
  }

  // 切换建议框显示/隐藏
  function toggleSuggestionBox() {
    const box = document.querySelector('.ai-suggestion-box');
    if (!box) return;
    box.classList.toggle('minimized');
  }

  function startWorksheetMode() {
    setupWorksheetObserver();
    scheduleWorksheetUpdate();
    if (!worksheetHeartbeatTimer) {
      worksheetHeartbeatTimer = setInterval(() => {
        ensureWorksheetAssistantMounted();
      }, 1000);
    }
  }

  // 主初始化函数
  async function init() {
    log('开始初始化...');

    if (window.top !== window) {
      const maybeWorksheet = document.querySelector('.m-sheet-reply') || document.querySelector('.m-sheet-main');
      if (!maybeWorksheet) {
        return;
      }
    }

    while (retryCount < CONFIG.MAX_RETRIES) {
      if (isPageReady()) {
        isActive = true;

        // 创建常驻建议框
        getOrCreateSuggestionBox();
        renderState();

        // 立即检查一次
        const result = shouldGenerateSuggestion();
        await handleSuggestionDecision(result);

        if (!isWorksheetMode()) {
          log('当前页面不是工单模式，停止初始化', 'warn');
          return;
        }
        startWorksheetMode();
        return;
      }
      retryCount++;
      await sleep(CONFIG.CHECK_INTERVAL);
    }

    if (window.top === window) {
      log('初始化失败：页面一直未就绪', 'error');
    }
  }

  // ========== 样式注入 ==========
  const style = document.createElement('style');
  style.textContent = `
    .ai-suggestion-box {
      background: linear-gradient(135deg, #6F63E9 0%, #A85AD8 100%);
      border-radius: 12px;
      padding: 24px;
      margin: 16px 0;
      color: white;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
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
        font-size: 14px;
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
      gap: 32px;
      align-items: flex-start;
    }

    /* Left Column */
    .ai-left-column {
      flex: 3;
      display: flex;
      flex-direction: column;
      gap: 16px;
      padding-right: 32px;
      border-right: 1px solid rgba(255,255,255,0.15);
      min-width: 0; /* Prevent flex overflow */
    }

    .ai-column-header {
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 4px;
      color: white;
      letter-spacing: 0.5px;
    }

    .ai-suggestion-display {
      background: rgba(255,255,255,0.15);
      border-radius: 8px;
      padding: 16px;
      min-height: 80px;
      font-size: 14px;
      line-height: 1.6;
      backdrop-filter: blur(4px);
      color: white;
      white-space: pre-wrap;
      box-shadow: inset 0 1px 4px rgba(0,0,0,0.05);
    }
    
    .ai-rag-references {
      font-size: 12px;
      color: rgba(255,255,255,0.8);
      background: rgba(0,0,0,0.1);
      padding: 6px 10px;
      border-radius: 6px;
      display: none; /* Default hidden */
      max-width: 260px;
      max-height: 48px;
      overflow-y: auto;
      line-height: 1.4;
      flex-grow: 1;
      margin-left: 4px;
    }
    .ai-rag-references::-webkit-scrollbar {
      width: 4px;
    }
    .ai-rag-references::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.3);
      border-radius: 2px;
    }
    .ai-rag-references.visible {
      display: block;
    }

    .ai-input-label {
      font-size: 14px;
      font-weight: 600;
      color: white;
      margin-top: 4px;
    }

    .ai-custom-input {
      width: 100%;
      background: rgba(255,255,255,0.95);
      border: 1px solid rgba(255,255,255,0.3);
      border-radius: 8px;
      padding: 12px;
      color: #333;
      font-size: 14px;
      resize: vertical;
      min-height: 60px;
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
      margin-top: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    
    .ai-rag-references {
      font-size: 12px;
      color: rgba(255,255,255,0.8);
      background: rgba(0,0,0,0.1);
      padding: 6px 10px;
      border-radius: 6px;
      display: none; /* Default hidden */
      max-width: 260px;
      max-height: 48px;
      overflow-y: auto;
      line-height: 1.4;
      flex-grow: 1;
      margin-left: 4px;
    }
    .ai-rag-references::-webkit-scrollbar {
      width: 4px;
    }
    .ai-rag-references::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.3);
      border-radius: 2px;
    }
    .ai-rag-references.visible {
      display: block;
    }
      padding: 8px 20px;
      border-radius: 6px;
      border: none;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
      transition: all 0.2s;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 36px;
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
    }
    .ai-btn-secondary:hover:not(:disabled) {
      background: rgba(255,255,255,0.3);
      border-color: rgba(255,255,255,0.4);
    }

    /* Right Column */
    .ai-right-column {
      flex: 2;
      display: flex;
      flex-direction: column;
      gap: 20px;
      padding-top: 0;
      min-width: 0;
    }

    .ai-summary-content {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .ai-summary-section {
      border-bottom: 1px solid rgba(255,255,255,0.15);
      padding-bottom: 12px;
    }
    .ai-summary-section:last-child {
      border-bottom: none;
    }

    .ai-summary-label {
      font-size: 14px;
      font-weight: 700;
      color:rgb(227, 221, 212);
      margin-bottom: 6px;
      opacity: 1;
    }

    .ai-summary-value {
      font-size: 14px;
      color: #1f2a3d;
      line-height: 1.5;
      min-height: 20px;
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

    /* Responsive */
    @media (max-width: 756px) {
      .ai-layout-container {
        flex-direction: column;
        gap: 24px;
      }
      .ai-left-column {
        border-right: none;
        border-bottom: 1px solid rgba(255,255,255,0.15);
        padding-right: 0;
        padding-bottom: 24px;
      }
    }
  `;
  document.head.appendChild(style);

  // 启动插件
  init();

})();
